"""Deterministic extraction of loan-product facts from a public lender page.

Rules are regex-based over visible page text. Every extracted value keeps the
text snippet it came from (``extraction_notes``) so it can be audited. When a
page states several values for the same criterion (e.g. different segments),
the *strictest* value is stored and a note is recorded, so a user is never told
they qualify on the basis of a looser segment's rule.

Anything that cannot be extracted is left as None (unknown).
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from urllib import robotparser

NUM = r"(\d{1,2}(?:\.\d{1,2})?)"
_RATE_CONTEXT_EXCLUDE = re.compile(r"penal|processing|prepayment|foreclosure|charge|fee|overdue|late|bounce", re.I)
_RATE_RANGE = re.compile(NUM + r"\s*%?\s*(?:-|–|to)\s*" + NUM + r"\s*%\s*(?:p\.?\s*a\.?|per annum)", re.I)
_RATE_FROM = re.compile(
    r"(?:starting|starts|start)\s*(?:at|from|@)?\s*(?:@\s*)?" + NUM + r"\s*%\s*\*?\s*(?:p\.?\s*a\.?|per annum)?", re.I
)
_RATE_FROM_ALT = re.compile(
    r"(?:rates?\s+of\s+interest|interest\s+rates?)\s+(?:starting\s+)?from\s+" + NUM + r"\s*%", re.I
)

# Clause body: any char except a sentence-ending period ("Rs." and "30,000.00" are not sentence ends).
_CLAUSE_CHAR = r"(?:[^.]|(?<=rs)\.|\.(?=\d))"
_INCOME_CLAUSE = re.compile(
    r"(?:minimum|min\.?)\s+(?:net\s+)?(?:monthly\s+)?(?:income|salary)" + _CLAUSE_CHAR + r"{0,110}"
    r"|income\s*:\s*minimum" + _CLAUSE_CHAR + r"{0,60}",
    re.I,
)
_AMOUNT = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]{4,})(?:\.\d{1,2})?(?!\s*(?:lakh|lac|crore|cr)\b)", re.I)

_AGE_RANGE = re.compile(r"(?:between|age\s*:?|aged)\s*(\d{2})\s*(?:and|to|-|–)\s*(\d{2})\s*(?:years|yrs)", re.I)
_AGE_MIN = re.compile(r"(?:minimum\s+age\s+(?:of\s+)?|at\s+least\s+)(\d{2})\s*(?:years|yrs)", re.I)
_AGE_MAX = re.compile(r"maximum\s+age\s+(?:of\s+)?(\d{2})\s*(?:years|yrs)", re.I)

_SCORE = re.compile(r"(?:cibil|credit)\s*score[^.]{0,60}?\b([3-9]\d{2})\b", re.I)
_SCORE_HARD = re.compile(r"minimum|at least|required|must|should be", re.I)
_SCORE_ADVISORY = re.compile(r"ideal|recommended|preferred|good|better", re.I)

_EMPLOYMENT_CLAUSE = re.compile(r"employment\s*(?:type)?\s*:\s*([^.]{10,240})", re.I)
_SALARIED = re.compile(r"\bsalaried\b", re.I)
_SELF_EMPLOYED = re.compile(r"\bself[\s-]?employed\b", re.I)
# Statements about which documents prove income say nothing about who is eligible.
# Not the bare word "document": tab labels such as "Eligibility Documentation" sit next to real criteria.
_DOCUMENT_PROOF = re.compile(
    r"\bproof\b|salary\s+slips?|bank\s+statements?|\bITR\b|income\s+tax\s+returns?|taxation", re.I
)

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_YEARS = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\s*(?:\+\s*)?years?"
_EXPERIENCE_PATTERNS = (
    # "Work Experience: 2 years"
    re.compile(r"(?:work|overall|total)\s+experience\s*:?\s*(?:of\s+)?" + _YEARS, re.I),
    # "at least one year of work experience", "at least one year or more work experience"
    re.compile(
        r"at\s+least\s+" + _YEARS + r"\s+(?:or\s+more\s+)?(?:of\s+)?(?:work\s+|total\s+|overall\s+)?experience", re.I
    ),
    # "An Employment period of at least one year"
    re.compile(r"employment\s+period\s+of\s+at\s+least\s+" + _YEARS, re.I),
)


@dataclass
class ExtractedProduct:
    interest_rate_min: Decimal | None = None
    interest_rate_max: Decimal | None = None
    interest_rate_text: str | None = None
    min_monthly_income: Decimal | None = None
    min_credit_score: int | None = None
    min_age: int | None = None
    max_age: int | None = None
    employment_types: list[str] | None = None
    employment_criteria_text: str | None = None
    other_criteria: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def extracted_field_count(self) -> int:
        values = [
            self.interest_rate_min,
            self.min_monthly_income,
            self.min_credit_score,
            self.min_age,
            self.max_age,
            self.employment_types,
        ]
        return sum(v is not None for v in values)


def html_to_text(raw_html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw_html)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _snippet(text: str, match: re.Match[str], pad: int = 40) -> str:
    start, end = max(0, match.start() - pad), min(len(text), match.end() + pad)
    return text[start:end].strip()


def _to_decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def extract_rates(text: str, out: ExtractedProduct) -> None:
    ranges: list[tuple[Decimal, Decimal, str]] = []
    for m in _RATE_RANGE.finditer(text):
        context = text[max(0, m.start() - 60) : m.end()]
        if _RATE_CONTEXT_EXCLUDE.search(context) or "emi" in context.lower():
            continue
        low, high = _to_decimal(m.group(1)), _to_decimal(m.group(2))
        if Decimal("1") <= low <= high <= Decimal("60"):
            ranges.append((low, high, _snippet(text, m)))
    starts: list[tuple[Decimal, str]] = []
    for regex in (_RATE_FROM, _RATE_FROM_ALT):
        for m in regex.finditer(text):
            context = text[max(0, m.start() - 60) : m.end()]
            if _RATE_CONTEXT_EXCLUDE.search(context):
                continue
            value = _to_decimal(m.group(1))
            if Decimal("1") <= value <= Decimal("60"):
                starts.append((value, _snippet(text, m)))

    if ranges:
        low, high, snippet = min(ranges, key=lambda r: (r[0], -r[1]))
        out.interest_rate_min, out.interest_rate_max, out.interest_rate_text = low, high, snippet
        out.notes.append(f"interest rate range from: '{snippet}'")
    elif starts:
        values = sorted({v for v, _ in starts})
        out.interest_rate_min = values[0]
        out.interest_rate_text = f"starting from {values[0]}% p.a."
        out.notes.append(f"interest rate 'starting from' value from: '{starts[0][1]}'")
        if len(values) > 1:
            out.notes.append(f"several 'starting' rates on page {[str(v) for v in values]}; lowest published kept")
        out.notes.append("maximum interest rate not published on page")


def extract_income(text: str, out: ExtractedProduct) -> None:
    amounts: list[Decimal] = []
    for m in _INCOME_CLAUSE.finditer(text):
        found = [_to_decimal(a) for a in _AMOUNT.findall(m.group(0))]
        found = [a for a in found if Decimal("1000") <= a <= Decimal("10000000")]
        if found:
            amounts.extend(found)
            out.notes.append(f"income clause: '{m.group(0)[:160].strip()}'")
    if amounts:
        out.min_monthly_income = max(amounts)
        if len(set(amounts)) > 1:
            out.notes.append(f"several minimum incomes {sorted({str(a) for a in amounts})}; strictest kept")


def extract_age(text: str, out: ExtractedProduct) -> None:
    mins: list[int] = []
    maxs: list[int] = []
    for m in _AGE_RANGE.finditer(text):
        low, high = int(m.group(1)), int(m.group(2))
        if 18 <= low < high <= 100:
            mins.append(low)
            maxs.append(high)
            out.notes.append(f"age range from: '{_snippet(text, m, 20)}'")
    for m in _AGE_MIN.finditer(text):
        if 18 <= int(m.group(1)) <= 100:
            mins.append(int(m.group(1)))
            out.notes.append(f"minimum age from: '{_snippet(text, m, 20)}'")
    for m in _AGE_MAX.finditer(text):
        if 18 <= int(m.group(1)) <= 100:
            maxs.append(int(m.group(1)))
            out.notes.append(f"maximum age from: '{_snippet(text, m, 20)}'")
    if mins:
        out.min_age = max(mins)
    if maxs:
        out.max_age = min(maxs)
    if len(set(mins)) > 1 or len(set(maxs)) > 1:
        out.notes.append("page lists several age limits (likely per segment); strictest kept")
    if out.min_age is not None and out.max_age is not None and out.min_age > out.max_age:
        out.notes.append("conflicting age limits; age left unknown")
        out.min_age = out.max_age = None


def extract_credit_score(text: str, out: ExtractedProduct) -> None:
    hard: list[int] = []
    for m in _SCORE.finditer(text):
        # Judge the clause itself (not the previous sentence) for hard vs advisory wording.
        clause = m.group(0) + text[m.end() : m.end() + 40]
        score = int(m.group(1))
        if not 300 <= score <= 900:
            continue
        if _SCORE_ADVISORY.search(clause) and not _SCORE_HARD.search(clause):
            out.other_criteria.append(f"Lender guidance (not a stated minimum): {_snippet(text, m, 20)}")
            continue
        if _SCORE_HARD.search(clause):
            hard.append(score)
            out.notes.append(f"credit score clause: '{_snippet(text, m, 20)}'")
        else:
            # Neither a stated minimum nor guidance (e.g. a bare "685 or higher"): record it, never apply it.
            out.notes.append(f"credit score statement not applied (unclear wording): '{_snippet(text, m, 20)}'")
    if hard:
        out.min_credit_score = max(hard)


def _self_employed_is_eligibility(sentence: str) -> bool:
    """True if a self-employed mention is not part of an income-proof/document statement."""
    return any(
        not _DOCUMENT_PROOF.search(sentence[max(0, m.start() - 120) : m.end() + 120])
        for m in _SELF_EMPLOYED.finditer(sentence)
    )


def extract_employment(text: str, out: ExtractedProduct) -> None:
    clauses = [m.group(1).strip() for m in _EMPLOYMENT_CLAUSE.finditer(text)]
    eligibility_window = " ".join(
        text[max(0, m.start() - 300) : m.end() + 600] for m in re.finditer(r"eligib", text, re.I)
    )
    haystack = " ".join(clauses) + " " + eligibility_window
    types = []
    if _SALARIED.search(haystack):
        types.append("salaried")
    if any(_self_employed_is_eligibility(sentence) for sentence in re.split(r"(?<=[.!?])\s+", haystack)):
        types.append("self_employed")
    if types:
        out.employment_types = types
        out.notes.append(f"employment types from eligibility keywords: {types}")
    if clauses:
        out.employment_criteria_text = clauses[0][:240]
    matches = sorted((m for p in _EXPERIENCE_PATTERNS for m in p.finditer(text)), key=lambda m: m.start())
    for m in matches:
        raw = m.group(1).lower()
        years = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
        criterion = f"Work experience: {years} year{'' if years == 1 else 's'} (as published)"
        if criterion not in out.other_criteria:
            out.other_criteria.append(criterion)


def extract_product(raw_html: str) -> ExtractedProduct:
    text = html_to_text(raw_html)
    out = ExtractedProduct()
    extract_rates(text, out)
    extract_income(text, out)
    extract_age(text, out)
    extract_credit_score(text, out)
    extract_employment(text, out)
    out.other_criteria = out.other_criteria[:10]
    out.notes = [n[:300] for n in out.notes[:40]]
    return out


def robots_allows(robots_txt: str, user_agent: str, url: str) -> bool:
    parser = robotparser.RobotFileParser()
    parser.parse(robots_txt.splitlines())
    return parser.can_fetch(user_agent, url)

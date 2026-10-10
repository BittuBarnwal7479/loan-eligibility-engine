"""CSV parsing and row validation.

Pure functions with no database or AWS dependencies so they are unit-testable
and reusable from both the local API and the S3-triggered Lambda.

Error messages never echo raw cell values (which may be personal data); they
reference the row number, field name and an error code only.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

REQUIRED_COLUMNS: tuple[str, ...] = (
    "user_id",
    "email",
    "monthly_income",
    "credit_score",
    "employment_status",
    "age",
)

MIN_CREDIT_SCORE, MAX_CREDIT_SCORE = 300, 900
MIN_AGE, MAX_AGE = 18, 100
MAX_MONTHLY_INCOME = Decimal("100000000")  # sanity bound, not an eligibility rule
MAX_USER_ID_LENGTH = 64

EMPLOYMENT_ALIASES: dict[str, str] = {
    "salaried": "salaried",
    "employed": "salaried",
    "self_employed": "self_employed",
    "selfemployed": "self_employed",
    "self_employed_professional": "self_employed",
    "unemployed": "unemployed",
    "student": "student",
    "retired": "retired",
}

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class CsvHeaderError(ValueError):
    """The file cannot be processed at all (bad header, encoding, empty)."""


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    email: str
    monthly_income: Decimal
    credit_score: int
    employment_status: str
    age: int


@dataclass(frozen=True)
class RowError:
    row: int  # 1-based line number in the file, header is line 1
    field: str | None
    code: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {"row": self.row, "field": self.field, "code": self.code, "message": self.message}


@dataclass
class ParseResult:
    records: list[UserRecord] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    total_rows: int = 0
    failed_rows: int = 0


def normalize_header(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lstrip("﻿").lower())


def normalize_employment(value: str) -> str | None:
    key = re.sub(r"[\s\-]+", "_", value.strip().lower())
    return EMPLOYMENT_ALIASES.get(key) or EMPLOYMENT_ALIASES.get(key.replace("_", ""))


def validate_header(header: list[str]) -> list[str]:
    normalized = [normalize_header(h) for h in header]
    missing = [c for c in REQUIRED_COLUMNS if c not in normalized]
    if missing:
        raise CsvHeaderError(f"missing required column(s): {', '.join(missing)}")
    duplicates = sorted({c for c in normalized if normalized.count(c) > 1 and c in REQUIRED_COLUMNS})
    if duplicates:
        raise CsvHeaderError(f"duplicate column(s): {', '.join(duplicates)}")
    return normalized


def _parse_int(raw: str) -> int | None:
    raw = raw.strip()
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


def validate_row(row: dict[str, str], line: int) -> tuple[UserRecord | None, list[RowError]]:
    errors: list[RowError] = []

    def err(field_name: str, code: str, message: str) -> None:
        errors.append(RowError(line, field_name, code, message))

    values = {k: (row.get(k) or "").strip() for k in REQUIRED_COLUMNS}
    for name, value in values.items():
        if not value:
            err(name, "required", f"{name} is required")

    user_id = values["user_id"]
    if user_id and (len(user_id) > MAX_USER_ID_LENGTH or not _USER_ID_RE.match(user_id)):
        err("user_id", "invalid_format", "user_id must be 1-64 chars of letters, digits, _ . : -")

    email = values["email"].lower()
    if email and not _EMAIL_RE.match(email):
        err("email", "invalid_format", "email is not a valid address")

    income: Decimal | None = None
    if values["monthly_income"]:
        try:
            income = Decimal(values["monthly_income"].replace(",", ""))
        except InvalidOperation:
            err("monthly_income", "invalid_type", "monthly_income must be a number")
        else:
            if not income.is_finite() or income < 0 or income > MAX_MONTHLY_INCOME:
                err("monthly_income", "out_of_range", "monthly_income must be between 0 and 100000000")
                income = None
            else:
                income = income.quantize(Decimal("0.01"))

    score: int | None = None
    if values["credit_score"]:
        score = _parse_int(values["credit_score"])
        if score is None:
            err("credit_score", "invalid_type", "credit_score must be an integer")
        elif not MIN_CREDIT_SCORE <= score <= MAX_CREDIT_SCORE:
            err(
                "credit_score",
                "out_of_range",
                f"credit_score must be between {MIN_CREDIT_SCORE} and {MAX_CREDIT_SCORE}",
            )
            score = None

    age: int | None = None
    if values["age"]:
        age = _parse_int(values["age"])
        if age is None:
            err("age", "invalid_type", "age must be an integer")
        elif not MIN_AGE <= age <= MAX_AGE:
            err("age", "out_of_range", f"age must be between {MIN_AGE} and {MAX_AGE}")
            age = None

    employment: str | None = None
    if values["employment_status"]:
        employment = normalize_employment(values["employment_status"])
        if employment is None:
            err(
                "employment_status",
                "invalid_value",
                "employment_status must be one of salaried, self_employed, unemployed, student, retired",
            )

    if errors:
        return None, errors
    assert income is not None and score is not None and age is not None and employment is not None
    return UserRecord(user_id, email, income, score, employment, age), []


def iter_text_lines(chunks: Iterable[bytes]) -> Iterator[str]:
    """Decode a byte stream as UTF-8 (BOM tolerated) line by line."""
    buffer = io.TextIOWrapper(_ChunkReader(chunks), encoding="utf-8-sig", newline="")
    try:
        yield from buffer
    except UnicodeDecodeError as exc:
        raise CsvHeaderError("file is not valid UTF-8 text") from exc


class _ChunkReader(io.RawIOBase):
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._iter = iter(chunks)
        self._pending = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray) -> int:  # type: ignore[override]
        while not self._pending:
            try:
                self._pending = next(self._iter)
            except StopIteration:
                return 0
        size = min(len(b), len(self._pending))
        b[:size] = self._pending[:size]
        self._pending = self._pending[size:]
        return size


def parse_csv(lines: Iterable[str], max_rows: int, max_errors: int = 100) -> ParseResult:
    """Validate every row. Rows with errors are excluded and reported.

    Duplicate user_id values inside one file keep the first occurrence and
    report later ones, so the outcome does not depend on row order surprises.
    `max_errors` caps the *reported* errors; all rows are still counted.
    """
    reader = csv.reader(lines)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise CsvHeaderError("file is empty") from exc
    except csv.Error as exc:
        raise CsvHeaderError("file is not valid CSV") from exc
    columns = validate_header(header)

    result = ParseResult()
    seen: set[str] = set()
    error_rows = 0

    def report(errors: list[RowError]) -> None:
        room = max_errors - len(result.errors)
        if room > 0:
            result.errors.extend(errors[:room])

    try:
        for raw in reader:
            line = reader.line_num
            if not any(cell.strip() for cell in raw):
                continue  # ignore blank lines
            result.total_rows += 1
            if result.total_rows > max_rows:
                raise CsvHeaderError(f"file exceeds the maximum of {max_rows} data rows")
            if len(raw) != len(columns):
                error_rows += 1
                report([RowError(line, None, "column_count", f"expected {len(columns)} columns, found {len(raw)}")])
                continue
            record, errors = validate_row(dict(zip(columns, raw, strict=True)), line)
            if errors:
                error_rows += 1
                report(errors)
                continue
            assert record is not None
            if record.user_id in seen:
                error_rows += 1
                report(
                    [
                        RowError(
                            line, "user_id", "duplicate_in_file", "user_id appears earlier in this file; first row kept"
                        )
                    ]
                )
                continue
            seen.add(record.user_id)
            result.records.append(record)
    except csv.Error as exc:
        raise CsvHeaderError(f"malformed CSV near line {reader.line_num}") from exc

    result.failed_rows = error_rows
    return result

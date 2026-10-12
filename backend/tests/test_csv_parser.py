from decimal import Decimal

import pytest

from app.ingestion.csv_parser import CsvHeaderError, iter_text_lines, normalize_employment, parse_csv

HEADER = "user_id,email,monthly_income,credit_score,employment_status,age"


def parse(text: str, max_rows: int = 1000, max_errors: int = 100):
    return parse_csv(text.splitlines(keepends=True), max_rows, max_errors)


def test_valid_rows_are_parsed_and_normalized():
    result = parse(
        f'{HEADER}\nU1,Alice@Example.com,"55,000",742,Self-Employed,29\nU2,b@example.com,30000.5,650,salaried,45\n'
    )
    assert result.total_rows == 2 and result.failed_rows == 0 and not result.errors
    first = result.records[0]
    assert first.email == "alice@example.com"
    assert first.monthly_income == Decimal("55000.00")
    assert first.employment_status == "self_employed"
    assert result.records[1].monthly_income == Decimal("30000.50")


def test_header_is_case_and_space_insensitive_and_tolerates_bom_and_extra_columns():
    text = "﻿User ID,EMAIL,Monthly Income,credit-score,employment_status,AGE,notes\nU1,a@example.com,1000,700,salaried,30,hi\n"
    result = parse(text)
    assert len(result.records) == 1


@pytest.mark.parametrize("header", ["user_id,email,monthly_income", "", "a,b,c,d,e,f"])
def test_missing_columns_reject_the_whole_file(header):
    with pytest.raises(CsvHeaderError):
        parse(header + "\n")


def test_duplicate_required_column_is_rejected():
    with pytest.raises(CsvHeaderError, match="duplicate"):
        parse(HEADER + ",age\n")


def test_invalid_rows_are_reported_without_echoing_values():
    secret = "very-private@example.com"
    text = (
        f"{HEADER}\n"
        f"U1,{secret},abc,700,salaried,30\n"  # income not numeric
        "U2,not-an-email,1000,950,salaried,30\n"  # bad email + score out of range
        "U3,c@example.com,1000,700,astronaut,30\n"  # unknown employment
        "U4,d@example.com,1000,700,salaried,17\n"  # under age
        "U5,e@example.com,1000,700.5,salaried,30\n"  # non-integer score
        ",f@example.com,1000,700,salaried,30\n"  # missing user_id
        "U7,g@example.com,1000,700,salaried\n"  # wrong column count
        "U8,h@example.com,-5,700,salaried,30\n"  # negative income
    )
    result = parse(text)
    assert result.records == []
    assert result.total_rows == 8 and result.failed_rows == 8
    codes = {(e.row, e.field, e.code) for e in result.errors}
    assert (2, "monthly_income", "invalid_type") in codes
    assert (3, "email", "invalid_format") in codes
    assert (3, "credit_score", "out_of_range") in codes
    assert (4, "employment_status", "invalid_value") in codes
    assert (5, "age", "out_of_range") in codes
    assert (6, "credit_score", "invalid_type") in codes
    assert (7, "user_id", "required") in codes
    assert (8, None, "column_count") in codes
    assert (9, "monthly_income", "out_of_range") in codes
    assert all(secret not in e.message and "abc" not in e.message for e in result.errors)


def test_duplicate_user_id_keeps_first_row():
    result = parse(f"{HEADER}\nU1,a@example.com,1000,700,salaried,30\nU1,b@example.com,2000,710,salaried,31\n")
    assert [r.email for r in result.records] == ["a@example.com"]
    assert result.errors[0].code == "duplicate_in_file"


def test_blank_lines_are_ignored():
    result = parse(f"{HEADER}\n\nU1,a@example.com,1000,700,salaried,30\n,,,,,\n")
    assert result.total_rows == 1 and len(result.records) == 1


def test_reported_errors_are_capped_but_counted():
    rows = "".join(f"U{i},bad,1,700,salaried,30\n" for i in range(50))
    result = parse(HEADER + "\n" + rows, max_errors=10)
    assert len(result.errors) == 10 and result.failed_rows == 50


def test_row_limit_rejects_file():
    rows = "".join(f"U{i},a{i}@example.com,1,700,salaried,30\n" for i in range(6))
    with pytest.raises(CsvHeaderError, match="maximum"):
        parse(HEADER + "\n" + rows, max_rows=5)


def test_non_utf8_file_is_rejected():
    with pytest.raises(CsvHeaderError, match="UTF-8"):
        parse_csv(iter_text_lines([HEADER.encode() + b"\nU1,\xff\xfe,1,700,salaried,30\n"]), 10)


def test_streamed_chunks_split_mid_line_parse_correctly():
    data = f"{HEADER}\nU1,a@example.com,1000,700,salaried,30\n".encode()
    chunks = [data[i : i + 3] for i in range(0, len(data), 3)]
    assert len(parse_csv(iter_text_lines(chunks), 10).records) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Salaried", "salaried"),
        ("self employed", "self_employed"),
        ("SELF-EMPLOYED", "self_employed"),
        ("Retired", "retired"),
        ("freelancer", None),
    ],
)
def test_employment_normalization(raw, expected):
    assert normalize_employment(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Salaried", "salaried"),
        ("Self-Employed", "self_employed"),
        ("Business", "self_employed"),
        ("  salaried ", "salaried"),
        ("\tSELF-employed  ", "self_employed"),
        (" BUSINESS ", "self_employed"),
        ("business", "self_employed"),
        ("Businessman", None),
        ("Business Owner", None),
        ("Contractor", None),
        ("", None),
    ],
)
def test_real_csv_employment_categories(raw, expected):
    assert normalize_employment(raw) == expected


def test_real_csv_categories_accepted_and_unknown_rows_rejected():
    text = (
        f"{HEADER}\n"
        "U1,a@example.com,1000,700,Salaried,30\n"
        "U2,b@example.com,1000,700, Self-Employed ,30\n"
        'U3,c@example.com,1000,700," BUSINESS ",30\n'
        "U4,d@example.com,1000,700,Contractor,30\n"
    )
    result = parse(text)
    assert [r.employment_status for r in result.records] == ["salaried", "self_employed", "self_employed"]
    assert result.total_rows == 4 and result.failed_rows == 1
    assert [(e.row, e.field, e.code) for e in result.errors] == [(5, "employment_status", "invalid_value")]

import pytest

from fintextsql.text2sql.sql_guard import SQLValidationError, ensure_limit, validate_select_sql


def test_accepts_safe_select() -> None:
    sql = validate_select_sql("select ticker from companies")

    assert sql == "select ticker from companies"


def test_rejects_delete() -> None:
    with pytest.raises(SQLValidationError):
        validate_select_sql("delete from companies")


def test_rejects_unknown_table() -> None:
    with pytest.raises(SQLValidationError):
        validate_select_sql("select * from users")


def test_adds_limit() -> None:
    assert ensure_limit("select ticker from companies", 50).endswith("LIMIT 50")


def test_accepts_cte_names_when_underlying_tables_are_allowed() -> None:
    sql = validate_select_sql("with q as (select * from prices) select * from q")

    assert sql.startswith("with q")

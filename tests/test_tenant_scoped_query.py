import uuid

import pytest

from db import init_db
from services import core


@pytest.fixture
def conn():
    connection = init_db(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def _institution(conn, name):
    suffix = uuid.uuid4().hex
    return core.create_institution(conn, name, f"TEST-{suffix}")


@pytest.mark.parametrize("table", ["question_banks", "exam_blueprints"])
def test_scoped_query_returns_only_rows_for_requested_institution(conn, table):
    institution_a = _institution(conn, "Tenant A")
    institution_b = _institution(conn, "Tenant B")

    if table == "question_banks":
        conn.execute(
            "INSERT INTO question_banks (id, institution_id, name) VALUES (?, ?, ?)",
            ("bank-a", institution_a, "Tenant A bank"),
        )
        conn.execute(
            "INSERT INTO question_banks (id, institution_id, name) VALUES (?, ?, ?)",
            ("bank-b", institution_b, "Tenant B bank"),
        )
    else:
        conn.execute(
            "INSERT INTO exam_blueprints (id, institution_id, total_marks) VALUES (?, ?, ?)",
            ("blueprint-a", institution_a, 20),
        )
        conn.execute(
            "INSERT INTO exam_blueprints (id, institution_id, total_marks) VALUES (?, ?, ?)",
            ("blueprint-b", institution_b, 30),
        )
    conn.commit()

    rows = core.scoped_query(conn, table, institution_a)

    assert len(rows) == 1
    assert rows[0]["institution_id"] == institution_a
    assert rows[0]["institution_id"] != institution_b


def test_scoped_query_rejects_unapproved_table_names(conn):
    institution_id = _institution(conn, "Tenant")

    with pytest.raises(ValueError, match="unsupported_tenant_scoped_table"):
        core.scoped_query(conn, "question_banks WHERE 1=1 --", institution_id)


@pytest.mark.parametrize("institution_id", ["", "   ", None, 42])
def test_scoped_query_rejects_missing_or_invalid_institution_id(conn, institution_id):
    with pytest.raises(ValueError, match="institution_id_required"):
        core.scoped_query(conn, "question_banks", institution_id)

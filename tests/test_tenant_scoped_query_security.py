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


def test_scoped_query_rejects_unapproved_table_and_sql_fragments(conn):
    with pytest.raises(ValueError, match="unsupported_tenant_scoped_table"):
        core.scoped_query(conn, "classes WHERE 1=1 OR 1=1 --", "institution-a")


def test_scoped_query_requires_a_nonempty_institution_id(conn):
    with pytest.raises(ValueError, match="institution_id_required"):
        core.scoped_query(conn, "classes", "")


def test_scoped_query_returns_only_rows_for_requested_institution(conn):
    institution_a = core.create_institution(conn, "School A", "SCOPE-A")
    institution_b = core.create_institution(conn, "School B", "SCOPE-B")
    core.create_class(conn, institution_a, None, "Class A", "2026")
    core.create_class(conn, institution_b, None, "Class B", "2026")

    rows = core.scoped_query(conn, "classes", institution_a)

    assert [row["name"] for row in rows] == ["Class A"]

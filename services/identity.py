"""Database-backed school identities with tenant-bound role assignments."""
import json
import re
import sqlite3
import uuid
from werkzeug.security import check_password_hash, generate_password_hash

_ALLOWED_INSTITUTION_ROLES = frozenset({
    "Institution Admin",
    "Principal",
    "Academic Coordinator",
    "Examiner",
    "HR/Attendance Officer",
})
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def create_school_user(conn, *, email, password, institution_id, role_name, granted_by):
    """Create a tenant user; only institution-scoped roles are accepted here."""
    email = email.strip().lower() if isinstance(email, str) else ""
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise ValueError("invalid_email")
    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError("password_must_be_12_to_1024_characters")
    if not isinstance(institution_id, str) or not institution_id.strip():
        raise ValueError("active_institution_required")
    if not isinstance(role_name, str) or role_name not in _ALLOWED_INSTITUTION_ROLES:
        raise ValueError("unsupported_institution_role")
    institution = conn.execute(
        "SELECT id FROM institutions WHERE id = ? AND status = 'active'",
        (institution_id,),
    ).fetchone()
    if institution is None:
        raise ValueError("active_institution_required")
    existing_email = conn.execute(
        "SELECT id FROM users WHERE lower(email) = ? LIMIT 1",
        (email,),
    ).fetchone()
    if existing_email is not None:
        raise ValueError("email_already_exists")
    role = conn.execute(
        "SELECT id, scope_level FROM roles WHERE name = ?",
        (role_name,),
    ).fetchone()
    if role is None or role["scope_level"] != "institution":
        raise ValueError("institution_role_not_configured")
    user_id = uuid.uuid4().hex
    password_hash = generate_password_hash(password, method="scrypt")
    try:
        conn.execute(
            "INSERT INTO users (id, institution_id, email, status, password_hash) "
            "VALUES (?, ?, ?, 'active', ?)",
            (user_id, institution_id, email, password_hash),
        )
        conn.execute(
            "INSERT INTO role_assignments "
            "(id, user_id, role_id, scope_type, scope_id, granted_by) "
            "VALUES (?, ?, ?, 'institution', ?, ?)",
            (uuid.uuid4().hex, user_id, role["id"], institution_id, granted_by),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("email_already_exists") from None
    except Exception:
        conn.rollback()
        raise
    return {"id": user_id, "email": email, "institution_id": institution_id, "role": role_name}


def authenticate_school_user(conn, *, email, password):
    """Authenticate only active users with a valid institution-scoped assignment."""
    email = email.strip().lower() if isinstance(email, str) else ""
    if not email or not isinstance(password, str) or not password:
        return None
    rows = conn.execute(
        """
        SELECT u.id, u.email, u.institution_id, u.password_hash
        FROM users u
        JOIN institutions i ON i.id = u.institution_id AND i.status = 'active'
        WHERE lower(u.email) = ? AND u.status = 'active'
        LIMIT 2
        """,
        (email,),
    ).fetchall()
    # Fail closed if a legacy database contains case-variant duplicate emails.
    if len(rows) != 1:
        return None
    row = rows[0]
    if not row["password_hash"]:
        return None
    try:
        valid = check_password_hash(row["password_hash"], password)
    except (ValueError, TypeError):
        return None
    if not valid:
        return None
    assignment = conn.execute(
        """
        SELECT r.name, r.scope_level
        FROM role_assignments ra JOIN roles r ON r.id = ra.role_id
        WHERE ra.user_id = ? AND ra.scope_type = 'institution'
          AND ra.scope_id = ? AND r.scope_level = 'institution'
        ORDER BY r.name LIMIT 1
        """,
        (row["id"], row["institution_id"]),
    ).fetchone()
    if assignment is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "institution_id": row["institution_id"],
        "role_name": assignment["name"],
    }


def active_tenant_identity(conn, *, user_id, institution_id):
    """Revalidate session tenant binding against persistent state on every request."""
    if not user_id or not institution_id:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM users u JOIN institutions i ON i.id = u.institution_id
        JOIN role_assignments ra ON ra.user_id = u.id
        JOIN roles r ON r.id = ra.role_id
        WHERE u.id = ? AND u.institution_id = ? AND u.status = 'active'
          AND i.status = 'active' AND ra.scope_type = 'institution'
          AND ra.scope_id = u.institution_id AND r.scope_level = 'institution'
        LIMIT 1
        """,
        (user_id, institution_id),
    ).fetchone()
    return row is not None



def institution_permissions(conn, *, user_id, institution_id):
    """Resolve only permissions explicitly assigned at this exact institution scope."""
    rows = conn.execute(
        """
        SELECT r.permissions
        FROM role_assignments ra JOIN roles r ON r.id = ra.role_id
        WHERE ra.user_id = ? AND ra.scope_type = 'institution'
          AND ra.scope_id = ? AND r.scope_level = 'institution'
        """,
        (user_id, institution_id),
    ).fetchall()
    permissions = set()
    for row in rows:
        try:
            values = json.loads(row["permissions"])
        except (TypeError, ValueError):
            continue
        if isinstance(values, list):
            permissions.update(value for value in values if isinstance(value, str))
    return permissions

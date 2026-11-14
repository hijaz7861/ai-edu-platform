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
    "Teacher Portal",
})
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def create_school_user(conn, *, email, password, institution_id, role_name, granted_by, teacher_id=None):
    """Create an institution user; Teacher Portal accounts require one active teacher link."""
    email = email.strip().lower() if isinstance(email, str) else ""
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise ValueError("invalid_email")
    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError("password_must_be_12_to_1024_characters")
    if not isinstance(institution_id, str) or not institution_id.strip():
        raise ValueError("active_institution_required")
    if not isinstance(role_name, str) or role_name not in _ALLOWED_INSTITUTION_ROLES:
        raise ValueError("unsupported_institution_role")
    if role_name == "Teacher Portal":
        if not isinstance(teacher_id, str) or not teacher_id.strip() or len(teacher_id) > 128:
            raise ValueError("teacher_id_required")
        teacher_id = teacher_id.strip()
    elif teacher_id is not None:
        raise ValueError("teacher_link_only_for_teacher_portal")
    if conn.in_transaction:
        raise ValueError("identity_creation_requires_clean_connection")

    conn.execute("BEGIN IMMEDIATE")
    try:
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

        if role_name == "Teacher Portal":
            teacher = conn.execute(
                """SELECT id FROM teachers
                   WHERE id = ? AND institution_id = ? AND status = 'active'
                     AND user_id IS NULL""",
                (teacher_id, institution_id),
            ).fetchone()
            existing_link = conn.execute(
                "SELECT user_id FROM teacher_identity_links WHERE teacher_id = ?",
                (teacher_id,),
            ).fetchone()
            if teacher is None or existing_link is not None:
                raise ValueError("teacher_identity_unavailable")

        user_id = uuid.uuid4().hex
        password_hash = generate_password_hash(password, method="scrypt")
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
        if role_name == "Teacher Portal":
            linked = conn.execute(
                """UPDATE teachers SET user_id = ?
                   WHERE id = ? AND institution_id = ? AND status = 'active'
                     AND user_id IS NULL""",
                (user_id, teacher_id, institution_id),
            )
            if linked.rowcount != 1:
                raise ValueError("teacher_identity_unavailable")
            conn.execute(
                """INSERT INTO teacher_identity_links
                   (user_id, teacher_id, institution_id)
                   VALUES (?, ?, ?)""",
                (user_id, teacher_id, institution_id),
            )
        # Provisioning is security-relevant and must commit atomically with
        # the user, role assignment, and optional teacher link.
        conn.execute(
            """INSERT INTO audit_log
               (id, institution_id, actor_id, action_type, entity_type,
                entity_id, after_state)
               VALUES (?, ?, ?, 'school_user_created', 'users', ?, ?)""",
            (
                uuid.uuid4().hex,
                institution_id,
                str(granted_by or ""),
                user_id,
                json.dumps(
                    {"role": role_name, "teacher_linked": role_name == "Teacher Portal"},
                    sort_keys=True,
                ),
            ),
        )
        conn.commit()
    except ValueError:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if "teacher_identity_links" in str(exc) or "teacher_identity_scope" in str(exc):
            raise ValueError("teacher_identity_unavailable") from None
        if "school_user_audit_unavailable" in str(exc):
            raise RuntimeError("school_user_audit_failed") from None
        raise ValueError("email_already_exists") from None
    except Exception:
        conn.rollback()
        raise
    return {
        "id": user_id,
        "email": email,
        "institution_id": institution_id,
        "role": role_name,
        **({"teacher_id": teacher_id} if role_name == "Teacher Portal" else {}),
    }


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
        ORDER BY r.name LIMIT 2
        """,
        (row["id"], row["institution_id"]),
    ).fetchall()
    if not assignment:
        return None
    if any(item["name"] == "Teacher Portal" for item in assignment) and len(assignment) != 1:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "institution_id": row["institution_id"],
        "role_name": assignment[0]["name"],
    }


def active_tenant_identity(conn, *, user_id, institution_id):
    """Revalidate tenant identity and prevent Teacher Portal role escalation."""
    if not user_id or not institution_id:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM users u
        JOIN institutions i ON i.id = u.institution_id
        WHERE u.id = ? AND u.institution_id = ? AND u.status = 'active'
          AND i.status = 'active'
          AND EXISTS (
              SELECT 1
              FROM role_assignments ra
              JOIN roles r ON r.id = ra.role_id
              WHERE ra.user_id = u.id
                AND ra.scope_type = 'institution'
                AND ra.scope_id = u.institution_id
                AND r.scope_level = 'institution'
          )
          AND (
              NOT EXISTS (
                  SELECT 1
                  FROM role_assignments teacher_ra
                  JOIN roles teacher_role ON teacher_role.id = teacher_ra.role_id
                  WHERE teacher_ra.user_id = u.id
                    AND teacher_ra.scope_type = 'institution'
                    AND teacher_ra.scope_id = u.institution_id
                    AND teacher_role.scope_level = 'institution'
                    AND teacher_role.name = 'Teacher Portal'
              )
              OR (
                  (SELECT COUNT(*)
                   FROM role_assignments scoped_ra
                   JOIN roles scoped_role ON scoped_role.id = scoped_ra.role_id
                   WHERE scoped_ra.user_id = u.id
                     AND scoped_ra.scope_type = 'institution'
                     AND scoped_ra.scope_id = u.institution_id
                     AND scoped_role.scope_level = 'institution') = 1
                  AND EXISTS (
                      SELECT 1
                      FROM teacher_identity_links l
                      JOIN teachers t ON t.id = l.teacher_id
                      WHERE l.user_id = u.id
                        AND l.institution_id = u.institution_id
                        AND t.user_id = u.id
                        AND t.institution_id = u.institution_id
                        AND t.status = 'active'
                  )
              )
          )
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


def linked_teacher_for_school_user(conn, *, user_id, institution_id):
    """Return only the active teacher linked to a single-role Teacher Portal identity."""
    if not user_id or not institution_id:
        return None
    row = conn.execute(
        """
        SELECT t.id, t.employee_code, t.department_id
        FROM teacher_identity_links l
        JOIN users u ON u.id = l.user_id
        JOIN institutions i ON i.id = l.institution_id
        JOIN teachers t ON t.id = l.teacher_id
        JOIN role_assignments ra
          ON ra.user_id = u.id AND ra.scope_type = 'institution'
         AND ra.scope_id = l.institution_id
        JOIN roles r ON r.id = ra.role_id
        WHERE l.user_id = ? AND l.institution_id = ?
          AND u.institution_id = l.institution_id AND u.status = 'active'
          AND i.status = 'active'
          AND t.institution_id = l.institution_id AND t.status = 'active'
          AND t.user_id = u.id
          AND r.name = 'Teacher Portal' AND r.scope_level = 'institution'
          AND (SELECT COUNT(*) FROM role_assignments extra
               WHERE extra.user_id = u.id
                 AND extra.scope_type = 'institution'
                 AND extra.scope_id = l.institution_id) = 1
        LIMIT 1
        """,
        (user_id, institution_id),
    ).fetchone()
    return dict(row) if row is not None else None

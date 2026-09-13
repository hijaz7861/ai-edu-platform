"""
Phase 1: Tenancy & RBAC — real, runnable logic (no external deps).
"""
import uuid
import json
import datetime


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------- Tenancy CRUD ----------

def create_institution(conn, name, code, type_="school"):
    iid = new_id()
    conn.execute(
        "INSERT INTO institutions (id, name, code, type, status) VALUES (?, ?, ?, ?, 'active')",
        (iid, name, code, type_),
    )
    conn.commit()
    return iid


def create_campus(conn, institution_id, name, code, timezone="UTC"):
    cid = new_id()
    conn.execute(
        "INSERT INTO campuses (id, institution_id, name, code, timezone, status) VALUES (?, ?, ?, ?, ?, 'active')",
        (cid, institution_id, name, code, timezone),
    )
    conn.commit()
    return cid


def create_class(conn, institution_id, campus_id, name, academic_year, department_id=None):
    clid = new_id()
    conn.execute(
        "INSERT INTO classes (id, institution_id, campus_id, department_id, name, academic_year, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active')",
        (clid, institution_id, campus_id, department_id, name, academic_year),
    )
    conn.commit()
    return clid


def create_section(conn, class_id, name, capacity=40):
    sid = new_id()
    conn.execute(
        "INSERT INTO sections (id, class_id, name, capacity) VALUES (?, ?, ?, ?)",
        (sid, class_id, name, capacity),
    )
    conn.commit()
    return sid


def create_user(conn, institution_id, email):
    uid = new_id()
    conn.execute(
        "INSERT INTO users (id, institution_id, email, status) VALUES (?, ?, ?, 'active')",
        (uid, institution_id, email),
    )
    conn.commit()
    return uid


def create_teacher(conn, user_id, institution_id, employee_code, department_id=None):
    tid = new_id()
    conn.execute(
        "INSERT INTO teachers (id, user_id, institution_id, department_id, employee_code, status) "
        "VALUES (?, ?, ?, ?, ?, 'active')",
        (tid, user_id, institution_id, department_id, employee_code),
    )
    conn.commit()
    return tid


def create_student(conn, user_id, institution_id, class_id, section_id, enrollment_code):
    sid = new_id()
    conn.execute(
        "INSERT INTO students (id, user_id, institution_id, class_id, section_id, enrollment_code, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active')",
        (sid, user_id, institution_id, class_id, section_id, enrollment_code),
    )
    conn.commit()
    return sid


# ---------- RBAC ----------

DEFAULT_ROLES = [
    ("Super Admin", "platform", ["*"]),
    ("Institution Admin", "institution", ["manage_institution", "manage_users", "view_all_reports"]),
    ("Principal", "institution", ["approve_leave", "view_all_reports", "publish_exam"]),
    ("Academic Coordinator", "institution", ["manage_curriculum", "view_reports"]),
    ("Department Head", "department", ["approve_leave", "manage_department"]),
    ("Teacher", "class", ["mark_attendance", "generate_exam", "manage_own_classes"]),
    ("Student", "class", ["view_own_records"]),
    ("Parent", "class", ["view_child_records"]),
    ("Examiner", "institution", ["verify_questions", "publish_exam"]),
    ("HR/Attendance Officer", "institution", ["manage_attendance_policy", "approve_leave"]),
]


def seed_default_roles(conn):
    role_ids = {}
    for name, scope_level, perms in DEFAULT_ROLES:
        existing = conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()
        if existing:
            role_ids[name] = existing["id"]
            continue
        rid = new_id()
        conn.execute(
            "INSERT INTO roles (id, name, scope_level, permissions) VALUES (?, ?, ?, ?)",
            (rid, name, scope_level, json.dumps(perms)),
        )
        role_ids[name] = rid
    conn.commit()
    return role_ids


def assign_role(conn, user_id, role_id, scope_type, scope_id, granted_by=None):
    aid = new_id()
    conn.execute(
        "INSERT INTO role_assignments (id, user_id, role_id, scope_type, scope_id, granted_by, granted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (aid, user_id, role_id, scope_type, scope_id, granted_by, now()),
    )
    conn.commit()
    return aid


class PermissionDenied(Exception):
    pass


def user_permissions_in_scope(conn, user_id, scope_type, scope_id):
    """Real permission resolution: union of permissions from every role assignment
    the user holds that matches this exact scope, or a platform-level assignment."""
    rows = conn.execute(
        """
        SELECT r.permissions, ra.scope_type, ra.scope_id
        FROM role_assignments ra
        JOIN roles r ON r.id = ra.role_id
        WHERE ra.user_id = ?
          AND (
                (ra.scope_type = ? AND ra.scope_id = ?)
                OR ra.scope_type = 'platform'
              )
        """,
        (user_id, scope_type, scope_id),
    ).fetchall()
    perms = set()
    for row in rows:
        perms.update(json.loads(row["permissions"]))
    return perms


def require_permission(conn, user_id, permission, scope_type, scope_id):
    perms = user_permissions_in_scope(conn, user_id, scope_type, scope_id)
    if "*" in perms or permission in perms:
        return True
    raise PermissionDenied(
        f"user {user_id} lacks '{permission}' at {scope_type}:{scope_id}"
    )


# ---------- Tenant isolation helper ----------

def scoped_query(conn, table, institution_id, extra_where="", params=()):
    """Every cross-tenant read should go through this, not raw SELECTs, so isolation
    is enforced in one place instead of hoped-for at every call site."""
    sql = f"SELECT * FROM {table} WHERE institution_id = ?"
    if extra_where:
        sql += f" AND {extra_where}"
    return conn.execute(sql, (institution_id, *params)).fetchall()

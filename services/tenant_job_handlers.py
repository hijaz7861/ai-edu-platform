"""Reviewed read-only handlers for the initial tenant worker."""
from services.tenant_jobs import PermanentJobError, RESOURCE_TABLES


def tenant_health_check(conn, context):
    institution = conn.execute(
        "SELECT status FROM institutions WHERE id = ?", (context.institution_id,)
    ).fetchone()
    if institution is None or institution["status"] != "active":
        raise PermanentJobError()
    counts = {}
    for resource in context.payload["resources"]:
        table = RESOURCE_TABLES[resource]
        row = conn.execute(
            f"SELECT COUNT(*) AS row_count FROM {table} WHERE institution_id = ?",
            (context.institution_id,),
        ).fetchone()
        counts[resource] = row["row_count"]
    return {"institution_status": "active", "resource_counts": counts}


HANDLERS = {"tenant_health_check": tenant_health_check}

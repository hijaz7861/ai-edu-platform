"""
Phase 9 (agents, approval gates, self-improvement) + Phase 10 (audit, provider
registry, retention) combined — both are cross-cutting governance concerns
over the same tables.
"""
import json
import datetime
from services.core import new_id, now


# ---------- Phase 9: Agents & Approval Gates ----------

def register_agent(conn, agent_type, provider_ref=None, institution_id=None, enabled=True, config=None):
    aid = new_id()
    conn.execute(
        "INSERT INTO agents (id, agent_type, provider_ref, institution_id, enabled, config) VALUES (?, ?, ?, ?, ?, ?)",
        (aid, agent_type, provider_ref, institution_id, int(enabled), json.dumps(config or {})),
    )
    conn.commit()
    return aid


def log_agent_action(conn, agent_id, action_type, input_ref, output_ref, knowledge_sources_used=None, status="completed"):
    aid = new_id()
    conn.execute(
        """INSERT INTO agent_actions
           (id, agent_id, action_type, input_ref, output_ref, knowledge_sources_used, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (aid, agent_id, action_type, json.dumps(input_ref), json.dumps(output_ref),
         json.dumps(knowledge_sources_used or []), status, now()),
    )
    conn.commit()
    return aid


class NotApproved(Exception):
    pass


def request_approval(conn, action_type, payload: dict, explanation: str, requested_by=None, requested_by_agent_id=None):
    if not explanation or not explanation.strip():
        raise ValueError("explanation is required before requesting approval (§31)")
    gid = new_id()
    conn.execute(
        """INSERT INTO approval_gates
           (id, action_type, requested_by, requested_by_agent_id, payload, explanation, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (gid, action_type, requested_by, requested_by_agent_id, json.dumps(payload), explanation, now()),
    )
    conn.commit()
    return gid


def decide_approval(conn, gate_id, approver_id, decision):
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved or rejected")
    conn.execute(
        "UPDATE approval_gates SET status = ?, approver_id = ?, decided_at = ? WHERE id = ?",
        (decision, approver_id, now(), gate_id),
    )
    conn.commit()
    return decision


def execute_gated_action(conn, gate_id, action_fn):
    """action_fn only runs if the gate is genuinely approved — this is the actual
    enforcement mechanism, not a comment saying 'approval required'."""
    gate = conn.execute("SELECT * FROM approval_gates WHERE id = ?", (gate_id,)).fetchone()
    if gate is None:
        raise ValueError("approval gate not found")
    if gate["status"] != "approved":
        raise NotApproved(f"gate {gate_id} is '{gate['status']}', not approved — action blocked")
    return action_fn()


def propose_improvement(conn, proposal_type, source_agent_id, related_entity_type, related_entity_id,
                         description, suggested_action, requires_approval_gate=None):
    pid = new_id()
    conn.execute(
        """INSERT INTO improvement_proposals
           (id, proposal_type, source_agent_id, related_entity_type, related_entity_id, description,
            suggested_action, approval_gate_id, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
        (pid, proposal_type, source_agent_id, related_entity_type, related_entity_id, description,
         json.dumps(suggested_action), requires_approval_gate, now()),
    )
    conn.commit()
    return pid


# ---------- Phase 10: Audit, Providers, Retention ----------

def write_audit(conn, institution_id, actor_id, action_type, entity_type, entity_id,
                 before_state=None, after_state=None, actor_agent_id=None):
    # Hard rule: never let credential-looking keys reach the log
    def _redact(state):
        if not state:
            return None
        redacted = dict(state)
        for key in list(redacted.keys()):
            if any(s in key.lower() for s in ("password", "token", "secret", "api_key")):
                redacted[key] = "***REDACTED***"
        return json.dumps(redacted)

    aid = new_id()
    conn.execute(
        """INSERT INTO audit_log
           (id, institution_id, actor_id, actor_agent_id, action_type, entity_type, entity_id,
            before_state, after_state, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (aid, institution_id, actor_id, actor_agent_id, action_type, entity_type, entity_id,
         _redact(before_state), _redact(after_state), now()),
    )
    conn.commit()
    return aid


def register_provider(conn, provider_name, provider_type, capabilities, priority=100, institution_id=None):
    pid = new_id()
    conn.execute(
        """INSERT INTO ai_provider_configs
           (id, provider_name, provider_type, capabilities, institution_id, priority, enabled, status)
           VALUES (?, ?, ?, ?, ?, ?, 1, 'available')""",
        (pid, provider_name, provider_type, json.dumps(capabilities), institution_id, priority),
    )
    conn.commit()
    return pid


def set_provider_status(conn, provider_id, status):
    conn.execute(
        "UPDATE ai_provider_configs SET status = ?, last_health_check_at = ? WHERE id = ?",
        (status, now(), provider_id),
    )
    conn.commit()


def select_provider(conn, capability, institution_id=None):
    """Real fallback: picks the enabled, available, capability-matching provider
    with the lowest priority number. If the top choice is degraded/unavailable,
    it's skipped — this is what makes 'AI providers are pluggable' concrete."""
    rows = conn.execute(
        "SELECT * FROM ai_provider_configs WHERE enabled = 1 ORDER BY priority ASC"
    ).fetchall()
    for row in rows:
        if row["status"] != "available":
            continue
        if capability in json.loads(row["capabilities"]):
            return row
    return None


def enforce_notification_retention(conn, institution_id, retention_days, as_of=None):
    """Real deletion based on real timestamps — not a no-op placeholder."""
    cutoff = (as_of or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=retention_days)
    cutoff_str = cutoff.isoformat()
    to_delete = conn.execute(
        "SELECT id FROM notifications WHERE created_at < ?", (cutoff_str,)
    ).fetchall()
    conn.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff_str,))
    conn.commit()
    return [r["id"] for r in to_delete]

"""
Database layer for the AI Education Platform reference implementation.
SQLite + stdlib only — no network required to run this.
"""
import sqlite3
import os

DB_PATH = os.environ.get("AI_EDU_DB", os.path.join(os.path.dirname(__file__), "storage", "platform.db"))

SCHEMA = """
PRAGMA foreign_keys = ON;

-- Phase 1: Tenancy & RBAC
CREATE TABLE IF NOT EXISTS institutions (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL,
    type TEXT, status TEXT DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS campuses (
    id TEXT PRIMARY KEY, institution_id TEXT NOT NULL REFERENCES institutions(id),
    name TEXT NOT NULL, code TEXT, address TEXT, timezone TEXT, status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS departments (
    id TEXT PRIMARY KEY, institution_id TEXT NOT NULL REFERENCES institutions(id),
    campus_id TEXT REFERENCES campuses(id), name TEXT NOT NULL, code TEXT
);
CREATE TABLE IF NOT EXISTS classes (
    id TEXT PRIMARY KEY, institution_id TEXT NOT NULL REFERENCES institutions(id),
    campus_id TEXT REFERENCES campuses(id), department_id TEXT REFERENCES departments(id),
    name TEXT NOT NULL, academic_year TEXT, status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY, class_id TEXT NOT NULL REFERENCES classes(id),
    name TEXT NOT NULL, capacity INTEGER, homeroom_teacher_id TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, institution_id TEXT REFERENCES institutions(id),
    email TEXT UNIQUE NOT NULL, status TEXT DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, scope_level TEXT NOT NULL, permissions TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS role_assignments (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), role_id TEXT NOT NULL REFERENCES roles(id),
    scope_type TEXT NOT NULL, scope_id TEXT NOT NULL, granted_by TEXT, granted_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS teachers (
    id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), institution_id TEXT NOT NULL REFERENCES institutions(id),
    department_id TEXT REFERENCES departments(id), employee_code TEXT, status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS students (
    id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), institution_id TEXT NOT NULL REFERENCES institutions(id),
    class_id TEXT REFERENCES classes(id), section_id TEXT REFERENCES sections(id),
    enrollment_code TEXT, status TEXT DEFAULT 'active'
);

-- Phase 2: Teacher Attendance
CREATE TABLE IF NOT EXISTS attendance_method_configs (
    id TEXT PRIMARY KEY, institution_id TEXT, method TEXT NOT NULL, enabled INTEGER DEFAULT 1, config TEXT
);
CREATE TABLE IF NOT EXISTS attendance_policies (
    id TEXT PRIMARY KEY, institution_id TEXT, campus_id TEXT, department_id TEXT, name TEXT,
    working_hours_start TEXT, working_hours_end TEXT, grace_period_minutes INTEGER DEFAULT 0,
    late_threshold_minutes INTEGER DEFAULT 0, early_departure_threshold_minutes INTEGER DEFAULT 0,
    missing_checkout_policy TEXT DEFAULT 'flag_for_review', version INTEGER DEFAULT 1,
    effective_from TEXT, effective_to TEXT, created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS attendance_events (
    id TEXT PRIMARY KEY, teacher_id TEXT NOT NULL REFERENCES teachers(id), institution_id TEXT,
    event_type TEXT NOT NULL, timestamp TEXT NOT NULL, method TEXT, policy_id TEXT REFERENCES attendance_policies(id),
    status TEXT, session_ref TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS attendance_correction_requests (
    id TEXT PRIMARY KEY, attendance_event_id TEXT REFERENCES attendance_events(id), teacher_id TEXT NOT NULL,
    requested_change TEXT, reason TEXT, status TEXT DEFAULT 'pending', reviewed_by TEXT, reviewed_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS leave_types (
    id TEXT PRIMARY KEY, institution_id TEXT, name TEXT NOT NULL, default_balance_per_year INTEGER,
    requires_approval INTEGER DEFAULT 1, multi_level_approval INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leave_requests (
    id TEXT PRIMARY KEY, teacher_id TEXT NOT NULL, leave_type_id TEXT NOT NULL REFERENCES leave_types(id),
    start_date TEXT, end_date TEXT, reason TEXT, status TEXT DEFAULT 'pending',
    substitute_teacher_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS leave_approvals (
    id TEXT PRIMARY KEY, leave_request_id TEXT NOT NULL REFERENCES leave_requests(id), approver_id TEXT,
    level INTEGER DEFAULT 1, decision TEXT DEFAULT 'pending', decided_at TEXT, comments TEXT
);
CREATE TABLE IF NOT EXISTS leave_balances (
    id TEXT PRIMARY KEY, teacher_id TEXT NOT NULL, leave_type_id TEXT NOT NULL,
    academic_year TEXT, allocated INTEGER DEFAULT 0, used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY, institution_id TEXT, actor_id TEXT, actor_agent_id TEXT, action_type TEXT NOT NULL,
    entity_type TEXT, entity_id TEXT, before_state TEXT, after_state TEXT, ip_address TEXT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Phase 3: Document Ingestion
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY, institution_id TEXT, title TEXT, author TEXT, publisher TEXT, edition TEXT,
    class_id TEXT, subject_id TEXT, language TEXT, status TEXT DEFAULT 'uploaded',
    created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS book_files (
    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id), file_type TEXT,
    storage_ref TEXT, original_filename TEXT, checksum TEXT, validation_status TEXT DEFAULT 'pending',
    uploaded_by TEXT, uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS book_pages (
    id TEXT PRIMARY KEY, book_file_id TEXT NOT NULL REFERENCES book_files(id), page_number INTEGER,
    image_ref TEXT, raw_text TEXT, language_detected TEXT, is_blank INTEGER DEFAULT 0, status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS ocr_results (
    id TEXT PRIMARY KEY, book_page_id TEXT NOT NULL REFERENCES book_pages(id), engine TEXT,
    raw_text TEXT, confidence_score REAL, flags TEXT, processed_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS document_structure_elements (
    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id), book_page_id TEXT,
    element_type TEXT NOT NULL, parent_element_id TEXT, content TEXT, position_ref TEXT,
    confidence REAL, page_start INTEGER, page_end INTEGER
);
CREATE TABLE IF NOT EXISTS processing_jobs (
    id TEXT PRIMARY KEY, book_id TEXT REFERENCES books(id), job_type TEXT NOT NULL, status TEXT DEFAULT 'queued',
    progress_percent INTEGER DEFAULT 0, current_stage TEXT, checkpoint_data TEXT, retry_count INTEGER DEFAULT 0,
    error_detail TEXT, started_at TEXT, completed_at TEXT
);

-- Phase 4: Knowledge Base
CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id), source_element_id TEXT,
    title TEXT, order_index INTEGER, page_start INTEGER, page_end INTEGER
);
CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL REFERENCES chapters(id), parent_topic_id TEXT,
    source_element_id TEXT, title TEXT, order_index INTEGER
);
CREATE TABLE IF NOT EXISTS concepts (
    id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id), book_id TEXT,
    name TEXT, concept_type TEXT, description TEXT, difficulty_level TEXT,
    verification_status TEXT DEFAULT 'ai_generated', source_element_id TEXT, page_ref INTEGER
);
CREATE TABLE IF NOT EXISTS concept_relationships (
    id TEXT PRIMARY KEY, from_concept_id TEXT NOT NULL REFERENCES concepts(id),
    to_concept_id TEXT NOT NULL REFERENCES concepts(id), relationship_type TEXT NOT NULL, confidence REAL
);
CREATE TABLE IF NOT EXISTS learning_objectives (
    id TEXT PRIMARY KEY, chapter_id TEXT, topic_id TEXT, book_id TEXT,
    description TEXT, bloom_level TEXT, source_element_id TEXT
);

-- Phase 5: Question Bank
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY, book_id TEXT, concept_id TEXT REFERENCES concepts(id), topic_id TEXT, chapter_id TEXT,
    class_id TEXT, subject_id TEXT, question_type TEXT NOT NULL, content TEXT NOT NULL,
    difficulty TEXT, learning_objective_id TEXT, language TEXT, marks REAL DEFAULT 1,
    version INTEGER DEFAULT 1, verification_status TEXT DEFAULT 'draft',
    created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS question_options (
    id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES questions(id), content TEXT,
    is_correct INTEGER DEFAULT 0, order_index INTEGER, match_target TEXT
);
CREATE TABLE IF NOT EXISTS question_answers (
    id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES questions(id), answer_content TEXT, explanation TEXT
);
CREATE TABLE IF NOT EXISTS question_sources (
    id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES questions(id), book_id TEXT,
    chapter_id TEXT, topic_id TEXT, concept_id TEXT, page_ref INTEGER,
    source_status TEXT DEFAULT 'unavailable', confidence REAL
);
CREATE TABLE IF NOT EXISTS question_duplicate_flags (
    id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES questions(id), duplicate_of_question_id TEXT,
    similarity_score REAL, flag_type TEXT, status TEXT DEFAULT 'pending_review', detected_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS question_verification_results (
    id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES questions(id), check_type TEXT NOT NULL,
    result TEXT NOT NULL, detail TEXT, checked_at TEXT DEFAULT CURRENT_TIMESTAMP, checked_by TEXT
);
CREATE TABLE IF NOT EXISTS question_banks (
    id TEXT PRIMARY KEY, institution_id TEXT, name TEXT, class_id TEXT, subject_id TEXT,
    book_id TEXT, status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS question_bank_items (
    id TEXT PRIMARY KEY, question_bank_id TEXT NOT NULL REFERENCES question_banks(id),
    question_id TEXT NOT NULL REFERENCES questions(id), added_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Phase 6: Exam Generator
CREATE TABLE IF NOT EXISTS exam_blueprints (
    id TEXT PRIMARY KEY, institution_id TEXT, class_id TEXT, subject_id TEXT, book_id TEXT,
    total_marks REAL, duration_minutes INTEGER, language TEXT, topic_coverage TEXT,
    difficulty_distribution TEXT, question_type_distribution TEXT, learning_objective_coverage TEXT,
    status TEXT DEFAULT 'draft', created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS exams (
    id TEXT PRIMARY KEY, blueprint_id TEXT NOT NULL REFERENCES exam_blueprints(id), version_group_id TEXT,
    version_label TEXT, title TEXT, total_marks REAL, duration_minutes INTEGER, status TEXT DEFAULT 'draft',
    language TEXT, created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS exam_questions (
    id TEXT PRIMARY KEY, exam_id TEXT NOT NULL REFERENCES exams(id), question_id TEXT NOT NULL REFERENCES questions(id),
    order_index INTEGER, marks_allocated REAL, section_label TEXT
);
CREATE TABLE IF NOT EXISTS answer_keys (
    id TEXT PRIMARY KEY, exam_id TEXT NOT NULL REFERENCES exams(id), content TEXT, generated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS marking_schemes (
    id TEXT PRIMARY KEY, exam_id TEXT NOT NULL REFERENCES exams(id), content TEXT, generated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Phase 7: Performance & Reporting
CREATE TABLE IF NOT EXISTS student_attendance (
    id TEXT PRIMARY KEY, student_id TEXT NOT NULL, class_id TEXT, section_id TEXT, date TEXT,
    status TEXT, method TEXT, recorded_by TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS student_exam_submissions (
    id TEXT PRIMARY KEY, student_id TEXT NOT NULL, exam_id TEXT NOT NULL REFERENCES exams(id),
    submitted_at TEXT DEFAULT CURRENT_TIMESTAMP, total_marks_obtained REAL, status TEXT DEFAULT 'submitted'
);
CREATE TABLE IF NOT EXISTS student_exam_answers (
    id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES student_exam_submissions(id),
    exam_question_id TEXT NOT NULL REFERENCES exam_questions(id), answer_content TEXT,
    marks_obtained REAL, is_correct INTEGER, graded_by TEXT
);
CREATE TABLE IF NOT EXISTS student_performance_records (
    id TEXT PRIMARY KEY, student_id TEXT NOT NULL, subject_id TEXT, period TEXT,
    avg_marks REAL, attendance_percent REAL, topic_mastery TEXT, weak_areas TEXT, strong_areas TEXT,
    trend TEXT, generated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Phase 8: Notifications, Voice, Search
CREATE TABLE IF NOT EXISTS notification_rules (
    id TEXT PRIMARY KEY, institution_id TEXT, event_type TEXT NOT NULL, enabled INTEGER DEFAULT 1,
    channels TEXT, recipient_rule TEXT, template_id TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY, rule_id TEXT, recipient_id TEXT, event_type TEXT, channel TEXT, content TEXT,
    related_entity_type TEXT, related_entity_id TEXT, status TEXT DEFAULT 'queued',
    sent_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS voice_command_logs (
    id TEXT PRIMARY KEY, user_id TEXT, raw_input TEXT, detected_language TEXT, parsed_intent TEXT,
    parsed_parameters TEXT, resulting_action_ref TEXT, status TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Phase 9: Agents, Approval, Self-Improvement
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, agent_type TEXT NOT NULL, provider_ref TEXT, institution_id TEXT,
    enabled INTEGER DEFAULT 1, config TEXT
);
CREATE TABLE IF NOT EXISTS agent_actions (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), action_type TEXT,
    input_ref TEXT, output_ref TEXT, knowledge_sources_used TEXT, status TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS approval_gates (
    id TEXT PRIMARY KEY, action_type TEXT NOT NULL, requested_by TEXT, requested_by_agent_id TEXT,
    payload TEXT, explanation TEXT NOT NULL, status TEXT DEFAULT 'pending',
    approver_id TEXT, decided_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS improvement_proposals (
    id TEXT PRIMARY KEY, proposal_type TEXT, source_agent_id TEXT, related_entity_type TEXT,
    related_entity_id TEXT, description TEXT, suggested_action TEXT, approval_gate_id TEXT,
    status TEXT DEFAULT 'proposed', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Phase 10: Security, Privacy, Providers, Offline
CREATE TABLE IF NOT EXISTS ai_provider_configs (
    id TEXT PRIMARY KEY, provider_name TEXT NOT NULL, provider_type TEXT, capabilities TEXT,
    institution_id TEXT, priority INTEGER DEFAULT 100, enabled INTEGER DEFAULT 1,
    status TEXT DEFAULT 'available', last_health_check_at TEXT
);
CREATE TABLE IF NOT EXISTS data_retention_policies (
    id TEXT PRIMARY KEY, institution_id TEXT, entity_type TEXT NOT NULL,
    retention_period_days INTEGER, deletion_method TEXT DEFAULT 'archive', last_enforced_at TEXT
);
CREATE TABLE IF NOT EXISTS offline_sync_queue (
    id TEXT PRIMARY KEY, institution_id TEXT, entity_type TEXT, entity_id TEXT, operation TEXT,
    payload TEXT, origin TEXT, sync_status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, synced_at TEXT
);

-- Phase 11: Testing & Acceptance
CREATE TABLE IF NOT EXISTS test_runs (
    id TEXT PRIMARY KEY, test_suite TEXT, test_name TEXT, status TEXT, output_log TEXT,
    duration_ms INTEGER, related_phase TEXT, git_commit_ref TEXT, run_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS acceptance_criteria_checks (
    id TEXT PRIMARY KEY, criteria_number INTEGER, criteria_description TEXT, status TEXT DEFAULT 'not_started',
    evidence_ref TEXT, verified_by TEXT, verified_at TEXT
);
"""


def get_connection(db_path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str = None):
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


if __name__ == "__main__":
    init_db()
    print(f"Initialized schema at {DB_PATH}")

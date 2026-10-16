-- 0001_initial: core schema for the Local-First Agent.
-- Secrets policy: no passwords, cookies, OTPs, access tokens or session secrets are stored.

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pairings (
    id              TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    extension_id    TEXT,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT,
    revoked_at      TEXT
);

CREATE TABLE IF NOT EXISTS candidate_profile (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    data_json     TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approved_answers (
    id            TEXT PRIMARY KEY,
    category      TEXT NOT NULL,           -- e.g. work_authorization, sponsorship, salary, notice_period, custom
    question      TEXT NOT NULL,           -- canonical question text or pattern
    answer        TEXT NOT NULL,
    provenance    TEXT NOT NULL,           -- verified_fact | approved_answer | inferred
    confidence    REAL NOT NULL DEFAULT 1.0,
    legally_significant INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approved_answers_category ON approved_answers(category);

CREATE TABLE IF NOT EXISTS resumes (
    id            TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    mime_type     TEXT NOT NULL,
    targeting_json TEXT NOT NULL DEFAULT '{}',  -- {"keywords": [...], "titles": [...], "default": bool}
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_sources (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,            -- url | csv | manual | greenhouse | lever
    name          TEXT NOT NULL,
    config_json   TEXT NOT NULL DEFAULT '{}',
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_run_at   TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    source_id       TEXT REFERENCES job_sources(id) ON DELETE SET NULL,
    source_kind     TEXT NOT NULL,
    original_url    TEXT NOT NULL,
    canonical_url   TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    company         TEXT,
    title           TEXT,
    location        TEXT,
    remote_type     TEXT,                   -- remote | hybrid | onsite | unknown
    employment_type TEXT,
    experience_min_years REAL,
    experience_max_years REAL,
    salary_min      REAL,
    salary_max      REAL,
    salary_currency TEXT,
    description     TEXT,
    requirements_json TEXT NOT NULL DEFAULT '[]',
    apply_url       TEXT,
    requisition_id  TEXT,
    posted_at       TEXT,
    extraction_method TEXT,                 -- jsonld | api | dom | model | manual
    match_score     REAL,
    match_explanation_json TEXT,
    filter_result_json TEXT,
    duplicate_of    TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedup_key ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS idx_jobs_company_title ON jobs(company, title);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_match_score ON jobs(match_score);

CREATE TABLE IF NOT EXISTS applications (
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    identity_key    TEXT NOT NULL,          -- composite: candidate|company|requisition|apply host
    state           TEXT NOT NULL,
    resume_id       TEXT REFERENCES resumes(id) ON DELETE SET NULL,
    application_url TEXT,
    verification_status TEXT NOT NULL DEFAULT 'not_attempted',
    reference_number TEXT,
    evidence_json   TEXT,
    failure_json    TEXT,
    fill_plan_json  TEXT,
    pending_questions_json TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_attempt_at TEXT,
    applied_at      TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_identity ON applications(identity_key);
CREATE INDEX IF NOT EXISTS idx_applications_state ON applications(state);
CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_applications_next_attempt ON applications(next_attempt_at);

CREATE TABLE IF NOT EXISTS application_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    reason          TEXT,
    actor           TEXT NOT NULL DEFAULT 'agent',   -- agent | user | scheduler | system
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_app ON application_transitions(application_id, id);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,          -- apply | extract_job | navigate | read | general
    status          TEXT NOT NULL,
    instruction     TEXT,
    application_id  TEXT REFERENCES applications(id) ON DELETE SET NULL,
    job_id          TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    tab_id          INTEGER,
    domain          TEXT,
    progress_json   TEXT,
    result_json     TEXT,
    failure_json    TEXT,
    action_count    INTEGER NOT NULL DEFAULT 0,
    model_calls     INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT,
    finished_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS execution_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    action_json     TEXT NOT NULL,
    result_json     TEXT,
    ok              INTEGER,
    duration_ms     INTEGER,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON execution_attempts(task_id, seq);

CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    application_id  TEXT REFERENCES applications(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,          -- submit | high_risk_action | answer | domain
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | expired
    decided_at      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    level           TEXT NOT NULL,
    event           TEXT NOT NULL,
    task_id         TEXT,
    application_id  TEXT,
    job_id          TEXT,
    data_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_events(task_id);

CREATE TABLE IF NOT EXISTS model_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    role            TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    latency_ms      INTEGER NOT NULL,
    prompt_chars    INTEGER NOT NULL,
    output_chars    INTEGER NOT NULL,
    prompt_tokens   INTEGER,
    output_tokens   INTEGER,
    cached          INTEGER NOT NULL DEFAULT 0,
    ok              INTEGER NOT NULL,
    error           TEXT,
    task_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_metrics_ts ON model_metrics(ts);

CREATE TABLE IF NOT EXISTS scheduler_config (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    data_json       TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_counters (
    day             TEXT NOT NULL,
    counter         TEXT NOT NULL,
    value           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, counter)
);

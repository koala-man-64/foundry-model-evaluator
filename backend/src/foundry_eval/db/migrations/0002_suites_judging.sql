-- Increment 2: suites, versioned rubrics and judge evaluations.
-- All tables are STRICT. Money is stored as decimal TEXT, never REAL.

CREATE TABLE suites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    trials INTEGER NOT NULL DEFAULT 1 CHECK (trials BETWEEN 1 AND 50),
    -- Automatic judging after each suite run (frozen into the run when it is created), or NULL.
    judge_config_json TEXT,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE suite_prompts (
    id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL REFERENCES suites(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    system_text TEXT NOT NULL DEFAULT '',
    user_text TEXT NOT NULL
) STRICT;

CREATE INDEX suite_prompts_suite ON suite_prompts (suite_id, position);

CREATE TABLE suite_candidates (
    id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL REFERENCES suites(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    profile_id TEXT NOT NULL,
    label TEXT,
    settings_json TEXT NOT NULL
) STRICT;

CREATE INDEX suite_candidates_suite ON suite_candidates (suite_id, position);

CREATE TABLE rubrics (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

-- Rubric versions are immutable: editing a rubric adds a version, so evaluations always refer to
-- exactly the criteria they were scored against.
CREATE TABLE rubric_versions (
    id TEXT PRIMARY KEY,
    rubric_id TEXT NOT NULL REFERENCES rubrics(id),
    version INTEGER NOT NULL CHECK (version >= 1),
    scale_min INTEGER NOT NULL,
    scale_max INTEGER NOT NULL,
    criteria_json TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (rubric_id, version),
    CHECK (scale_min < scale_max)
) STRICT;

CREATE TRIGGER rubric_versions_immutable_update BEFORE UPDATE ON rubric_versions
BEGIN SELECT RAISE(ABORT, 'rubric versions are immutable'); END;
CREATE TRIGGER rubric_versions_immutable_delete BEFORE DELETE ON rubric_versions
BEGIN SELECT RAISE(ABORT, 'rubric versions are immutable'); END;

INSERT INTO rubrics (id, name, is_default, archived, created_at, updated_at)
VALUES ('default', 'Default rubric', 1, 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');

INSERT INTO rubric_versions (id, rubric_id, version, scale_min, scale_max, criteria_json, instructions, created_at)
VALUES (
    'default-v1', 'default', 1, 1, 5,
    '[{"key": "instruction_following", "name": "Instruction following", "weight": 1, "description": "Does the response do what the system instructions and the user prompt ask, respecting every stated constraint (format, length, scope, tone)?"}, {"key": "correctness", "name": "Correctness", "weight": 1, "description": "Are the facts, reasoning, calculations and code correct? Penalize errors and unsupported claims."}, {"key": "completeness", "name": "Completeness", "weight": 1, "description": "Does the response address every part of the request with the necessary detail?"}, {"key": "clarity", "name": "Clarity", "weight": 1, "description": "Is the response well organized, easy to follow and free of unnecessary padding?"}]',
    '',
    '2026-01-01T00:00:00Z'
);

-- One judge evaluation of the succeeded responses for one (run, prompt, trial).
CREATE TABLE evaluations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    prompt_id TEXT NOT NULL REFERENCES run_prompts(id) ON DELETE CASCADE,
    trial_index INTEGER NOT NULL CHECK (trial_index >= 1),
    batch_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'auto')),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'succeeded', 'malformed', 'failed', 'rejected_oversized',
        'cancelled', 'interrupted', 'skipped_budget')),
    judge_profile_id TEXT NOT NULL,
    judge_profile_snapshot_json TEXT NOT NULL,
    judge_settings_json TEXT NOT NULL,
    rubric_version_id TEXT NOT NULL REFERENCES rubric_versions(id),
    price_snapshot_id TEXT NOT NULL REFERENCES price_snapshots(id),
    structured_output INTEGER NOT NULL CHECK (structured_output IN (0, 1)),
    judge_is_participant INTEGER NOT NULL CHECK (judge_is_participant IN (0, 1)),
    participant_reasons_json TEXT NOT NULL DEFAULT '[]',
    -- Presentation order revealed after judging: [{"label", "attempt_id", "candidate_id"}].
    order_json TEXT NOT NULL,
    input_estimate_tokens INTEGER NOT NULL,
    input_limit_tokens INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    request_sent_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    latency_ms INTEGER,
    ttft_ms INTEGER,
    streamed INTEGER CHECK (streamed IN (0, 1)),
    http_status INTEGER,
    request_id TEXT,
    finish_reason TEXT,
    raw_finish_reason TEXT,
    response_model TEXT,
    service_tier TEXT,
    output_text TEXT,
    raw_response_json TEXT,
    raw_usage_json TEXT,
    usage_json TEXT,
    parse_errors_json TEXT NOT NULL DEFAULT '[]',
    preferred TEXT,
    preferred_candidate_id TEXT,
    preference_explanation TEXT,
    cost_json TEXT,
    cost_status TEXT CHECK (cost_status IN ('complete', 'incomplete', 'not_sent')),
    cost_total TEXT,
    cost_lower TEXT,
    cost_upper TEXT,
    error_type TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX evaluations_run ON evaluations (run_id);
CREATE INDEX evaluations_status ON evaluations (status);
CREATE INDEX evaluations_idempotency ON evaluations (idempotency_key);

CREATE TABLE evaluation_candidates (
    evaluation_id TEXT NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    position INTEGER NOT NULL,
    attempt_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    judge_overall REAL NOT NULL,
    weighted_overall REAL NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (evaluation_id, label)
) STRICT;

CREATE TABLE evaluation_criteria (
    evaluation_id TEXT NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    criterion_key TEXT NOT NULL,
    score INTEGER NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (evaluation_id, label, criterion_key)
) STRICT;

-- Why automatic judging could not start for a run (shown with the run), or NULL.
ALTER TABLE runs ADD COLUMN judge_error TEXT;

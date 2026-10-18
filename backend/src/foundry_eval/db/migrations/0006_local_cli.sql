-- Increment 6: Local CLI providers (claude_cli, codex_cli), cli route, and local_cli auth_style.
-- Rebuilds endpoints and model_profiles STRICT tables to widen CHECK constraints while preserving all rows and FKs.

CREATE TABLE endpoints_new (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    auth_style TEXT NOT NULL CHECK (auth_style IN ('api-key', 'bearer', 'local_cli')),
    credential_ref TEXT NOT NULL UNIQUE,
    key_set_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

INSERT INTO endpoints_new (
    id, name, base_url, region, auth_style, credential_ref, key_set_at, created_at, updated_at
)
SELECT
    id, name, base_url, region, auth_style, credential_ref, key_set_at, created_at, updated_at
FROM endpoints;

DROP TABLE endpoints;
ALTER TABLE endpoints_new RENAME TO endpoints;

CREATE TABLE model_profiles_new (
    id TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    api_style TEXT NOT NULL CHECK (api_style IN (
        'openai_responses', 'openai_chat', 'anthropic_messages', 'claude_cli', 'codex_cli'
    )),
    route TEXT NOT NULL CHECK (route IN ('openai_v1', 'foundry_models', 'anthropic', 'custom', 'cli')),
    custom_path TEXT,
    api_version TEXT,
    deployment TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL DEFAULT '',
    context_window INTEGER,
    max_output_tokens INTEGER,
    preset TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    deployment_type TEXT NOT NULL CHECK (deployment_type IN ('global', 'data_zone', 'regional')),
    processing_tier TEXT NOT NULL CHECK (processing_tier IN ('standard', 'priority')),
    pricing_region TEXT,
    meter_terms TEXT NOT NULL DEFAULT '',
    long_context_threshold INTEGER,
    streaming INTEGER NOT NULL DEFAULT 1 CHECK (streaming IN (0, 1)),
    include_usage INTEGER NOT NULL DEFAULT 1 CHECK (include_usage IN (0, 1)),
    timeout_connect_s REAL NOT NULL DEFAULT 15,
    timeout_read_s REAL NOT NULL DEFAULT 600,
    timeout_total_s REAL NOT NULL DEFAULT 900,
    notes TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

INSERT INTO model_profiles_new (
    id, endpoint_id, name, api_style, route, custom_path, api_version, deployment,
    publisher, model_name, model_version, context_window, max_output_tokens, preset,
    capabilities_json, deployment_type, processing_tier, pricing_region, meter_terms,
    long_context_threshold, streaming, include_usage, timeout_connect_s, timeout_read_s,
    timeout_total_s, notes, archived, created_at, updated_at
)
SELECT
    id, endpoint_id, name, api_style, route, custom_path, api_version, deployment,
    publisher, model_name, model_version, context_window, max_output_tokens, preset,
    capabilities_json, deployment_type, processing_tier, pricing_region, meter_terms,
    long_context_threshold, streaming, include_usage, timeout_connect_s, timeout_read_s,
    timeout_total_s, notes, archived, created_at, updated_at
FROM model_profiles;

DROP TABLE model_profiles;
ALTER TABLE model_profiles_new RENAME TO model_profiles;

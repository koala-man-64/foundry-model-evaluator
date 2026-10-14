-- Increment 1: configuration, pricing, runs, attempts, manual scores.
-- All tables are STRICT. Money and rates are stored as decimal TEXT, never REAL.
-- No table stores API keys: endpoints reference an opaque credential_ref in the OS credential store.

CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
) STRICT;

CREATE TABLE endpoints (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    auth_style TEXT NOT NULL CHECK (auth_style IN ('api-key', 'bearer')),
    credential_ref TEXT NOT NULL UNIQUE,
    key_set_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE model_profiles (
    id TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    api_style TEXT NOT NULL CHECK (api_style IN ('openai_responses', 'openai_chat', 'anthropic_messages')),
    route TEXT NOT NULL CHECK (route IN ('openai_v1', 'foundry_models', 'anthropic', 'custom')),
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

-- Current (editable) rate configuration per profile. Azure rows hold a meter reference; the price
-- is read from the cached catalog. Manual and override rows hold the price directly.
CREATE TABLE profile_rates (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK (category IN (
        'input_uncached', 'cache_read', 'cache_write', 'cache_write_5m', 'cache_write_1h', 'output')),
    tier_above_input INTEGER NOT NULL DEFAULT 0 CHECK (tier_above_input >= 0),
    layer TEXT NOT NULL CHECK (layer IN ('base', 'override')),
    source TEXT NOT NULL CHECK (source IN (
        'azure_auto', 'azure_pinned', 'manual', 'not_applicable', 'custom_override')),
    price TEXT,
    unit_label TEXT,
    unit_tokens INTEGER,
    currency TEXT NOT NULL DEFAULT 'USD',
    meter_id TEXT,
    sku_id TEXT,
    source_url TEXT,
    effective_date TEXT,
    note TEXT,
    declared_by TEXT CHECK (declared_by IN ('preset', 'user')),
    updated_at TEXT NOT NULL,
    UNIQUE (profile_id, category, tier_above_input, layer)
) STRICT;

-- Cache of Azure Retail Prices API items (refreshed only on explicit request).
CREATE TABLE price_catalog (
    meter_id TEXT NOT NULL,
    sku_id TEXT NOT NULL,
    region TEXT NOT NULL,
    currency TEXT NOT NULL,
    tier_min_units TEXT NOT NULL,
    effective_start TEXT NOT NULL,
    service_name TEXT NOT NULL,
    product_name TEXT NOT NULL,
    sku_name TEXT NOT NULL,
    meter_name TEXT NOT NULL,
    unit_of_measure TEXT NOT NULL,
    retail_price TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    price_type TEXT NOT NULL,
    is_primary_region INTEGER,
    raw_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (meter_id, sku_id, region, currency, tier_min_units, effective_start)
) STRICT;

CREATE INDEX price_catalog_region ON price_catalog (region, currency);

CREATE TABLE price_catalog_fetches (
    id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    currency TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    item_count INTEGER,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    error TEXT
) STRICT;

-- Immutable price snapshots: frozen copies of a profile's resolved rate card at run creation.
CREATE TABLE price_snapshots (
    id TEXT PRIMARY KEY,
    profile_id TEXT,
    created_at TEXT NOT NULL,
    currency TEXT NOT NULL,
    processing_tier TEXT NOT NULL,
    pricing_identity TEXT NOT NULL,
    context_json TEXT NOT NULL
) STRICT;

-- Rates are written before their snapshot header (same transaction); once the header exists the
-- snapshot is sealed: no rate can be added, changed or removed.
CREATE TABLE price_snapshot_rates (
    snapshot_id TEXT NOT NULL,
    category TEXT NOT NULL,
    tier_above_input INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('priced', 'not_applicable', 'unknown')),
    source TEXT,
    price TEXT,
    unit_label TEXT,
    unit_tokens INTEGER,
    price_per_million TEXT,
    currency TEXT NOT NULL,
    base_price TEXT,
    base_unit_tokens INTEGER,
    base_source TEXT,
    meter_id TEXT,
    sku_id TEXT,
    meter_name TEXT,
    product_name TEXT,
    region TEXT,
    source_url TEXT,
    effective_date TEXT,
    retrieved_at TEXT,
    note TEXT,
    declared_by TEXT,
    PRIMARY KEY (snapshot_id, category, tier_above_input)
) STRICT;

CREATE TRIGGER price_snapshots_immutable_update BEFORE UPDATE ON price_snapshots
BEGIN SELECT RAISE(ABORT, 'price snapshots are immutable'); END;
CREATE TRIGGER price_snapshots_immutable_delete BEFORE DELETE ON price_snapshots
BEGIN SELECT RAISE(ABORT, 'price snapshots are immutable'); END;
CREATE TRIGGER price_snapshot_rates_immutable_update BEFORE UPDATE ON price_snapshot_rates
BEGIN SELECT RAISE(ABORT, 'price snapshots are immutable'); END;
CREATE TRIGGER price_snapshot_rates_immutable_delete BEFORE DELETE ON price_snapshot_rates
BEGIN SELECT RAISE(ABORT, 'price snapshots are immutable'); END;
CREATE TRIGGER price_snapshot_rates_sealed BEFORE INSERT ON price_snapshot_rates
WHEN EXISTS (SELECT 1 FROM price_snapshots WHERE id = NEW.snapshot_id)
BEGIN SELECT RAISE(ABORT, 'price snapshots are immutable'); END;

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('compare', 'suite', 'smoke')),
    suite_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'completed', 'completed_with_errors', 'cancelled', 'interrupted')),
    idempotency_key TEXT NOT NULL UNIQUE,
    trials INTEGER NOT NULL DEFAULT 1 CHECK (trials >= 1),
    budget TEXT,
    judge_config_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
) STRICT;

CREATE INDEX runs_created ON runs (created_at);

CREATE TABLE run_prompts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    suite_prompt_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    system_text TEXT NOT NULL DEFAULT '',
    user_text TEXT NOT NULL,
    content_hash TEXT NOT NULL
) STRICT;

CREATE TABLE run_candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    label TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    profile_snapshot_json TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    price_snapshot_id TEXT NOT NULL REFERENCES price_snapshots(id)
) STRICT;

CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES run_candidates(id) ON DELETE CASCADE,
    prompt_id TEXT NOT NULL REFERENCES run_prompts(id) ON DELETE CASCADE,
    trial_index INTEGER NOT NULL CHECK (trial_index >= 1),
    retry_of TEXT REFERENCES attempts(id),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted', 'skipped_budget')),
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
    reasoning_summary TEXT,
    raw_response_json TEXT,
    raw_usage_json TEXT,
    usage_json TEXT,
    cost_json TEXT,
    cost_status TEXT CHECK (cost_status IN ('complete', 'incomplete', 'not_sent')),
    cost_total TEXT,
    cost_lower TEXT,
    cost_upper TEXT,
    error_type TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX attempts_run ON attempts (run_id);
CREATE INDEX attempts_status ON attempts (status);

CREATE TABLE manual_scores (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
    score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
) STRICT;

-- Review fixes.
-- The worst-case cost reserved against a run budget for each sent call. A call that was sent but
-- never reported usage (cancelled, interrupted, connection lost) counts at this amount, also after
-- a restart, so spending can never be under-counted.
ALTER TABLE attempts ADD COLUMN budget_reserved TEXT;
ALTER TABLE evaluations ADD COLUMN budget_reserved TEXT;

-- Idempotency keys for paid actions other than run creation (explicit retries). The primary key
-- makes the check-and-record atomic, so concurrent requests with one key pay only once.
CREATE TABLE action_keys (
    key TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

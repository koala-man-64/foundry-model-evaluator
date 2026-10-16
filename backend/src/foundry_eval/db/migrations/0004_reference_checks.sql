-- Reference answers and deterministic answer checks per prompt: on suite prompts, and on the copies
-- frozen into each run (so a run keeps the reference and checks it was created with).
ALTER TABLE suite_prompts ADD COLUMN reference_text TEXT NOT NULL DEFAULT '';
ALTER TABLE suite_prompts ADD COLUMN checks_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE run_prompts ADD COLUMN reference_text TEXT NOT NULL DEFAULT '';
ALTER TABLE run_prompts ADD COLUMN checks_json TEXT NOT NULL DEFAULT '[]';

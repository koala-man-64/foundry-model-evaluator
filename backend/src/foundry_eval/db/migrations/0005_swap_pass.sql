-- Position-swap passes: an evaluation that re-judged the same answers as an earlier one, with the
-- presentation order reversed, names the evaluation it mirrors. NULL for every other evaluation, so
-- existing rows keep their meaning. The pairs measure how often the judge's preference follows the
-- position instead of the answer.
--
-- Rollback: an older app refuses a newer schema, so to go back run
--   DROP INDEX evaluations_swap_of; ALTER TABLE evaluations DROP COLUMN swap_of; PRAGMA user_version = 4;
-- Swap evaluations then read as ordinary re-judgements of the same answers.
ALTER TABLE evaluations ADD COLUMN swap_of TEXT REFERENCES evaluations(id);
CREATE INDEX evaluations_swap_of ON evaluations (swap_of);

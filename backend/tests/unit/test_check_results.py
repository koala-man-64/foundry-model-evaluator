"""Session retention, retry metadata and concurrent access to pattern outcomes."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from foundry_eval.grading.results import PatternResults


@pytest.mark.parametrize("value", [(True, "found", None), (False, "absent", None), (None, "retry", 123.5)])
def test_results_preserve_outcomes_and_expiry_until_cleared(value):
    results = PatternResults()
    try:
        assert results.get(b"missing") is None
        results.put(b"answer", value)
        assert results.get(b"answer") == value
        results.put(b"answer", (True, "updated", None))
        assert results.get(b"answer") == (True, "updated", None)
        results.clear()
        assert results.get(b"answer") is None
        results.put(b"new-session", value)
        assert results.get(b"new-session") == value
    finally:
        results.clear()


def test_concurrent_views_share_results_safely():
    results = PatternResults()

    def evaluate(n):
        key = n.to_bytes(4, "little")
        value = (n % 2 == 0, f"result-{n}", None)
        results.put(key, value)
        assert results.get(key) == value

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(evaluate, range(200)))
        for n in range(200):
            assert results.get(n.to_bytes(4, "little")) == (n % 2 == 0, f"result-{n}", None)
    finally:
        results.clear()

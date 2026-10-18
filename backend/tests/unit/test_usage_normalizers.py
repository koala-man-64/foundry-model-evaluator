from __future__ import annotations

import pytest

from foundry_eval.types import ApiStyle
from foundry_eval.usage.normalizers import normalize_usage

R, C, A = ApiStyle.OPENAI_RESPONSES, ApiStyle.OPENAI_CHAT, ApiStyle.ANTHROPIC_MESSAGES


def test_missing_usage_is_not_reported():
    assert normalize_usage(R, None) is None
    assert normalize_usage(C, {}) is None
    assert normalize_usage(A, None) is None


def test_responses_cached_and_reasoning_are_subsets(usage_fixture):
    facts = normalize_usage(R, usage_fixture("responses_cached_reasoning"))
    assert facts is not None
    assert facts.input_total == 5000
    assert facts.cache_read == 3840
    assert facts.cache_write is None  # field absent: not reported, not zero
    assert facts.input_uncached is None
    assert facts.output_total == 1200
    assert facts.reasoning == 896
    assert facts.anomalies == []


def test_responses_cache_write_reported(usage_fixture):
    facts = normalize_usage(R, usage_fixture("responses_cache_write"))
    assert facts is not None
    assert (facts.input_total, facts.cache_read, facts.cache_write) == (4000, 1000, 2500)
    assert facts.input_uncached == 500
    assert facts.input_uncached_derived is True


def test_responses_inconsistent_subset_is_anomaly(usage_fixture):
    facts = normalize_usage(R, usage_fixture("responses_inconsistent"))
    assert facts is not None
    assert facts.anomalies and "exceed total input" in facts.anomalies[0]
    assert facts.input_uncached is None


def test_responses_without_details(usage_fixture):
    facts = normalize_usage(R, usage_fixture("responses_no_details"))
    assert facts is not None
    assert facts.input_total == 800
    assert facts.cache_read is None
    assert facts.reasoning is None


def test_chat_fields(usage_fixture):
    facts = normalize_usage(C, usage_fixture("chat_cached_reasoning"))
    assert facts is not None
    assert (facts.input_total, facts.cache_read, facts.output_total, facts.reasoning) == (
        2048,
        1024,
        600,
        400,
    )
    assert facts.provider_total == 2648
    assert facts.notes == []


def test_chat_zero_audio_is_fine_but_nonzero_audio_is_flagged(usage_fixture):
    basic = normalize_usage(C, usage_fixture("chat_basic"))
    assert basic is not None and basic.unsupported_modalities == {}
    audio = normalize_usage(C, usage_fixture("chat_audio"))
    assert audio is not None
    assert audio.unsupported_modalities == {"prompt_tokens_details.audio_tokens": 50}


def test_chat_unknown_token_field_is_flagged(usage_fixture):
    facts = normalize_usage(C, usage_fixture("chat_unknown_field"))
    assert facts is not None
    assert facts.unrecognized_nonzero == {"prompt_tokens_details.mystery_tokens": 12}


def test_chat_known_subset_fields_are_informational(usage_fixture):
    facts = normalize_usage(C, usage_fixture("chat_text_tokens"))
    assert facts is not None
    assert facts.unrecognized_nonzero == {}
    assert facts.informational == {
        "prompt_tokens_details.text_tokens": 300,
        "completion_tokens_details.text_tokens": 20,
        "completion_tokens_details.rejected_prediction_tokens": 4,
    }


def test_chat_total_mismatch_is_a_note_not_an_anomaly():
    facts = normalize_usage(C, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 99})
    assert facts is not None
    assert facts.anomalies == []
    assert facts.notes and "differs" in facts.notes[0]


def test_negative_and_non_integer_values_are_anomalies():
    facts = normalize_usage(C, {"prompt_tokens": -1, "completion_tokens": "five"})
    assert facts is not None
    assert facts.input_total is None
    assert facts.output_total is None
    assert len(facts.anomalies) == 2


def test_float_integers_are_accepted():
    facts = normalize_usage(C, {"prompt_tokens": 10.0, "completion_tokens": 2})
    assert facts is not None
    assert facts.input_total == 10


def test_reasoning_above_output_is_noted():
    facts = normalize_usage(
        R,
        {"input_tokens": 1, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 9}},
    )
    assert facts is not None
    assert facts.notes and "exceed output" in facts.notes[0]


def test_claude_total_is_sum_of_three_input_fields(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_cache_write_split"))
    assert facts is not None
    assert facts.input_uncached == 2048
    assert facts.cache_read == 1800
    assert facts.cache_write == 248
    assert (facts.cache_write_5m, facts.cache_write_1h) == (148, 100)
    assert facts.input_total == 2048 + 1800 + 248
    assert facts.reasoning is None  # Claude reports thinking only when output_tokens_details is present


def test_claude_cache_read_example(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_cache_read"))
    assert facts is not None
    assert facts.input_total == 100050


def test_claude_write_without_split(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_cache_write_no_split"))
    assert facts is not None
    assert facts.cache_write == 2000
    assert facts.cache_write_5m is None and facts.cache_write_1h is None
    assert not facts.cache_write_split_reported


def test_claude_thinking_tokens(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_thinking"))
    assert facts is not None
    assert facts.output_total == 2400
    assert facts.reasoning == 1800


def test_claude_breakdown_mismatch_is_anomaly(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_mismatch"))
    assert facts is not None
    assert facts.anomalies and "does not match" in facts.anomalies[0]


def test_claude_partial_breakdown_is_ignored():
    facts = normalize_usage(
        A,
        {
            "input_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 5,
            "output_tokens": 1,
            "cache_creation": {"ephemeral_5m_input_tokens": 5},
        },
    )
    assert facts is not None
    assert facts.cache_write == 5
    assert facts.cache_write_5m is None
    assert facts.notes


def test_claude_breakdown_without_total_derives_total():
    facts = normalize_usage(
        A,
        {
            "input_tokens": 1,
            "cache_read_input_tokens": 0,
            "output_tokens": 1,
            "cache_creation": {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 4},
        },
    )
    assert facts is not None
    assert facts.cache_write == 7
    assert facts.input_total == 8


def test_claude_server_tool_requests_are_unrecognized(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_server_tool"))
    assert facts is not None
    assert facts.unrecognized_nonzero == {"server_tool_use.web_search_requests": 2}


def test_claude_missing_cache_fields_leave_total_unknown(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_missing_cache_fields"))
    assert facts is not None
    assert facts.input_total is None
    assert facts.cache_read is None


def test_claude_service_tier_and_geo(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_basic"))
    assert facts is not None
    assert facts.service_tier == "standard"
    assert facts.unrecognized_nonzero == {}


@pytest.mark.parametrize("style", [R, C])
def test_openai_service_tier_passthrough(style):
    usage = {"input_tokens": 1, "output_tokens": 1} if style is R else {"prompt_tokens": 1, "completion_tokens": 1}
    facts = normalize_usage(style, usage, service_tier="priority")
    assert facts is not None
    assert facts.service_tier == "priority"


def test_unknown_nested_mapping_is_scanned():
    facts = normalize_usage(
        R,
        {"input_tokens": 1, "output_tokens": 1, "brand_new": {"surprise_tokens": 3, "label": "x"}},
    )
    assert facts is not None
    assert facts.unrecognized_nonzero == {"brand_new.surprise_tokens": 3}


def test_null_nested_object_does_not_rescan_usage(usage_fixture):
    facts = normalize_usage(A, usage_fixture("claude_cache_write_no_split"))
    assert facts is not None
    assert facts.unrecognized_nonzero == {}


def test_boolean_values_are_not_token_counts():
    facts = normalize_usage(C, {"prompt_tokens": True, "completion_tokens": 1})
    assert facts is not None
    assert facts.input_total is None
    assert facts.anomalies


def test_unknown_non_token_numbers_are_ignored():
    facts = normalize_usage(C, {"prompt_tokens": 1, "completion_tokens": 1, "latency_ms": 250})
    assert facts is not None
    assert facts.unrecognized_nonzero == {}


def test_list_valued_usage_blocks_are_scanned():
    facts = normalize_usage(
        A,
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 5,
            "iterations": [{"type": "compaction", "input_tokens": 50000, "output_tokens": 3000}],
        },
    )
    assert facts is not None
    assert facts.unrecognized_nonzero == {"iterations[0].input_tokens": 50000, "iterations[0].output_tokens": 3000}


def test_premium_speed_is_flagged():
    facts = normalize_usage(
        A,
        {
            "input_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 1,
            "speed": "fast",
        },
    )
    assert facts is not None and "speed=fast" in facts.unrecognized_nonzero
    standard = normalize_usage(
        A,
        {
            "input_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 1,
            "speed": "standard",
        },
    )
    assert standard is not None and standard.unrecognized_nonzero == {}


def test_string_token_values_and_huge_floats_are_anomalies():
    facts = normalize_usage(C, {"prompt_tokens": 1e30, "completion_tokens": 1, "mystery_tokens": "12"})
    assert facts is not None
    assert facts.input_total is None
    assert any("mystery_tokens" in a for a in facts.anomalies)


@pytest.mark.parametrize("style", [R, C, A, ApiStyle.CLAUDE_CLI, ApiStyle.CODEX_CLI])
def test_non_mapping_usage_is_an_anomaly(style):
    facts = normalize_usage(style, ["not", "a", "dict"])  # type: ignore[arg-type]
    assert facts is not None and facts.anomalies == ["usage is not an object"]


def test_lists_of_scalars_are_ignored():
    facts = normalize_usage(C, {"prompt_tokens": 1, "completion_tokens": 1, "tags": [1, 2, 3]})
    assert facts is not None and facts.unrecognized_nonzero == {}


def test_claude_cli_accepts_consistent_iterations_without_unrecognized_flag():
    facts = normalize_usage(
        ApiStyle.CLAUDE_CLI,
        {
            "input_tokens": 15,
            "cache_read_input_tokens": 120,
            "cache_creation_input_tokens": 40,
            "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 0},
            "output_tokens": 80,
            "output_tokens_details": {"thinking_tokens": 32},
            "iterations": [
                {
                    "input_tokens": 15,
                    "output_tokens": 80,
                    "cache_read_input_tokens": 120,
                    "cache_creation_input_tokens": 40,
                }
            ],
        },
    )
    assert facts is not None
    assert facts.anomalies == []
    assert facts.unrecognized_nonzero == {}
    assert facts.input_total == 175
    assert facts.input_uncached == 15
    assert facts.cache_read == 120
    assert facts.cache_write == 40
    assert facts.output_total == 80
    assert facts.reasoning == 32

    no_iter = normalize_usage(
        ApiStyle.CLAUDE_CLI,
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 5,
        },
    )
    assert no_iter is not None and no_iter.input_total == 10


def test_claude_cli_flags_divergent_iterations_and_secondary_model_usage():
    facts = normalize_usage(
        ApiStyle.CLAUDE_CLI,
        {
            "input_tokens": 15,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 80,
            "iterations": [{"input_tokens": 999, "output_tokens": 777}],
            "_requested_model": "sonnet",
            "_model_usage": {
                "claude-sonnet-4-6": {"inputTokens": 15, "outputTokens": 80},
                "claude-haiku-4-5-20251001": {"inputTokens": 300, "outputTokens": 50},
                "claude-zero-model": {"inputTokens": 0, "outputTokens": 0},
            },
        },
    )
    assert facts is not None
    assert any("iterations input_tokens sum" in a for a in facts.anomalies)
    assert any("iterations output_tokens sum" in a for a in facts.anomalies)
    assert facts.unrecognized_nonzero == {"modelUsage.claude-haiku-4-5-20251001.tokens": 350}


def test_codex_cli_normalizes_flat_and_nested_token_fields():
    assert normalize_usage(ApiStyle.CODEX_CLI, None) is None
    facts = normalize_usage(
        ApiStyle.CODEX_CLI,
        {
            "input_tokens": 37131,
            "cached_input_tokens": 23168,
            "cache_write_input_tokens": 0,
            "output_tokens": 218,
            "reasoning_output_tokens": 128,
            "total_tokens": 37349,
        },
    )
    assert facts is not None
    assert facts.anomalies == []
    assert facts.unrecognized_nonzero == {}
    assert facts.input_total == 37131
    assert facts.cache_read == 23168
    assert facts.cache_write == 0
    assert facts.input_uncached == 37131 - 23168
    assert facts.input_uncached_derived is True
    assert facts.output_total == 218
    assert facts.reasoning == 128
    assert facts.provider_total == 37349

    minimal = normalize_usage(ApiStyle.CODEX_CLI, {"input_tokens": 10, "output_tokens": 5})
    assert minimal is not None
    assert minimal.input_total == 10
    assert minimal.output_total == 5

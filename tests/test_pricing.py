import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cx.pricing import estimate_cost_usd, estimate_stat_cost_usd, model_tier  # noqa: E402
from cx.sessions import ModelStat  # noqa: E402


def test_model_tier_matches_by_substring_regardless_of_version():
    assert model_tier("claude-opus-5") == "opus"
    assert model_tier("claude-3-5-sonnet-20241022") == "sonnet"
    assert model_tier("claude-haiku-4-5-20251001") == "haiku"


def test_model_tier_unknown_model_returns_none():
    assert model_tier("claude-fable-5") is None
    assert model_tier("gpt-4") is None


def test_estimate_cost_usd_computes_from_all_token_kinds():
    # sonnet: input $3, output $15, cache_write $3.75, cache_read $0.30 每百万 token
    cost = estimate_cost_usd("claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000,
                             cache_creation_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert cost == 3.0 + 15.0 + 3.75 + 0.30


def test_estimate_cost_usd_unknown_model_returns_none_not_zero():
    assert estimate_cost_usd("claude-fable-5", 1000, 1000, 0, 0) is None


def test_estimate_cost_usd_zero_tokens_is_zero():
    assert estimate_cost_usd("claude-opus-5", 0, 0, 0, 0) == 0.0


def test_estimate_stat_cost_usd_reads_fields_from_model_stat():
    stat = ModelStat(model="claude-haiku-4-5-20251001", sessions=1, messages=1,
                     input=1_000_000, output=0, cache_creation=0, cache_read=0,
                     first=None, last=None)
    assert estimate_stat_cost_usd(stat) == 0.80

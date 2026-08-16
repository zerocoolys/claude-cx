"""按模型名估算美元花费。

Anthropic 按「档位」（opus / sonnet / haiku）定价，同档位不同版本
（`claude-opus-5`、`claude-3-opus-...`）价格历史上保持一致，所以这里不逐个
型号列价格，而是用子串匹配把模型名归到档位再查表——新版本号发布时不用改代码。

价目表是写这个文件时的公开定价快照，会过期；查不到档位的模型（如未公开定价的
`fable`）一律返回 None，调用方要把它当"无法估算"处理，不能当 0 花费。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cx.sessions import ModelStat

# (input, output, cache_write, cache_read)，单位：美元 / 百万 token
_TIER_RATES: dict[str, tuple[float, float, float, float]] = {
    "opus": (15.0, 75.0, 18.75, 1.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku": (0.80, 4.0, 1.0, 0.08),
}

_TIER_ORDER = ("opus", "sonnet", "haiku")


def model_tier(model: str) -> str | None:
    """按子串匹配把型号名归到定价档位；查不到返回 None。"""
    lowered = model.lower()
    for tier in _TIER_ORDER:
        if tier in lowered:
            return tier
    return None


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int,
                      cache_creation_tokens: int, cache_read_tokens: int) -> float | None:
    """估算一次调用的花费；型号不在价目表里时返回 None（不是 0）。"""
    tier = model_tier(model)
    if tier is None:
        return None
    rate_in, rate_out, rate_write, rate_read = _TIER_RATES[tier]
    return (
        input_tokens * rate_in
        + output_tokens * rate_out
        + cache_creation_tokens * rate_write
        + cache_read_tokens * rate_read
    ) / 1_000_000


def estimate_stat_cost_usd(stat: ModelStat) -> float | None:
    return estimate_cost_usd(stat.model, stat.input, stat.output,
                             stat.cache_creation, stat.cache_read)

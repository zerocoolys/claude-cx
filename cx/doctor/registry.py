"""doctor 的数据模型与检查调度。

每个检查是一个纯函数 (Probe) -> list[Finding]，用 @check 注册。
三条契约见 spec 3.4：只读、崩溃隔离、确定性排序。
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Callable

from cx.model import Ctx

SEVERITY_RANK = {"error": 0, "warn": 1, "info": 2}


@dataclass(frozen=True)
class Finding:
    id: str      # 稳定的公开契约，形如 "refs.hook-command-missing"
    severity: str  # "error" | "warn" | "info"
    title: str
    detail: str
    where: str   # 文件路径，或点分配置键路径
    fix: str


@dataclass(frozen=True)
class Probe:
    """检查的唯一输入。四样东西 main() 已算好，不重复扫盘。"""

    ctx: Ctx
    merged: dict = field(default_factory=dict)
    prov: dict = field(default_factory=dict)
    assets: dict = field(default_factory=dict)
    budget: int = 20000


_CHECKS: list[Callable[[Probe], list[Finding]]] = []


def check(fn: Callable[[Probe], list[Finding]]) -> Callable[[Probe], list[Finding]]:
    """把检查函数注册进全局表。导入即注册。"""
    _CHECKS.append(fn)
    return fn


def _sort_key(f: Finding) -> tuple:
    return (SEVERITY_RANK.get(f.severity, 99), f.id, f.where)


def run_checks(probe: Probe) -> list[Finding]:
    """跑完所有检查并确定性排序。

    单个检查抛异常不会拖垮整轮——转成一条 internal.check-crashed 的 warn，
    其余检查照跑。否则一个边界 case 会让 doctor 在最需要它的坏配置上直接崩掉。
    """
    out: list[Finding] = []
    for fn in _CHECKS:
        try:
            result = fn(probe)
        except Exception as e:  # noqa: BLE001 — 崩溃隔离是本函数的职责
            out.append(
                Finding(
                    id="internal.check-crashed",
                    severity="warn",
                    title=f"检查 {fn.__name__} 执行时崩溃",
                    detail=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}",
                    where=f"cx.doctor:{fn.__name__}",
                    fix="这是 cx 自身的缺陷，请到项目 issue 区反馈上面的堆栈",
                )
            )
            continue
        out.extend(result or [])
    return sorted(out, key=_sort_key)

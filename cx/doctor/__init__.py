"""cx doctor —— 只读配置诊断。

导入本包即注册全部检查：下面四行 import 的副作用就是注册。
"""

from __future__ import annotations

from cx.doctor.registry import Finding, Probe, SEVERITY_RANK, check, run_checks

__all__ = ["Finding", "Probe", "SEVERITY_RANK", "check", "run_checks"]

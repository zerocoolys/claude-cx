"""doctor 检查的测试脚手架。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cx.doctor.registry import Probe  # noqa: E402
from cx.model import Ctx  # noqa: E402


def make_probe(*, ctx=None, merged=None, prov=None, assets=None, budget=20000,
               cwd=None, home=None):
    """构造一个默认全空的 Probe，测试只覆盖自己关心的部分。"""
    cwd = Path(cwd) if cwd else Path("/tmp/cx-test-proj")
    home = Path(home) if home else Path("/tmp/cx-test-home")
    if ctx is None:
        ctx = Ctx(cwd=cwd, repo_root=None, home=home)
    base_assets = {
        "memory": [], "agents": [], "commands": [], "skills": [],
        "mcp": [], "plugins": [], "gitignore": None,
    }
    base_assets.update(assets or {})
    return Probe(
        ctx=ctx,
        merged=merged or {},
        prov=prov or {},
        assets=base_assets,
        budget=budget,
    )

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.2.0"

# ---------------------------------------------------------------------------
# scope 定义：数字越大优先级越高
# 参考 https://code.claude.com/docs/en/settings
# ---------------------------------------------------------------------------
SCOPES = ["user", "project", "local", "managed"]
SCOPE_RANK = {name: i for i, name in enumerate(SCOPES)}
SCOPE_LABEL = {
    "user": "user ",
    "project": "proj ",
    "local": "local",
    "managed": "MGMT ",
}

# permission 规则跨 scope 合并而非覆盖
MERGE_ONLY_KEYS = {"permissions.allow", "permissions.deny", "permissions.ask"}
# fallbackModel 是整链替换，不拼接
REPLACE_WHOLE_KEYS = {"fallbackModel"}

SECRET_PAT = re.compile(
    r"(key|token|secret|password|passwd|credential|auth|bearer|session)", re.I
)


@dataclass
class SourceFile:
    scope: str
    path: Path
    exists: bool = False
    data: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class Ctx:
    cwd: Path
    repo_root: Path | None
    home: Path
    sources: list[SourceFile] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    show_secrets: bool = False


ALL_SECTIONS = ["env", "settings", "perms", "hooks", "mcp", "memory",
                "agents", "commands", "skills", "plugins"]

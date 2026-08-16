"""cx - Claude Code 配置状态检视器。

本模块只做再导出，保持 `import cx` 的公开 API 与拆包前一致。
"""

from __future__ import annotations

from cx import discovery, merge, model, render, scan, term, util
from cx.cli import main
from cx.discovery import discover_sources, find_repo_root, managed_dirs
from cx.merge import effective_scope, merge_with_provenance
from cx.model import (
    ALL_SECTIONS,
    MERGE_ONLY_KEYS,
    REPLACE_WHOLE_KEYS,
    SCOPE_LABEL,
    SCOPE_RANK,
    SCOPES,
    SECRET_PAT,
    VERSION,
    Ctx,
    SourceFile,
)
from cx.render import render
from cx.scan import (
    claude_version,
    gitignore_status,
    parse_frontmatter,
    scan_md_assets,
    scan_mcp,
    scan_memory,
    scan_plugins,
)
from cx.term import C, SCOPE_COLOR, hr, tag
from cx.util import (
    count_tokens_rough,
    disp_width,
    fmt_value,
    load_json,
    pad,
    redact,
    short,
)

__all__ = [
    "ALL_SECTIONS", "C", "Ctx", "MERGE_ONLY_KEYS", "REPLACE_WHOLE_KEYS",
    "SCOPES", "SCOPE_COLOR", "SCOPE_LABEL", "SCOPE_RANK", "SECRET_PAT",
    "SourceFile", "VERSION", "claude_version", "count_tokens_rough",
    "disp_width", "discover_sources", "discovery", "effective_scope",
    "find_repo_root", "fmt_value", "gitignore_status", "hr", "load_json",
    "main", "managed_dirs", "merge", "merge_with_provenance", "model",
    "pad", "parse_frontmatter", "redact", "render", "scan", "scan_mcp",
    "scan_md_assets", "scan_memory", "scan_plugins", "short", "tag",
    "term", "util",
]

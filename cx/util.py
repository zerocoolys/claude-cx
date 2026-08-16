from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from cx.model import Ctx, SECRET_PAT


def load_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"读取失败: {e}"
    if not raw.strip():
        return {}, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        # JSON 语法错误会导致整个文件被静默忽略，是 hook/权限不生效最常见的原因
        return None, f"JSON 语法错误 (行 {e.lineno} 列 {e.colno}): {e.msg}"


def redact(value: Any, key: str = "", show: bool = False) -> Any:
    if show:
        return value
    if isinstance(value, dict):
        return {k: redact(v, k, show) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key, show) for v in value]
    if isinstance(value, str) and SECRET_PAT.search(key) and value:
        if len(value) <= 8:
            return "••••"
        return f"{value[:4]}••••{value[-2:]} ({len(value)} chars)"
    return value


def short(path: Path, ctx: Ctx) -> str:
    s = str(path)
    try:
        if path.is_relative_to(ctx.cwd):
            return "./" + str(path.relative_to(ctx.cwd))
    except (ValueError, AttributeError):
        pass
    try:
        if path.is_relative_to(ctx.home):
            return "~/" + str(path.relative_to(ctx.home))
    except (ValueError, AttributeError):
        pass
    return s


def fmt_value(v: Any, width: int = 60) -> str:
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(v, bool):
        s = "true" if v else "false"
    elif v is None:
        s = "null"
    else:
        s = str(v)
    if len(s) > width:
        s = s[: width - 1] + "…"
    return s


def count_tokens_rough(text: str) -> int:
    """粗略估算 token：中文按字符计，其余按 ~4 字符/token。够用于排序。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", text))
    rest = len(text) - cjk
    return cjk + rest // 4


def disp_width(s: str) -> int:
    """CJK 字符占两个终端列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - disp_width(s))

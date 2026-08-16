from __future__ import annotations

import platform
from pathlib import Path

from cx.model import Ctx, SourceFile
from cx.util import load_json, short


def find_repo_root(start: Path) -> Path | None:
    p = start.resolve()
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def managed_dirs() -> list[Path]:
    sysname = platform.system()
    if sysname == "Darwin":
        return [Path("/Library/Application Support/ClaudeCode")]
    if sysname == "Windows":
        return [Path(r"C:\Program Files\ClaudeCode")]
    return [Path("/etc/claude-code")]


def discover_sources(ctx: Ctx) -> None:
    settings_root = ctx.repo_root or ctx.cwd

    candidates: list[tuple[str, Path]] = [
        ("user", ctx.home / ".claude" / "settings.json"),
        ("project", ctx.cwd / ".claude" / "settings.json"),
        ("local", settings_root / ".claude" / "settings.local.json"),
    ]
    # 2.1.211 之前 settings.local.json 总是落在启动目录，旧文件仍会被读取
    legacy_local = ctx.cwd / ".claude" / "settings.local.json"
    if legacy_local != settings_root / ".claude" / "settings.local.json":
        candidates.insert(2, ("local", legacy_local))

    # managed: managed-settings.json 作为基底，随后 managed-settings.d/*.json 按字母序叠加
    for d in managed_dirs():
        candidates.append(("managed", d / "managed-settings.json"))
        dropin = d / "managed-settings.d"
        if dropin.is_dir():
            for f in sorted(dropin.glob("*.json")):
                if not f.name.startswith("."):
                    candidates.append(("managed", f))

    for scope, path in candidates:
        data, err = load_json(path)
        sf = SourceFile(scope=scope, path=path, exists=path.exists())
        if err:
            sf.error = err
            lvl = "managed 配置" if scope == "managed" else "配置"
            ctx.problems.append(f"{short(path, ctx)}: {err} → 该{lvl}文件会被整体忽略")
        elif data is not None:
            sf.data = data
        ctx.sources.append(sf)

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cx.discovery import discover_sources, find_repo_root
from cx.merge import merge_with_provenance
from cx.model import ALL_SECTIONS, Ctx, VERSION
from cx.render import render
from cx.scan import gitignore_status, scan_mcp, scan_md_assets, scan_memory, scan_plugins
from cx.util import redact


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="cx", description="检视当前目录下 Claude Code 的生效配置"
    )
    ap.add_argument("--path", default=".", help="要检视的目录 (默认: 当前目录)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--show-secrets", action="store_true", help="不脱敏 env / MCP 中的密钥")
    ap.add_argument("--section", action="append", choices=ALL_SECTIONS,
                    help="只输出指定小节，可重复")
    ap.add_argument("--version", action="version", version=f"cx {VERSION}")
    args = ap.parse_args(argv)

    cwd = Path(args.path).resolve()
    if not cwd.is_dir():
        print(f"cx: 目录不存在: {cwd}", file=sys.stderr)
        return 2

    ctx = Ctx(cwd=cwd, repo_root=find_repo_root(cwd), home=Path.home(),
              show_secrets=args.show_secrets)
    discover_sources(ctx)
    merged, prov = merge_with_provenance(ctx.sources)

    assets = {
        "memory": scan_memory(ctx),
        "agents": scan_md_assets(ctx, "agents"),
        "commands": scan_md_assets(ctx, "commands"),
        "skills": scan_md_assets(ctx, "skills"),
        "mcp": scan_mcp(ctx, merged),
        "plugins": scan_plugins(ctx, merged),
        "gitignore": gitignore_status(ctx),
    }

    if args.json:
        payload = {
            "cx_version": VERSION,
            "cwd": str(ctx.cwd),
            "repo_root": str(ctx.repo_root) if ctx.repo_root else None,
            "sources": [
                {"scope": s.scope, "path": str(s.path), "exists": s.exists,
                 "error": s.error, "keys": sorted(s.data.keys())}
                for s in ctx.sources
            ],
            "effective": redact(merged, show=ctx.show_secrets),
            "provenance": {
                k: [{"scope": sc, "path": str(pp),
                     "value": redact(v, k.split(".")[-1], ctx.show_secrets)}
                    for sc, pp, v in entries]
                for k, entries in prov.items()
            },
            "assets": {k: v for k, v in assets.items() if k != "gitignore"},
            "settings_local_git_status": assets["gitignore"],
            "problems": ctx.problems,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    sections = set(args.section) if args.section else set(ALL_SECTIONS)
    render(ctx, merged, prov, assets, sections)
    return 0

"""cx 的命令行入口。

子命令布局见 spec 5.1。argparse 的 parents 有个经典陷阱：
如果共享 flag 在主 parser 和子 parser 上都带默认值，
`cx --path X doctor` 会被子 parser 的默认值 "." 覆盖掉 X。
解法是共享 parser 用 argument_default=SUPPRESS，默认值只由主 parser 的
set_defaults 提供——未显式给出的参数不会写进 namespace，也就不会覆盖。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cx.discovery import discover_sources, find_repo_root
from cx.merge import merge_with_provenance
from cx.model import ALL_SECTIONS, VERSION, Ctx
from cx.render import render
from cx.scan import (
    gitignore_status,
    scan_mcp,
    scan_md_assets,
    scan_memory,
    scan_plugins,
)
from cx.util import redact, split_tokens

FAIL_ON_CHOICES = ["error", "warn", "info", "never"]


def parse_sections(tokens: list[str] | None) -> set[str]:
    """把 --section / show 的模块参数解析成小节集合。空输入代表全量。"""
    names = split_tokens(tokens)
    if not names:
        return set(ALL_SECTIONS)
    unknown = [n for n in names if n not in ALL_SECTIONS]
    if unknown:
        raise ValueError(
            f"未知模块: {', '.join(unknown)}；可用模块: {', '.join(ALL_SECTIONS)}"
        )
    return set(names)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(
        add_help=False, argument_default=argparse.SUPPRESS
    )
    common.add_argument("--path", help="要检视的目录 (默认: 当前目录)")
    common.add_argument("--json", action="store_true", help="输出 JSON")
    common.add_argument("--show-secrets", action="store_true",
                        help="不脱敏 env / MCP 中的密钥")

    ap = argparse.ArgumentParser(
        prog="cx", parents=[common],
        description="检视与诊断当前目录下 Claude Code 的生效配置",
    )
    ap.add_argument("--section", action="append",
                    help="[deprecated] 等价于 cx show <模块>")
    ap.add_argument("--version", action="version", version=f"cx {VERSION}")
    ap.set_defaults(cmd=None, path=".", json=False, show_secrets=False,
                    modules=None, fail_on="error", ignore=None, budget=20000)

    sub = ap.add_subparsers(dest="cmd")

    p_show = sub.add_parser("show", parents=[common],
                            help="只输出指定模块")
    p_show.add_argument("modules", nargs="?",
                        help=f"逗号分隔，可选: {', '.join(ALL_SECTIONS)}")

    # p_doc 自己也要 SUPPRESS：--fail-on / --ignore / --budget 定义在
    # p_doc 上而非 common 上，没有 SUPPRESS 的话它们的 None 默认值会在
    # 子 parser 解析时覆盖掉主 parser set_defaults 给的 "error" / 20000。
    p_doc = sub.add_parser("doctor", parents=[common], help="诊断配置问题",
                           argument_default=argparse.SUPPRESS)
    p_doc.add_argument("--fail-on", choices=FAIL_ON_CHOICES, dest="fail_on",
                       help="退出码阈值 (默认: error)")
    p_doc.add_argument("--ignore", action="append",
                       help="抑制指定 finding id，可重复、可逗号分隔")
    p_doc.add_argument("--budget", type=int,
                       help="CLAUDE.md 常驻 token 阈值 (默认: 20000)")
    return ap


def build_context(args) -> tuple[Ctx, dict, dict, dict]:
    cwd = Path(args.path).resolve()
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
    return ctx, merged, prov, assets


def _run_doctor(args, ctx, merged, prov, assets) -> int:
    from cx.doctor import run_checks
    from cx.doctor.registry import Probe
    from cx.doctor.render import (
        doctor_payload,
        exit_code_for,
        render_doctor,
        split_ignored,
    )

    probe = Probe(ctx=ctx, merged=merged, prov=prov, assets=assets,
                  budget=args.budget)
    findings = run_checks(probe)
    visible, ignored = split_ignored(findings, set(split_tokens(args.ignore)))
    code = exit_code_for(visible, args.fail_on)

    if args.json:
        payload = doctor_payload(ctx, visible, ignored, args.fail_on, code)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        render_doctor(visible, ignored)
    return code


def _report_json(ctx, merged, prov, assets) -> int:
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


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if not Path(args.path).is_dir():
        print(f"cx: 目录不存在: {Path(args.path).resolve()}", file=sys.stderr)
        return 2

    try:
        section_tokens = [args.modules] if args.modules else args.section
        sections = parse_sections(section_tokens)
    except ValueError as e:
        print(f"cx: {e}", file=sys.stderr)
        return 2

    ctx, merged, prov, assets = build_context(args)

    if args.cmd == "doctor":
        return _run_doctor(args, ctx, merged, prov, assets)

    if args.json:
        return _report_json(ctx, merged, prov, assets)

    render(ctx, merged, prov, assets, sections)
    return 0

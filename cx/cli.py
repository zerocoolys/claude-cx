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

    p_model = sub.add_parser("model", parents=[common],
                             help="统计本项目各会话里每个模型的 token 用量")
    p_model.add_argument("--detail", action="store_true",
                         help="额外输出子 agent 调用与逐会话明细")

    p_debug = sub.add_parser("debug", parents=[common],
                             help="查看会话请求的调试字段 (stop_reason / requestId / 错误 / tool_use)")
    p_debug.add_argument("--limit", type=int, default=200,
                         help="最多显示条数 (默认: 200)")
    p_debug.add_argument("--follow", "-f", action="store_true",
                         help="持续输出新增的调试记录，类似 tail -f")

    p_server = sub.add_parser("server", parents=[common],
                              help="启动本地 HTTP dashboard")
    p_server.add_argument("--host", default="127.0.0.1", help="监听地址 (默认: 127.0.0.1)")
    p_server.add_argument("--port", type=int, default=8765, help="监听端口 (默认: 8765)")
    p_server.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
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


def build_doctor_payload(ctx, merged, prov, assets, budget, ignore, fail_on) -> dict:
    """跑一轮 doctor 检查，返回可直接 json.dumps 的 payload。CLI 和 server 共用。"""
    from cx.doctor import run_checks
    from cx.doctor.registry import Probe
    from cx.doctor.render import doctor_payload, exit_code_for, split_ignored

    probe = Probe(ctx=ctx, merged=merged, prov=prov, assets=assets, budget=budget)
    findings = run_checks(probe)
    visible, ignored = split_ignored(findings, set(split_tokens(ignore)))
    code = exit_code_for(visible, fail_on)
    return doctor_payload(ctx, visible, ignored, fail_on, code)


def _run_doctor(args, ctx, merged, prov, assets) -> int:
    from cx.doctor.render import render_doctor

    if args.json:
        payload = build_doctor_payload(ctx, merged, prov, assets, args.budget,
                                       args.ignore, args.fail_on)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return payload["exit_code"]

    from cx.doctor import run_checks
    from cx.doctor.registry import Probe
    from cx.doctor.render import exit_code_for, split_ignored

    probe = Probe(ctx=ctx, merged=merged, prov=prov, assets=assets,
                  budget=args.budget)
    findings = run_checks(probe)
    visible, ignored = split_ignored(findings, set(split_tokens(args.ignore)))
    code = exit_code_for(visible, args.fail_on)
    render_doctor(visible, ignored)
    return code


def _run_model(args) -> int:
    """cx model 只读会话记录，不需要走配置发现/合并那一整套。"""
    from cx.render import render_model_usage
    from cx.sessions import collect_usage, usage_payload

    cwd = Path(args.path).resolve()
    ctx = Ctx(cwd=cwd, repo_root=find_repo_root(cwd), home=Path.home(),
              show_secrets=args.show_secrets)
    usage = collect_usage(ctx)
    detail = getattr(args, "detail", False)

    if args.json:
        payload = {"cx_version": VERSION, **usage_payload(usage, detail)}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        render_model_usage(ctx, usage, detail)
    return 0


def build_report_payload(ctx, merged, prov, assets) -> dict:
    """完整配置报告的 JSON payload。CLI 的 --json 和 server 的 /api/config 共用。"""
    return {
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


def _run_debug(args) -> int:
    """cx debug 只读会话记录，跟 model 一样不需要走配置发现/合并那一整套。"""
    from cx.render import render_debug_log
    from cx.sessions import collect_debug_log, debug_log_payload

    cwd = Path(args.path).resolve()
    ctx = Ctx(cwd=cwd, repo_root=find_repo_root(cwd), home=Path.home(),
              show_secrets=args.show_secrets)

    if getattr(args, "follow", False):
        return _run_debug_follow(ctx, args)

    entries = collect_debug_log(ctx, limit=args.limit)
    if args.json:
        payload = {"cx_version": VERSION, **debug_log_payload(entries)}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        render_debug_log(ctx, entries)
    return 0


_FOLLOW_POLL_SECONDS = 1.0


def _emit_debug_entry(e, as_json: bool) -> None:
    # --follow 常被重定向到文件或管道，非 tty 下 stdout 是全缓冲的；
    # 不 flush 的话新记录会在缓冲区里等半天，"实时" tail 就名不副实。
    if as_json:
        from cx.sessions import debug_entry_payload

        print(json.dumps(debug_entry_payload(e), ensure_ascii=False, default=str))
    else:
        from cx.render import render_debug_entry

        render_debug_entry(e)
    sys.stdout.flush()


def _run_debug_follow(ctx, args) -> int:
    """轮询式 tail -f：每秒重新扫盘，只吐出比上次见过的时间戳更新的记录。

    会话目录本地读写量很小，全量重扫比维护逐文件字节偏移量简单得多，
    且天然免疫日志轮转/新文件出现——不需要额外处理。
    """
    import time

    from cx.sessions import collect_debug_log
    from cx.term import C

    entries = collect_debug_log(ctx, limit=args.limit)
    for e in reversed(entries):
        _emit_debug_entry(e, args.json)
    after_ts = entries[0].timestamp if entries else ""

    if not args.json:
        print(C.dim("  -- 等待新的调试记录 (Ctrl+C 退出) --"))
        sys.stdout.flush()
    try:
        while True:
            time.sleep(_FOLLOW_POLL_SECONDS)
            fresh = collect_debug_log(ctx, limit=1000, after_ts=after_ts)
            for e in fresh:
                _emit_debug_entry(e, args.json)
                after_ts = e.timestamp
    except KeyboardInterrupt:
        pass
    return 0


def _run_server(args) -> int:
    from cx.server import serve

    path = Path(args.path).resolve()
    return serve(path, host=args.host, port=args.port,
                show_secrets=args.show_secrets, open_browser=not args.no_open)


def _report_json(ctx, merged, prov, assets) -> int:
    payload = build_report_payload(ctx, merged, prov, assets)
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

    if args.cmd == "model":
        return _run_model(args)

    if args.cmd == "debug":
        return _run_debug(args)

    if args.cmd == "server":
        return _run_server(args)

    ctx, merged, prov, assets = build_context(args)

    if args.cmd == "doctor":
        return _run_doctor(args, ctx, merged, prov, assets)

    if args.json:
        return _report_json(ctx, merged, prov, assets)

    from cx.doctor import run_checks
    from cx.doctor.registry import Probe

    findings = run_checks(Probe(ctx=ctx, merged=merged, prov=prov,
                                assets=assets, budget=args.budget))
    render(ctx, merged, prov, assets, sections, findings=findings)
    return 0

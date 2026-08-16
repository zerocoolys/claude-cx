"""doctor 的人读输出与 JSON 输出。"""

from __future__ import annotations

from cx.doctor.registry import SEVERITY_RANK, Finding
from cx.model import VERSION, Ctx
from cx.term import C, hr

SEVERITY_MARK = {
    "error": ("✗", C.red),
    "warn": ("⚠", C.yellow),
    "info": ("·", C.blue),
}


def split_ignored(
    findings: list[Finding], ignore_ids: set[str]
) -> tuple[list[Finding], list[Finding]]:
    """按 id 把 finding 分成可见与被抑制两组，各自保持原有顺序。"""
    visible = [f for f in findings if f.id not in ignore_ids]
    ignored = [f for f in findings if f.id in ignore_ids]
    return visible, ignored


def exit_code_for(visible: list[Finding], fail_on: str) -> int:
    """阈值以上（含）存在任何可见 finding 即返回 1。被抑制的不参与判定。"""
    if fail_on == "never":
        return 0
    threshold = SEVERITY_RANK[fail_on]
    for f in visible:
        if SEVERITY_RANK.get(f.severity, 99) <= threshold:
            return 1
    return 0


def _counts(findings: list[Finding]) -> dict[str, int]:
    out = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        if f.severity in out:
            out[f.severity] += 1
    return out


def render_doctor(visible: list[Finding], ignored: list[Finding]) -> None:
    print(hr("doctor"))

    if not visible:
        msg = "未发现问题"
        print("  " + C.green(f"✓ {msg}"))
    for f in visible:
        mark, color = SEVERITY_MARK.get(f.severity, ("·", C.dim))
        print()
        print(f"  {color(mark)} {color(f.severity):<9} {C.cyan(f.id)}")
        print(f"            {f.title}")
        for line in f.detail.splitlines():
            print(f"            {C.dim(line)}")
        print(f"            {C.dim('位置')}  {f.where}")
        print(f"            {C.dim('修复')}  {f.fix}")

    c = _counts(visible)
    tail = ""
    if ignored:
        tail = C.dim(f"      ({len(ignored)} 项被 --ignore 抑制)")
    print()
    print(f"  合计  {c['error']} error · {c['warn']} warn · {c['info']} info{tail}")
    print()


def doctor_payload(
    ctx: Ctx,
    visible: list[Finding],
    ignored: list[Finding],
    fail_on: str,
    exit_code: int,
) -> dict:
    def row(f: Finding, is_ignored: bool) -> dict:
        return {
            "id": f.id,
            "severity": f.severity,
            "title": f.title,
            "detail": f.detail,
            "where": f.where,
            "fix": f.fix,
            "ignored": is_ignored,
        }

    summary = _counts(visible)
    summary["ignored"] = len(ignored)
    return {
        "cx_version": VERSION,
        "cwd": str(ctx.cwd),
        "findings": [row(f, False) for f in visible] + [row(f, True) for f in ignored],
        "summary": summary,
        "fail_on": fail_on,
        "exit_code": exit_code,
    }

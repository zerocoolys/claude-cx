"""doctor 输出与退出码语义。"""

from cx.doctor.registry import Finding
from cx.doctor.render import (
    doctor_payload,
    exit_code_for,
    render_doctor,
    split_ignored,
)
from cx.model import Ctx
from pathlib import Path


def finding(fid, severity="warn"):
    return Finding(id=fid, severity=severity, title=f"标题 {fid}",
                   detail="细节", where="某处", fix="改法")


def test_split_ignored_separates_by_id():
    findings = [finding("refs.a"), finding("schema.b"), finding("refs.c")]
    visible, ignored = split_ignored(findings, {"schema.b"})
    assert [f.id for f in visible] == ["refs.a", "refs.c"]
    assert [f.id for f in ignored] == ["schema.b"]


def test_split_ignored_with_empty_set_hides_nothing():
    findings = [finding("refs.a")]
    visible, ignored = split_ignored(findings, set())
    assert len(visible) == 1
    assert ignored == []


def test_exit_code_default_threshold_is_error():
    assert exit_code_for([finding("refs.a", "error")], "error") == 1
    assert exit_code_for([finding("refs.a", "warn")], "error") == 0
    assert exit_code_for([], "error") == 0


def test_exit_code_warn_threshold_counts_errors_too():
    assert exit_code_for([finding("refs.a", "warn")], "warn") == 1
    assert exit_code_for([finding("refs.a", "error")], "warn") == 1
    assert exit_code_for([finding("refs.a", "info")], "warn") == 0


def test_exit_code_never_is_always_zero():
    assert exit_code_for([finding("refs.a", "error")], "never") == 0


def test_ignored_findings_do_not_affect_exit_code():
    findings = [finding("refs.a", "error")]
    visible, _ = split_ignored(findings, {"refs.a"})
    assert exit_code_for(visible, "error") == 0


def test_payload_marks_ignored_and_carries_summary():
    visible = [finding("refs.a", "error"), finding("schema.b", "warn")]
    ignored = [finding("security.c", "warn")]
    ctx = Ctx(cwd=Path("/p"), repo_root=None, home=Path("/h"))

    payload = doctor_payload(ctx, visible, ignored, "error", 1)

    by_id = {f["id"]: f for f in payload["findings"]}
    assert by_id["refs.a"]["ignored"] is False
    assert by_id["security.c"]["ignored"] is True
    assert payload["summary"] == {"error": 1, "warn": 1, "info": 0, "ignored": 1}
    assert payload["fail_on"] == "error"
    assert payload["exit_code"] == 1


def test_payload_summary_excludes_ignored_from_severity_counts():
    """被抑制的 finding 不计入 error/warn/info，只计入 ignored。"""
    ctx = Ctx(cwd=Path("/p"), repo_root=None, home=Path("/h"))
    payload = doctor_payload(ctx, [], [finding("refs.a", "error")], "error", 0)
    assert payload["summary"]["error"] == 0
    assert payload["summary"]["ignored"] == 1


def test_render_prints_id_title_where_and_fix(capsys):
    render_doctor([finding("refs.a", "error")], [])
    out = capsys.readouterr().out
    assert "refs.a" in out
    assert "标题 refs.a" in out
    assert "某处" in out
    assert "改法" in out


def test_render_reports_clean_when_nothing_found(capsys):
    render_doctor([], [])
    out = capsys.readouterr().out
    assert "未发现问题" in out


def test_render_mentions_suppressed_count(capsys):
    render_doctor([], [finding("refs.a"), finding("refs.b")])
    out = capsys.readouterr().out
    assert "2" in out
    assert "抑制" in out

"""默认报告与 doctor 引擎的收口一致性。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cx.cli import main  # noqa: E402


@pytest.fixture
def broken_proj(tmp_path, monkeypatch):
    home = tmp_path / "home"
    p = tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    (p / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (p / ".claude" / "settings.json").write_text("{ broken", encoding="utf-8")
    return p


def test_default_report_surfaces_error_findings(broken_proj, capsys):
    assert main(["--path", str(broken_proj)]) == 0
    out = capsys.readouterr().out
    assert "需要注意" in out
    assert "schema.json-syntax" in out


def test_default_report_hints_at_doctor(broken_proj, capsys):
    main(["--path", str(broken_proj)])
    assert "cx doctor" in capsys.readouterr().out


def test_default_report_shows_only_errors(broken_proj, capsys):
    """warn/info 级不进默认报告，避免刷屏。"""
    main(["--path", str(broken_proj)])
    out = capsys.readouterr().out
    assert "conflicts.shadowed-key" not in out


def test_default_report_error_set_matches_doctor(broken_proj, capsys):
    main(["doctor", "--path", str(broken_proj), "--json"])
    payload = json.loads(capsys.readouterr().out)
    doctor_errors = {f["id"] for f in payload["findings"]
                     if f["severity"] == "error" and not f["ignored"]}

    main(["--path", str(broken_proj)])
    report = capsys.readouterr().out

    assert doctor_errors  # 这个 fixture 必然产生 error，否则测试没意义
    for fid in doctor_errors:
        assert fid in report


@pytest.fixture
def broken_mcp_json_proj(tmp_path, monkeypatch):
    """.mcp.json 语法错误：曾经会被 cx 打印，重构后一度变得静默（finding #1）。"""
    home = tmp_path / "home"
    p = tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    (p / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (p / ".mcp.json").write_text('{"mcpServers": {,}}', encoding="utf-8")
    return p


def test_broken_mcp_json_is_reported_by_doctor(broken_mcp_json_proj, capsys):
    assert main(["doctor", "--path", str(broken_mcp_json_proj), "--json"]) != 0
    payload = json.loads(capsys.readouterr().out)
    matches = [f for f in payload["findings"] if f["id"] == "schema.json-syntax"]
    assert matches, "expected schema.json-syntax to fire for broken .mcp.json"
    assert any(".mcp.json" in f["where"] for f in matches)


def test_broken_mcp_json_is_visible_in_default_report(broken_mcp_json_proj, capsys):
    """回归测试：cx（无子命令）之前会打印这个错误，Task 9 重构后一度变得静默。"""
    assert main(["--path", str(broken_mcp_json_proj)]) == 0
    out = capsys.readouterr().out
    assert "需要注意" in out
    assert "schema.json-syntax" in out
    assert ".mcp.json" in out


def test_clean_project_has_no_attention_section(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    p = tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    (p / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    assert main(["--path", str(p)]) == 0
    assert "需要注意" not in capsys.readouterr().out

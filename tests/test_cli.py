"""CLI 分发、模块解析与退出码。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cx.cli import main, parse_sections  # noqa: E402
from cx.model import ALL_SECTIONS  # noqa: E402
from cx.util import split_tokens  # noqa: E402


@pytest.fixture
def proj(tmp_path, monkeypatch):
    home = tmp_path / "home"
    p = tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    (p / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return p


@pytest.fixture
def stub_check(monkeypatch):
    """注册一个确定性的假检查。

    本任务只负责 CLI 的接线（--ignore / --fail-on / --json 是否正确
    作用于 doctor 引擎的输出），不负责任何真实检查——那是 Tasks 5-8。
    用桩替代真实检查，测试才不会依赖尚未存在的 finding id。
    """
    from cx.doctor import registry

    def _fake(probe):
        return [registry.Finding(id="refs.stub", severity="error", title="桩",
                                 detail="细节", where="某处", fix="改法")]

    monkeypatch.setattr(registry, "_CHECKS", [_fake])
    return _fake


# --- split_tokens -----------------------------------------------------------
def test_split_tokens_handles_commas_repeats_and_whitespace():
    assert split_tokens(["a,b", " c ", "d , e"]) == ["a", "b", "c", "d", "e"]


def test_split_tokens_drops_empties():
    assert split_tokens(["a,,b", "", "  "]) == ["a", "b"]


def test_split_tokens_of_none_is_empty():
    assert split_tokens(None) == []


# --- parse_sections ---------------------------------------------------------
def test_parse_sections_empty_means_all():
    assert parse_sections(None) == set(ALL_SECTIONS)
    assert parse_sections([]) == set(ALL_SECTIONS)


def test_parse_sections_single_name():
    assert parse_sections(["skills"]) == {"skills"}


def test_parse_sections_comma_separated():
    assert parse_sections(["mcp,perms"]) == {"mcp", "perms"}


def test_parse_sections_dedupes():
    assert parse_sections(["mcp", "mcp,perms"]) == {"mcp", "perms"}


def test_parse_sections_rejects_unknown_and_lists_valid_names():
    with pytest.raises(ValueError) as e:
        parse_sections(["nope"])
    assert "nope" in str(e.value)
    assert "skills" in str(e.value)


# --- 分发 -------------------------------------------------------------------
def test_bare_cx_renders_full_report(proj, capsys):
    assert main(["--path", str(proj)]) == 0
    assert "配置来源" in capsys.readouterr().out


def test_show_subcommand_limits_output(proj, capsys):
    assert main(["show", "skills", "--path", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "Skills" in out
    assert "配置来源" not in out


def test_deprecated_section_flag_still_works(proj, capsys):
    assert main(["--section", "skills", "--path", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "Skills" in out
    assert "配置来源" not in out


def test_show_rejects_unknown_module(proj, capsys):
    assert main(["show", "nope", "--path", str(proj)]) == 2
    assert "nope" in capsys.readouterr().err


def test_missing_directory_returns_2(capsys):
    assert main(["--path", "/definitely/not/here"]) == 2
    assert "目录不存在" in capsys.readouterr().err


def test_global_flag_before_subcommand_is_not_clobbered(proj, capsys):
    """--path 写在子命令之前也必须生效（argparse parents 的经典陷阱）。"""
    assert main(["--path", str(proj), "doctor"]) in (0, 1)
    assert "doctor" in capsys.readouterr().out


def test_doctor_clean_config_exits_0(proj, capsys):
    assert main(["doctor", "--path", str(proj)]) == 0
    assert "未发现问题" in capsys.readouterr().out


def test_doctor_error_finding_exits_1(proj, stub_check, capsys):
    assert main(["doctor", "--path", str(proj)]) == 1
    assert "refs.stub" in capsys.readouterr().out


def test_doctor_fail_on_never_always_exits_0(proj, stub_check, capsys):
    assert main(["doctor", "--path", str(proj), "--fail-on", "never"]) == 0


def test_doctor_json_output_is_valid(proj, capsys):
    assert main(["doctor", "--path", str(proj), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fail_on"] == "error"
    assert payload["summary"]["error"] == 0


def test_doctor_ignore_accepts_comma_separated(proj, stub_check, capsys):
    code = main(["doctor", "--path", str(proj),
                 "--ignore", "refs.stub,refs.nothing", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["summary"]["ignored"] == 1
    assert payload["summary"]["error"] == 0
    assert any(f["ignored"] for f in payload["findings"])


# --- debug --------------------------------------------------------------
def test_debug_subcommand_with_no_sessions(proj, capsys):
    assert main(["debug", "--path", str(proj)]) == 0
    assert "没有找到该项目的会话记录" in capsys.readouterr().out


def test_debug_json_output_is_valid(proj, capsys):
    assert main(["debug", "--path", str(proj), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entries"] == []
    assert "cx_version" in payload


def test_debug_follow_prints_existing_then_waits_for_interrupt(proj, capsys, monkeypatch):
    """--follow 是轮询循环，用假 sleep 在第一轮就抛 KeyboardInterrupt 让它退出。"""
    def fake_sleep(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", fake_sleep)
    assert main(["debug", "--path", str(proj), "--follow"]) == 0
    assert "等待新的调试记录" in capsys.readouterr().out

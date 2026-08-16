"""refs 类检查：hook / MCP / CLAUDE.md 导入的失效引用。"""

from cx.doctor.checks_refs import (
    check_hook_commands,
    check_mcp_commands,
    check_mcp_urls,
    check_memory_imports,
)

from .conftest import make_probe


def hooks_with(command):
    return {"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}}


# --- hook 命令 ---------------------------------------------------------------
def test_hook_command_missing_path_is_error(tmp_path):
    probe = make_probe(merged=hooks_with(str(tmp_path / "nope")))
    findings = check_hook_commands(probe)
    assert [f.id for f in findings] == ["refs.hook-command-missing"]
    assert findings[0].severity == "error"


def test_hook_command_existing_executable_is_clean(tmp_path):
    exe = tmp_path / "fmt"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    probe = make_probe(merged=hooks_with(str(exe)))
    assert check_hook_commands(probe) == []


def test_hook_command_existing_but_not_executable_is_error(tmp_path):
    f = tmp_path / "notexec"
    f.write_text("x", encoding="utf-8")
    f.chmod(0o644)
    probe = make_probe(merged=hooks_with(str(f)))
    assert [x.id for x in check_hook_commands(probe)] == ["refs.hook-command-missing"]


def test_hook_bare_command_on_path_is_clean(monkeypatch):
    monkeypatch.setattr("cx.doctor.checks_refs.shutil.which",
                        lambda name: "/usr/bin/" + name)
    probe = make_probe(merged=hooks_with("jq -r ."))
    assert check_hook_commands(probe) == []


def test_hook_bare_command_off_path_is_error(monkeypatch):
    monkeypatch.setattr("cx.doctor.checks_refs.shutil.which", lambda name: None)
    probe = make_probe(merged=hooks_with("nosuchtool --flag"))
    assert [x.id for x in check_hook_commands(probe)] == ["refs.hook-command-missing"]


def test_hook_shell_fragment_degrades_to_info(monkeypatch):
    """含 shell 元字符时不硬猜，降级为 info。"""
    monkeypatch.setattr("cx.doctor.checks_refs.shutil.which", lambda name: None)
    probe = make_probe(merged=hooks_with("cat x | jq -r ."))
    findings = check_hook_commands(probe)
    assert [f.id for f in findings] == ["refs.hook-command-missing"]
    assert findings[0].severity == "info"


def test_hook_empty_command_is_ignored():
    assert check_hook_commands(make_probe(merged=hooks_with("   "))) == []


def test_no_hooks_is_clean():
    assert check_hook_commands(make_probe()) == []


# --- MCP 命令 ---------------------------------------------------------------
def test_mcp_stdio_command_off_path_is_error(monkeypatch):
    monkeypatch.setattr("cx.doctor.checks_refs.shutil.which", lambda name: None)
    probe = make_probe(assets={"mcp": [
        {"name": "s", "scope": "user", "source": "~/.claude.json",
         "spec": {"command": "nosuchserver"}}]})
    assert [f.id for f in check_mcp_commands(probe)] == ["refs.mcp-command-missing"]


def test_mcp_stdio_command_on_path_is_clean(monkeypatch):
    monkeypatch.setattr("cx.doctor.checks_refs.shutil.which",
                        lambda name: "/usr/bin/" + name)
    probe = make_probe(assets={"mcp": [
        {"name": "s", "scope": "user", "source": "x",
         "spec": {"command": "npx"}}]})
    assert check_mcp_commands(probe) == []


def test_mcp_http_server_is_not_command_checked():
    probe = make_probe(assets={"mcp": [
        {"name": "s", "scope": "user", "source": "x",
         "spec": {"type": "http", "url": "https://example.com/mcp"}}]})
    assert check_mcp_commands(probe) == []


# --- MCP URL ----------------------------------------------------------------
def test_mcp_bad_url_is_warn():
    probe = make_probe(assets={"mcp": [
        {"name": "s", "scope": "user", "source": "x",
         "spec": {"type": "http", "url": "not a url"}}]})
    findings = check_mcp_urls(probe)
    assert [f.id for f in findings] == ["refs.mcp-url-invalid"]
    assert findings[0].severity == "warn"


def test_mcp_good_url_is_clean():
    probe = make_probe(assets={"mcp": [
        {"name": "s", "scope": "user", "source": "x",
         "spec": {"type": "http", "url": "https://example.com/mcp"}}]})
    assert check_mcp_urls(probe) == []


def test_mcp_stdio_server_is_not_url_checked():
    probe = make_probe(assets={"mcp": [
        {"name": "s", "scope": "user", "source": "x",
         "spec": {"command": "npx"}}]})
    assert check_mcp_urls(probe) == []


# --- CLAUDE.md 导入 ----------------------------------------------------------
def test_memory_import_missing_is_error(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("@rules/missing.md\n", encoding="utf-8")
    probe = make_probe(assets={"memory": [
        {"scope": "project", "path": str(md), "lines": 1, "bytes": 1,
         "tokens": 1, "imports": ["rules/missing.md"]}]})
    findings = check_memory_imports(probe)
    assert [f.id for f in findings] == ["refs.memory-import-missing"]
    assert findings[0].severity == "error"


def test_memory_import_present_is_clean(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "ok.md").write_text("x", encoding="utf-8")
    md = tmp_path / "CLAUDE.md"
    md.write_text("@rules/ok.md\n", encoding="utf-8")
    probe = make_probe(assets={"memory": [
        {"scope": "project", "path": str(md), "lines": 1, "bytes": 1,
         "tokens": 1, "imports": ["rules/ok.md"]}]})
    assert check_memory_imports(probe) == []


def test_memory_import_resolves_relative_to_its_own_file(tmp_path):
    """导入路径相对于 CLAUDE.md 所在目录，不是 cwd。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "x.md").write_text("x", encoding="utf-8")
    md = sub / "CLAUDE.md"
    md.write_text("@x.md\n", encoding="utf-8")
    probe = make_probe(assets={"memory": [
        {"scope": "project", "path": str(md), "lines": 1, "bytes": 1,
         "tokens": 1, "imports": ["x.md"]}]})
    assert check_memory_imports(probe) == []


def test_memory_import_expands_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "RTK.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    md = tmp_path / "CLAUDE.md"
    md.write_text("@~/.claude/RTK.md\n", encoding="utf-8")
    probe = make_probe(assets={"memory": [
        {"scope": "user", "path": str(md), "lines": 1, "bytes": 1,
         "tokens": 1, "imports": ["~/.claude/RTK.md"]}]})
    assert check_memory_imports(probe) == []

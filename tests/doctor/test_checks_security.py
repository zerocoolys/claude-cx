"""security 类检查：密钥泄漏、过宽权限、上下文预算。"""

from cx.doctor.checks_security import (
    check_broad_allow,
    check_context_budget,
    check_local_settings_git,
    check_plaintext_secrets,
)

from .conftest import make_probe


# --- settings.local.json 的 git 状态 -----------------------------------------
def test_tracked_local_settings_is_error():
    findings = check_local_settings_git(make_probe(assets={"gitignore": "tracked"}))
    assert [f.id for f in findings] == ["security.local-settings-tracked"]
    assert findings[0].severity == "error"


def test_untracked_local_settings_is_warn():
    findings = check_local_settings_git(make_probe(assets={"gitignore": "untracked"}))
    assert [f.id for f in findings] == ["security.local-settings-unignored"]
    assert findings[0].severity == "warn"


def test_ignored_local_settings_is_clean():
    assert check_local_settings_git(make_probe(assets={"gitignore": "ignored"})) == []


def test_no_local_settings_is_clean():
    assert check_local_settings_git(make_probe(assets={"gitignore": None})) == []


# --- 明文密钥 ---------------------------------------------------------------
def test_plaintext_api_key_in_env_is_warn():
    probe = make_probe(merged={"env": {"ANTHROPIC_API_KEY": "sk-ant-realvalue"}})
    findings = check_plaintext_secrets(probe)
    assert [f.id for f in findings] == ["security.plaintext-secret"]
    assert findings[0].severity == "warn"


def test_secret_value_is_not_echoed_in_the_finding():
    """finding 本身不能泄漏密钥——它会被打印、被写进 CI 日志。"""
    probe = make_probe(merged={"env": {"ANTHROPIC_API_KEY": "sk-ant-realvalue"}})
    f = check_plaintext_secrets(probe)[0]
    blob = f.title + f.detail + f.where + f.fix
    assert "sk-ant-realvalue" not in blob


def test_variable_reference_is_clean():
    probe = make_probe(merged={"env": {"ANTHROPIC_API_KEY": "${MY_KEY}"}})
    assert check_plaintext_secrets(probe) == []


def test_empty_value_is_clean():
    assert check_plaintext_secrets(make_probe(merged={"env": {"API_KEY": ""}})) == []


def test_non_secret_env_key_is_clean():
    probe = make_probe(merged={"env": {"EDITOR": "vim"}})
    assert check_plaintext_secrets(probe) == []


# --- 过宽的 allow 规则 -------------------------------------------------------
def test_bare_bash_allow_is_warn():
    probe = make_probe(merged={"permissions": {"allow": ["Bash"]}})
    findings = check_broad_allow(probe)
    assert [f.id for f in findings] == ["security.broad-allow"]


def test_bash_star_allow_is_warn():
    probe = make_probe(merged={"permissions": {"allow": ["Bash(*)"]}})
    assert [f.id for f in check_broad_allow(probe)] == ["security.broad-allow"]


def test_narrow_allow_is_clean():
    probe = make_probe(merged={"permissions": {"allow": ["Bash(git status)"]}})
    assert check_broad_allow(probe) == []


def test_broad_rule_in_deny_is_not_flagged():
    """deny 里的宽规则是收紧，不是风险。"""
    probe = make_probe(merged={"permissions": {"deny": ["Bash(*)"]}})
    assert check_broad_allow(probe) == []


# --- 上下文预算 -------------------------------------------------------------
def test_over_budget_is_info():
    probe = make_probe(budget=100, assets={"memory": [
        {"scope": "user", "path": "/h/CLAUDE.md", "lines": 1, "bytes": 1,
         "tokens": 150, "imports": []}]})
    findings = check_context_budget(probe)
    assert [f.id for f in findings] == ["security.context-budget"]
    assert findings[0].severity == "info"


def test_under_budget_is_clean():
    probe = make_probe(budget=100, assets={"memory": [
        {"scope": "user", "path": "/h/CLAUDE.md", "lines": 1, "bytes": 1,
         "tokens": 50, "imports": []}]})
    assert check_context_budget(probe) == []


def test_budget_sums_across_all_memory_files():
    probe = make_probe(budget=100, assets={"memory": [
        {"scope": "user", "path": "/a", "lines": 1, "bytes": 1,
         "tokens": 60, "imports": []},
        {"scope": "project", "path": "/b", "lines": 1, "bytes": 1,
         "tokens": 60, "imports": []}]})
    assert [f.id for f in check_context_budget(probe)] == ["security.context-budget"]


def test_no_memory_is_clean():
    assert check_context_budget(make_probe(budget=100)) == []

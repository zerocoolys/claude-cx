"""conflicts 类检查：静默覆盖、遮蔽、重复定义。"""

from pathlib import Path

from cx.doctor.checks_conflicts import (
    check_deny_shadows_allow,
    check_legacy_local_settings,
    check_managed_override,
    check_mcp_name_collision,
    check_shadowed_keys,
)
from cx.model import Ctx, SourceFile

from .conftest import make_probe


def prov_chain(key, *scopes):
    return {key: [(s, Path(f"/{s}.json"), s) for s in scopes]}


# --- managed 覆盖 ------------------------------------------------------------
def test_managed_overriding_user_is_warn():
    probe = make_probe(prov=prov_chain("model", "user", "managed"))
    findings = check_managed_override(probe)
    assert [f.id for f in findings] == ["conflicts.managed-override"]
    assert findings[0].severity == "warn"
    assert findings[0].where == "model"


def test_managed_only_is_clean():
    """只有 managed 定义，没覆盖任何人，不报。"""
    assert check_managed_override(make_probe(prov=prov_chain("model", "managed"))) == []


def test_no_managed_is_clean():
    assert check_managed_override(make_probe(prov=prov_chain("model", "user"))) == []


# --- 普通遮蔽 ---------------------------------------------------------------
def test_multi_scope_key_is_info():
    probe = make_probe(prov=prov_chain("model", "user", "project"))
    findings = check_shadowed_keys(probe)
    assert [f.id for f in findings] == ["conflicts.shadowed-key"]
    assert findings[0].severity == "info"


def test_single_scope_key_is_clean():
    assert check_shadowed_keys(make_probe(prov=prov_chain("model", "user"))) == []


def test_managed_chain_is_left_to_the_other_check():
    """managed 覆盖由 check_managed_override 报，这里不重复报。"""
    probe = make_probe(prov=prov_chain("model", "user", "managed"))
    assert check_shadowed_keys(probe) == []


# --- deny 遮蔽 allow ---------------------------------------------------------
def test_identical_rule_in_allow_and_deny_is_warn():
    probe = make_probe(merged={"permissions": {
        "allow": ["Bash(rm -rf /)"], "deny": ["Bash(rm -rf /)"]}})
    findings = check_deny_shadows_allow(probe)
    assert [f.id for f in findings] == ["conflicts.deny-shadows-allow"]


def test_different_but_overlapping_rules_are_not_flagged():
    """宽窄重叠需要完整匹配语义才能判定，刻意不报，避免误报。"""
    probe = make_probe(merged={"permissions": {
        "allow": ["Bash(npm run test)"], "deny": ["Bash(npm *)"]}})
    assert check_deny_shadows_allow(probe) == []


def test_no_overlap_is_clean():
    probe = make_probe(merged={"permissions": {
        "allow": ["Bash(ls)"], "deny": ["Bash(rm)"]}})
    assert check_deny_shadows_allow(probe) == []


# --- 新旧 local settings 并存 ------------------------------------------------
def test_two_local_settings_files_is_warn():
    ctx = Ctx(cwd=Path("/p/sub"), repo_root=Path("/p"), home=Path("/h"))
    ctx.sources = [
        SourceFile(scope="local", path=Path("/p/sub/.claude/settings.local.json"),
                   exists=True, data={"model": "opus"}),
        SourceFile(scope="local", path=Path("/p/.claude/settings.local.json"),
                   exists=True, data={"model": "sonnet"}),
    ]
    findings = check_legacy_local_settings(make_probe(ctx=ctx))
    assert [f.id for f in findings] == ["conflicts.legacy-local-settings"]


def test_single_local_settings_file_is_clean():
    ctx = Ctx(cwd=Path("/p"), repo_root=Path("/p"), home=Path("/h"))
    ctx.sources = [
        SourceFile(scope="local", path=Path("/p/.claude/settings.local.json"),
                   exists=True, data={"model": "opus"}),
    ]
    assert check_legacy_local_settings(make_probe(ctx=ctx)) == []


# --- MCP 重名 ---------------------------------------------------------------
def test_same_mcp_name_in_two_scopes_is_warn():
    probe = make_probe(assets={"mcp": [
        {"name": "gh", "scope": "user", "source": "~/.claude.json", "spec": {}},
        {"name": "gh", "scope": "project", "source": "./.mcp.json", "spec": {}}]})
    findings = check_mcp_name_collision(probe)
    assert [f.id for f in findings] == ["conflicts.mcp-name-collision"]


def test_distinct_mcp_names_are_clean():
    probe = make_probe(assets={"mcp": [
        {"name": "gh", "scope": "user", "source": "x", "spec": {}},
        {"name": "jira", "scope": "project", "source": "y", "spec": {}}]})
    assert check_mcp_name_collision(probe) == []


def test_same_name_same_scope_is_not_a_collision():
    probe = make_probe(assets={"mcp": [
        {"name": "gh", "scope": "user", "source": "x", "spec": {}},
        {"name": "gh", "scope": "user", "source": "x", "spec": {}}]})
    assert check_mcp_name_collision(probe) == []

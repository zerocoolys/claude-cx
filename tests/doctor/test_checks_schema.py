"""schema 类检查：语法、层级、拼写、结构。"""

from pathlib import Path

from cx.doctor.checks_schema import (
    _distance_one,
    check_asset_frontmatter,
    check_hook_shape,
    check_json_syntax,
    check_likely_typos,
    check_misplaced_keys,
)
from cx.model import Ctx, SourceFile

from .conftest import make_probe


def ctx_with_sources(*sources):
    ctx = Ctx(cwd=Path("/p"), repo_root=None, home=Path("/h"))
    ctx.sources = list(sources)
    return ctx


# --- JSON 语法 ---------------------------------------------------------------
def test_broken_json_is_error():
    sf = SourceFile(scope="project", path=Path("/p/.claude/settings.json"),
                    exists=True, error="JSON 语法错误 (行 1 列 3): x")
    findings = check_json_syntax(make_probe(ctx=ctx_with_sources(sf)))
    assert [f.id for f in findings] == ["schema.json-syntax"]
    assert findings[0].severity == "error"


def test_valid_json_is_clean():
    sf = SourceFile(scope="project", path=Path("/p/.claude/settings.json"),
                    exists=True, data={"model": "opus"})
    assert check_json_syntax(make_probe(ctx=ctx_with_sources(sf))) == []


# --- 层级放错 ---------------------------------------------------------------
def test_top_level_default_mode_is_error():
    findings = check_misplaced_keys(make_probe(merged={"defaultMode": "plan"}))
    assert [f.id for f in findings] == ["schema.misplaced-key"]
    assert "permissions.defaultMode" in findings[0].fix


def test_top_level_allow_is_error():
    findings = check_misplaced_keys(make_probe(merged={"allow": ["Bash(ls)"]}))
    assert [f.id for f in findings] == ["schema.misplaced-key"]


def test_correctly_nested_permissions_are_clean():
    probe = make_probe(merged={"permissions": {"defaultMode": "plan",
                                               "allow": ["Bash(ls)"]}})
    assert check_misplaced_keys(probe) == []


# --- 编辑距离 ---------------------------------------------------------------
def test_distance_one_substitution():
    assert _distance_one("modle", "model") is False  # 换位不是距离 1 的替换
    assert _distance_one("modeX", "model") is True


def test_distance_one_insertion_and_deletion():
    assert _distance_one("modell", "model") is True
    assert _distance_one("mode", "model") is True


def test_distance_one_rejects_identical_and_far():
    assert _distance_one("model", "model") is False
    assert _distance_one("modelXyz", "model") is False
    assert _distance_one("", "model") is False


# --- 疑似拼写错误 ------------------------------------------------------------
def test_near_miss_top_level_key_is_warn():
    findings = check_likely_typos(make_probe(merged={"modeL": "opus"}))
    assert [f.id for f in findings] == ["schema.likely-typo"]
    assert findings[0].severity == "warn"
    assert "model" in findings[0].fix


def test_distant_unknown_key_is_silent():
    """距离 >= 2 的未知键更可能是新版本的新键，不报。"""
    assert check_likely_typos(make_probe(merged={"brandNewSetting": 1})) == []


def test_known_key_is_silent():
    assert check_likely_typos(make_probe(merged={"model": "opus"})) == []


def test_env_keys_are_exempt():
    """env 下是任意用户变量名，整体豁免。"""
    probe = make_probe(merged={"env": {"MODEL": "x", "modeL": "y"}})
    assert check_likely_typos(probe) == []


def test_hook_event_names_are_exempt():
    probe = make_probe(merged={"hooks": {"PreToolUsX": []}})
    assert check_likely_typos(probe) == []


def test_near_miss_inside_permissions_is_warn():
    probe = make_probe(merged={"permissions": {"alow": []}})
    findings = check_likely_typos(probe)
    assert [f.id for f in findings] == ["schema.likely-typo"]
    assert "permissions.alow" in findings[0].where


# --- frontmatter ------------------------------------------------------------
def test_asset_missing_description_is_warn():
    probe = make_probe(assets={"agents": [
        {"name": "a", "scope": "user", "path": "/h/.claude/agents/a.md",
         "description": "", "model": "", "tools": "", "bytes": 1, "tokens": 1}]})
    findings = check_asset_frontmatter(probe)
    assert [f.id for f in findings] == ["schema.asset-missing-frontmatter"]


def test_asset_with_full_frontmatter_is_clean():
    probe = make_probe(assets={"agents": [
        {"name": "a", "scope": "user", "path": "/h/.claude/agents/a.md",
         "description": "干活的", "model": "", "tools": "",
         "bytes": 1, "tokens": 1}]})
    assert check_asset_frontmatter(probe) == []


# --- hooks 结构 --------------------------------------------------------------
def test_hook_matcher_not_a_dict_is_error():
    probe = make_probe(merged={"hooks": {"PostToolUse": ["oops"]}})
    findings = check_hook_shape(probe)
    assert [f.id for f in findings] == ["schema.hook-malformed"]
    assert findings[0].severity == "error"


def test_hook_entry_without_command_is_error():
    probe = make_probe(merged={"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [{"type": "command"}]}]}})
    assert [f.id for f in check_hook_shape(probe)] == ["schema.hook-malformed"]


def test_well_formed_hooks_are_clean():
    probe = make_probe(merged={"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "echo"}]}]}})
    assert check_hook_shape(probe) == []


def test_url_type_hook_without_command_is_clean():
    """cx/render.py 一直把 command 或 url 都当作合法的 hook 载荷。"""
    probe = make_probe(merged={"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [
            {"type": "url", "url": "https://example.com"}]}]}})
    assert check_hook_shape(probe) == []


def test_hook_entry_not_a_dict_is_still_error():
    probe = make_probe(merged={"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": ["not-a-dict"]}]}})
    assert [f.id for f in check_hook_shape(probe)] == ["schema.hook-malformed"]


def test_hook_event_value_not_a_list_is_still_error():
    probe = make_probe(merged={"hooks": {"PostToolUse": "not-a-list"}})
    assert [f.id for f in check_hook_shape(probe)] == ["schema.hook-malformed"]


# --- 密钥不外泄 ---------------------------------------------------------------
def test_malformed_matcher_does_not_leak_secret_shaped_value():
    """matcher 项不是对象时，原本用 {m!r} 会把内容原样带出来。"""
    sentinel = "sk-ant-veryuniquesentinel123"
    # matcher 本身不是 dict（是个 list），触发 "matcher 项不是对象" 分支，
    # 但其内容里嵌了一个密钥形状的键值对。
    probe = make_probe(merged={"hooks": {"PostToolUse": [
        [{"apiKey": sentinel}]]}})
    findings = check_hook_shape(probe)
    assert findings, "expected a schema.hook-malformed finding"
    for f in findings:
        for field_val in (f.id, f.title, f.detail, f.where, f.fix):
            assert sentinel not in field_val

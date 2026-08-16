"""cx 的行为测试。重点覆盖合并语义——那是最容易写错也最难肉眼发现的部分。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cx  # noqa: E402


def write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """一个隔离的 home + project，避免碰到跑测试的人自己的 ~/.claude。"""
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    (proj / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home, proj


def build(home: Path, proj: Path):
    ctx = cx.Ctx(cwd=proj, repo_root=cx.find_repo_root(proj), home=home)
    cx.discover_sources(ctx)
    merged, prov = cx.merge_with_provenance(ctx.sources)
    return ctx, merged, prov


# ---------------------------------------------------------------------------
# 优先级
# ---------------------------------------------------------------------------
def test_local_beats_project_beats_user(env):
    home, proj = env
    write(home / ".claude" / "settings.json", {"model": "opus", "editorMode": "vim"})
    write(proj / ".claude" / "settings.json", {"model": "sonnet"})
    write(proj / ".claude" / "settings.local.json", {"model": "haiku"})

    ctx, merged, prov = build(home, proj)
    assert merged["model"] == "haiku"
    # user 独有的键不该被抹掉
    assert merged["editorMode"] == "vim"
    # 来源链完整保留，末位为生效值
    assert [e[0] for e in prov["model"]] == ["user", "project", "local"]


def test_managed_beats_everything(env, monkeypatch):
    home, proj = env
    managed = proj.parent / "managed"
    managed.mkdir()
    monkeypatch.setattr(cx.discovery, "managed_dirs", lambda: [managed])
    write(home / ".claude" / "settings.json", {"model": "opus"})
    write(proj / ".claude" / "settings.local.json", {"model": "haiku"})
    write(managed / "managed-settings.json", {"model": "sonnet"})

    _, merged, prov = build(home, proj)
    assert merged["model"] == "sonnet"
    assert cx.effective_scope(prov["model"]) == "managed"


def test_managed_dropin_merges_alphabetically(env, monkeypatch):
    home, proj = env
    managed = proj.parent / "managed"
    (managed / "managed-settings.d").mkdir(parents=True)
    monkeypatch.setattr(cx.discovery, "managed_dirs", lambda: [managed])
    write(managed / "managed-settings.json", {"model": "base"})
    write(managed / "managed-settings.d" / "10-a.json", {"model": "ten"})
    write(managed / "managed-settings.d" / "20-b.json", {"model": "twenty"})
    write(managed / "managed-settings.d" / ".hidden.json", {"model": "hidden"})

    _, merged, _ = build(home, proj)
    assert merged["model"] == "twenty"  # 字母序靠后的覆盖靠前的
    assert "hidden" not in json.dumps(merged)  # 点号开头的文件被忽略


# ---------------------------------------------------------------------------
# 合并语义的例外
# ---------------------------------------------------------------------------
def test_permission_rules_merge_across_scopes(env):
    home, proj = env
    write(home / ".claude" / "settings.json",
          {"permissions": {"allow": ["Bash(git status)"], "deny": ["Read(./.env)"]}})
    write(proj / ".claude" / "settings.json",
          {"permissions": {"allow": ["Bash(pytest *)"]}})

    _, merged, _ = build(home, proj)
    # 累加而非覆盖：三条规则同时生效
    assert set(merged["permissions"]["allow"]) == {"Bash(git status)", "Bash(pytest *)"}
    assert merged["permissions"]["deny"] == ["Read(./.env)"]


def test_array_merge_dedupes(env):
    home, proj = env
    write(home / ".claude" / "settings.json", {"permissions": {"allow": ["Bash(ls)"]}})
    write(proj / ".claude" / "settings.json", {"permissions": {"allow": ["Bash(ls)"]}})

    _, merged, _ = build(home, proj)
    assert merged["permissions"]["allow"] == ["Bash(ls)"]


def test_fallback_model_replaces_whole_chain(env):
    home, proj = env
    write(home / ".claude" / "settings.json", {"fallbackModel": ["a", "b"]})
    write(proj / ".claude" / "settings.json", {"fallbackModel": ["c"]})

    _, merged, _ = build(home, proj)
    # 这个键不拼接，高优先级文件提供整条链
    assert merged["fallbackModel"] == ["c"]


def test_nested_objects_deep_merge(env):
    home, proj = env
    write(home / ".claude" / "settings.json", {"env": {"A": "1", "B": "2"}})
    write(proj / ".claude" / "settings.json", {"env": {"B": "override", "C": "3"}})

    _, merged, _ = build(home, proj)
    assert merged["env"] == {"A": "1", "B": "override", "C": "3"}


# ---------------------------------------------------------------------------
# 故障可见性
# ---------------------------------------------------------------------------
def test_broken_json_is_reported_not_swallowed(env):
    home, proj = env
    write(proj / ".claude" / "settings.json", '{"model": "opus",}')

    ctx, merged, _ = build(home, proj)
    assert any("JSON 语法错误" in p for p in ctx.problems)
    assert "model" not in merged  # 整个文件被忽略，不是部分生效
    bad = [s for s in ctx.sources if s.path.name == "settings.json" and s.scope == "project"]
    assert bad[0].error is not None


def test_empty_file_is_not_an_error(env):
    home, proj = env
    (proj / ".claude" / "settings.json").write_text("", encoding="utf-8")
    ctx, _, _ = build(home, proj)
    assert ctx.problems == []


def test_missing_files_are_silent(env):
    home, proj = env
    ctx, merged, _ = build(home, proj)
    assert ctx.problems == []
    assert merged == {}


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
def test_secrets_redacted_by_default():
    out = cx.redact({"ANTHROPIC_API_KEY": "sk-ant-abcdefghijklmnop"}, show=False)
    assert "abcdefghijklmnop" not in json.dumps(out)
    assert "chars" in out["ANTHROPIC_API_KEY"]


def test_short_secrets_fully_masked():
    assert cx.redact({"token": "abc"}, show=False)["token"] == "••••"


def test_non_secret_keys_untouched():
    assert cx.redact({"model": "opus"}, show=False)["model"] == "opus"


def test_show_secrets_bypasses_redaction():
    out = cx.redact({"api_key": "sk-ant-abcdefghijklmnop"}, show=True)
    assert out["api_key"] == "sk-ant-abcdefghijklmnop"


def test_redaction_recurses_into_nested_structures():
    out = cx.redact({"mcpServers": {"x": {"env": {"GITHUB_TOKEN": "ghp_abcdefghijkl"}}}}, show=False)
    assert "ghp_abcdefghijkl" not in json.dumps(out)


# ---------------------------------------------------------------------------
# MCP 审批状态
# ---------------------------------------------------------------------------
def test_mcp_project_servers_need_approval(env):
    home, proj = env
    write(proj / ".mcp.json", {"mcpServers": {
        "approved": {"type": "http", "url": "https://a"},
        "pending": {"type": "http", "url": "https://b"},
        "rejected": {"type": "http", "url": "https://c"},
    }})
    ctx = cx.Ctx(cwd=proj, repo_root=None, home=home)
    merged = {"enabledMcpjsonServers": ["approved"], "disabledMcpjsonServers": ["rejected"]}
    by_name = {s["name"]: s for s in cx.scan_mcp(ctx, merged)}

    assert by_name["approved"]["status"] == "已批准"
    assert by_name["pending"]["status"] == "待批准"
    assert "拒绝" in by_name["rejected"]["status"]


def test_enable_all_project_mcp_servers(env):
    home, proj = env
    write(proj / ".mcp.json", {"mcpServers": {"x": {"url": "https://a"}}})
    ctx = cx.Ctx(cwd=proj, repo_root=None, home=home)
    servers = cx.scan_mcp(ctx, {"enableAllProjectMcpServers": True})
    assert servers[0]["status"] == "已批准"


def test_mcp_scopes_are_distinguished(env):
    home, proj = env
    write(home / ".claude.json", {
        "mcpServers": {"user-level": {"command": "foo"}},
        "projects": {str(proj): {"mcpServers": {"local-level": {"command": "bar"}}}},
    })
    write(proj / ".mcp.json", {"mcpServers": {"project-level": {"url": "https://x"}}})
    ctx = cx.Ctx(cwd=proj, repo_root=None, home=home)
    scopes = {s["name"]: s["scope"] for s in cx.scan_mcp(ctx, {})}

    assert scopes == {"user-level": "user", "local-level": "local", "project-level": "project"}


# ---------------------------------------------------------------------------
# 资产扫描
# ---------------------------------------------------------------------------
def test_frontmatter_parsed_from_agents(env):
    home, proj = env
    d = proj / ".claude" / "agents"
    d.mkdir(parents=True)
    (d / "r.md").write_text("---\nname: researcher\ndescription: 市场扫描\nmodel: haiku\n---\n正文",
                            encoding="utf-8")
    ctx = cx.Ctx(cwd=proj, repo_root=None, home=home)
    agents = cx.scan_md_assets(ctx, "agents")

    assert agents[0]["name"] == "researcher"
    assert agents[0]["description"] == "市场扫描"
    assert agents[0]["model"] == "haiku"
    assert agents[0]["scope"] == "project"


def test_memory_layers_and_imports(env):
    home, proj = env
    (home / ".claude" / "CLAUDE.md").write_text("user memory", encoding="utf-8")
    (proj / "CLAUDE.md").write_text("proj memory\n@docs/arch.md\n", encoding="utf-8")
    ctx = cx.Ctx(cwd=proj, repo_root=proj, home=home)
    mem = {m["scope"]: m for m in cx.scan_memory(ctx)}

    assert set(mem) == {"user", "project"}
    assert mem["project"]["imports"] == ["docs/arch.md"]


# ---------------------------------------------------------------------------
# CLI 冒烟
# ---------------------------------------------------------------------------
def test_json_output_is_valid(env, tmp_path):
    home, proj = env
    write(proj / ".claude" / "settings.json", {"model": "sonnet"})
    repo_root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, "-m", "cx", "--path", str(proj), "--json"],
        cwd=str(repo_root),
        capture_output=True, text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["effective"]["model"] == "sonnet"
    assert payload["cwd"] == str(proj)


def test_nonexistent_path_exits_nonzero(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, "-m", "cx", "--path", str(tmp_path / "nope")],
        cwd=str(repo_root),
        capture_output=True, text=True,
    )
    assert r.returncode == 2


def test_report_renders_without_crashing(env, capsys):
    home, proj = env
    write(home / ".claude" / "settings.json",
          {"model": "opus", "env": {"KEY": "secret-value-here"},
           "permissions": {"allow": ["Bash(ls)"]},
           "hooks": {"PreToolUse": [{"matcher": "Bash",
                                     "hooks": [{"type": "command", "command": "./g.sh"}]}]}})
    ctx, merged, prov = build(home, proj)
    assets = {"memory": [], "agents": [], "commands": [], "skills": [],
              "mcp": [], "plugins": [], "gitignore": None}
    cx.render(ctx, merged, prov, assets, set(cx.ALL_SECTIONS))
    out = capsys.readouterr().out

    assert "PreToolUse" in out
    assert "secret-value-here" not in out  # 报告里也要脱敏


def test_disp_width_counts_cjk_as_two():
    assert cx.disp_width("abc") == 3
    assert cx.disp_width("中文") == 4
    assert cx.disp_width("a中") == 3

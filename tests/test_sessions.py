"""会话模型用量统计。

重点是三处容易算错的地方：按 message.id 去重（同一次响应会写多行）、
排除本地合成的错误记录、以及目录名编码不可逆导致的邻居项目误配。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cx.cli import main  # noqa: E402
from cx.model import Ctx  # noqa: E402
from cx.sessions import (  # noqa: E402
    UNNAMED_AGENT,
    collect_usage,
    encode_project_dir,
    parse_session,
    session_dirs,
    usage_payload,
)


def assistant(msg_id: str, model: str, *, inp=0, out=0, cw=0, cr=0,
              cwd="/proj", ts="2026-08-16T00:00:00.000Z", **extra) -> dict:
    return {
        "type": "assistant",
        "uuid": f"u-{msg_id}",
        "timestamp": ts,
        "cwd": cwd,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cw,
                "cache_read_input_tokens": cr,
            },
        },
        **extra,
    }


def sub_assistant(msg_id: str, model: str, agent: str | None, **kw) -> dict:
    """子 agent 记录：isSidechain=true，agent 名在 attributionAgent 上。"""
    rec = assistant(msg_id, model, **kw)
    rec["isSidechain"] = True
    if agent is not None:
        rec["attributionAgent"] = agent
    return rec


def _dump(path: Path, session_id: str, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps({"sessionId": session_id, **r}, ensure_ascii=False) + "\n"
                for r in records),
        encoding="utf-8",
    )
    return path


def write_session(home: Path, cwd: Path, session_id: str, records) -> Path:
    d = home / ".claude" / "projects" / encode_project_dir(cwd)
    return _dump(d / f"{session_id}.jsonl", session_id, records)


def write_subagent(home: Path, cwd: Path, session_id: str, agent_id: str,
                   records) -> Path:
    """子 agent 的记录写在 <sessionId>/subagents/agent-<id>.jsonl。"""
    d = home / ".claude" / "projects" / encode_project_dir(cwd) / session_id
    return _dump(d / "subagents" / f"agent-{agent_id}.jsonl", session_id, records)


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)

    def ctx() -> Ctx:
        return Ctx(cwd=proj, repo_root=proj, home=home)

    return home, proj, ctx


# --- 路径编码 ---------------------------------------------------------------
def test_encode_replaces_every_non_alnum_char():
    assert encode_project_dir("/Users/me/unknown_ws/claude-cx") == \
        "-Users-me-unknown-ws-claude-cx"


def test_encode_maps_dot_dirs_like_claude_code_does():
    assert encode_project_dir("/a/b/.claude/worktrees/x") == \
        "-a-b--claude-worktrees-x"


# --- 单文件解析 -------------------------------------------------------------
def test_same_message_id_counted_once(env):
    """一次响应会分多行写入，每行带同一份累计 usage——按行加会翻倍。"""
    home, proj, ctx = env
    f = write_session(home, proj, "s1", [
        assistant("m1", "claude-opus-5", inp=10, out=20, cwd=str(proj)),
        assistant("m1", "claude-opus-5", inp=10, out=20, cwd=str(proj)),
    ])
    s = parse_session(f)
    assert len(s.messages) == 1
    assert s.messages[0].output == 20


def test_last_usage_wins_for_a_message_id(env):
    home, proj, ctx = env
    f = write_session(home, proj, "s1", [
        assistant("m1", "claude-opus-5", out=5, cwd=str(proj)),
        assistant("m1", "claude-opus-5", out=42, cwd=str(proj)),
    ])
    assert parse_session(f).messages[0].output == 42


def test_synthetic_and_api_error_records_excluded(env):
    home, proj, ctx = env
    f = write_session(home, proj, "s1", [
        assistant("m0", "<synthetic>", out=999, cwd=str(proj)),
        assistant("m1", "claude-opus-5", out=999, cwd=str(proj),
                  isApiErrorMessage=True),
        assistant("m2", "claude-opus-5", out=7, cwd=str(proj)),
    ])
    s = parse_session(f)
    assert [m.model for m in s.messages] == ["claude-opus-5"]
    assert s.messages[0].output == 7


def test_non_assistant_records_ignored(env):
    home, proj, ctx = env
    f = write_session(home, proj, "s1", [
        {"type": "user", "cwd": str(proj), "message": {"role": "user"}},
        {"type": "file-history-snapshot", "snapshot": {}},
        assistant("m1", "claude-opus-5", out=3, cwd=str(proj)),
    ])
    assert len(parse_session(f).messages) == 1


def test_truncated_line_does_not_abort_parsing(env):
    """会话文件可能正在被写入，最后一行常是半截 JSON。"""
    home, proj, ctx = env
    f = write_session(home, proj, "s1", [
        assistant("m1", "claude-opus-5", out=3, cwd=str(proj)),
    ])
    with f.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","mess')
    assert len(parse_session(f).messages) == 1


# --- 聚合 -------------------------------------------------------------------
def test_aggregates_per_model_across_sessions(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant("a1", "claude-opus-5", inp=1, out=2, cw=3, cr=4, cwd=str(proj)),
        assistant("a2", "claude-sonnet-5", inp=10, out=20, cwd=str(proj)),
    ])
    write_session(home, proj, "s2", [
        assistant("b1", "claude-opus-5", inp=100, out=200, cwd=str(proj)),
    ])
    usage = collect_usage(ctx())
    by_model = {m.model: m for m in usage.models}

    assert by_model["claude-opus-5"].sessions == 2
    assert by_model["claude-opus-5"].messages == 2
    assert by_model["claude-opus-5"].input == 101
    assert by_model["claude-opus-5"].cache_read == 4
    assert by_model["claude-sonnet-5"].sessions == 1
    assert usage.total.messages == 3
    assert usage.total.sessions == 2
    assert usage.total.total == sum(m.total for m in usage.models)


def test_models_sorted_by_total_tokens_desc(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant("a", "small-model", out=1, cwd=str(proj)),
        assistant("b", "big-model", out=1000, cwd=str(proj)),
    ])
    assert [m.model for m in collect_usage(ctx()).models] == \
        ["big-model", "small-model"]


def test_time_span_spans_all_sessions(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant("a", "m", out=1, cwd=str(proj), ts="2026-08-10T00:00:00Z"),
    ])
    write_session(home, proj, "s2", [
        assistant("b", "m", out=1, cwd=str(proj), ts="2026-08-14T00:00:00Z"),
    ])
    usage = collect_usage(ctx())
    assert usage.total.first.startswith("2026-08-10")
    assert usage.total.last.startswith("2026-08-14")


def test_no_sessions_yields_empty_usage(env):
    home, proj, ctx = env
    usage = collect_usage(ctx())
    assert usage.models == ()
    assert usage.total.messages == 0


# --- 子 agent / 明细 --------------------------------------------------------
def test_subagent_transcript_merged_into_its_session(env):
    """子 agent 的记录单独成文件，但属于同一个会话，不能算成两个会话。"""
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant("a", "m", out=10, cwd=str(proj)),
    ])
    write_subagent(home, proj, "s1", "aaa", [
        sub_assistant("b", "m", "Explore", out=3, cwd=str(proj)),
    ])
    usage = collect_usage(ctx())
    assert len(usage.sessions) == 1
    assert usage.total.messages == 2
    assert usage.total.output == 13


def test_agent_usage_split_from_main_line(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant("a", "m", out=10, cwd=str(proj)),
    ])
    write_subagent(home, proj, "s1", "aaa", [
        sub_assistant("b", "m", "Explore", out=3, cwd=str(proj)),
    ])
    usage = collect_usage(ctx())
    assert usage.main.messages == 1 and usage.main.output == 10
    assert usage.sub.messages == 1 and usage.sub.output == 3


def test_usage_aggregated_per_agent_name(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [assistant("a", "m", out=1, cwd=str(proj))])
    write_subagent(home, proj, "s1", "aaa", [
        sub_assistant("b", "m", "Explore", out=5, cwd=str(proj)),
        sub_assistant("c", "m", "Explore", out=5, cwd=str(proj)),
    ])
    write_subagent(home, proj, "s1", "bbb", [
        sub_assistant("d", "m", "code-reviewer", out=100, cwd=str(proj)),
    ])
    agents = {a.model: a for a in collect_usage(ctx()).agents}
    assert agents["Explore"].messages == 2 and agents["Explore"].output == 10
    assert agents["code-reviewer"].messages == 1
    # 总量降序
    assert [a.model for a in collect_usage(ctx()).agents] == \
        ["code-reviewer", "Explore"]


def test_subagent_without_attribution_gets_placeholder(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [assistant("a", "m", out=1, cwd=str(proj))])
    write_subagent(home, proj, "s1", "aaa", [
        sub_assistant("b", "m", None, out=2, cwd=str(proj)),
    ])
    assert collect_usage(ctx()).agents[0].model == UNNAMED_AGENT


def test_main_line_messages_carry_no_agent(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [assistant("a", "m", out=1, cwd=str(proj))])
    usage = collect_usage(ctx())
    assert usage.agents == ()
    assert usage.sub.messages == 0
    assert all(not m.agent for m in usage.sessions[0].messages)


def test_session_records_branch_and_cli_version(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        dict(assistant("a", "m", out=1, cwd=str(proj)),
             gitBranch="feature/x", version="2.1.229"),
    ])
    s = collect_usage(ctx()).sessions[0]
    assert s.branch == "feature/x"
    assert s.cli_version == "2.1.229"


def test_detail_payload_carries_per_session_rows(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant("a", "claude-opus-5", out=5, cwd=str(proj)),
    ])
    write_subagent(home, proj, "s1", "aaa", [
        sub_assistant("b", "claude-sonnet-5", "Explore", out=2, cwd=str(proj)),
    ])
    usage = collect_usage(ctx())
    lean = usage_payload(usage)
    full = usage_payload(usage, detail=True)

    assert "session_detail" not in lean
    assert full["agents"][0]["agent"] == "Explore"
    assert full["sub_agent"]["total_tokens"] == 2
    row = full["session_detail"][0]
    assert row["session_id"] == "s1"
    assert row["total_tokens"] == 7
    assert len(row["files"]) == 2


# --- 目录归属 ---------------------------------------------------------------
def test_worktree_sessions_counted_under_the_project(env):
    home, proj, ctx = env
    wt = proj / ".claude" / "worktrees" / "feature-x"
    write_session(home, proj, "s1", [
        assistant("a", "m", out=1, cwd=str(proj)),
    ])
    write_session(home, wt, "s2", [
        assistant("b", "m", out=1, cwd=str(wt)),
    ])
    usage = collect_usage(ctx())
    assert len(usage.dirs) == 2
    assert usage.total.messages == 2


def test_sibling_project_with_same_encoded_prefix_excluded(env):
    """/proj 与 /proj-other 的编码前缀相同，必须靠记录里的 cwd 排除。"""
    home, proj, ctx = env
    sibling = proj.parent / (proj.name + "-other")
    write_session(home, proj, "s1", [
        assistant("a", "m", out=1, cwd=str(proj)),
    ])
    write_session(home, sibling, "s2", [
        assistant("b", "m", out=99, cwd=str(sibling)),
    ])
    usage = collect_usage(ctx())
    assert usage.total.messages == 1
    assert usage.total.output == 1


def test_session_dirs_ignores_unrelated_projects(env):
    home, proj, ctx = env
    other = proj.parent / "unrelated"
    write_session(home, other, "s1", [assistant("a", "m", cwd=str(other))])
    assert session_dirs(ctx()) == []


# --- CLI --------------------------------------------------------------------
def test_cli_model_json(env, monkeypatch, capsys):
    home, proj, ctx = env
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    write_session(home, proj, "s1", [
        assistant("a", "claude-opus-5", inp=5, out=6, cwd=str(proj)),
    ])
    assert main(["model", "--path", str(proj), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"] == 1
    assert payload["models"][0]["model"] == "claude-opus-5"
    assert payload["total"]["total_tokens"] == 11


def test_cli_model_renders_table(env, monkeypatch, capsys):
    home, proj, ctx = env
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    write_session(home, proj, "s1", [
        assistant("a", "claude-opus-5", out=1234, cwd=str(proj)),
    ])
    assert main(["model", "--path", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "claude-opus-5" in out
    assert "1.2k" in out


def test_cli_model_detail_shows_agents_and_sessions(env, monkeypatch, capsys):
    home, proj, ctx = env
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    write_session(home, proj, "abcdef12", [
        assistant("a", "claude-opus-5", out=10, cwd=str(proj)),
    ])
    write_subagent(home, proj, "abcdef12", "aaa", [
        sub_assistant("b", "claude-opus-5", "code-reviewer", out=4, cwd=str(proj)),
    ])
    assert main(["model", "--path", str(proj), "--detail"]) == 0
    out = capsys.readouterr().out
    assert "子 agent" in out
    assert "code-reviewer" in out
    assert "abcdef12" in out


def test_cli_model_without_detail_stays_lean(env, monkeypatch, capsys):
    home, proj, ctx = env
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    write_session(home, proj, "abcdef12", [
        assistant("a", "claude-opus-5", out=10, cwd=str(proj)),
    ])
    write_subagent(home, proj, "abcdef12", "aaa", [
        sub_assistant("b", "claude-opus-5", "code-reviewer", out=4, cwd=str(proj)),
    ])
    assert main(["model", "--path", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "code-reviewer" not in out
    assert "abcdef12" not in out


def test_cli_model_without_history_is_not_an_error(env, monkeypatch, capsys):
    home, proj, ctx = env
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert main(["model", "--path", str(proj)]) == 0
    assert "没有找到" in capsys.readouterr().out

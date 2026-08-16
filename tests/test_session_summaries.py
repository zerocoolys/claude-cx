"""Sessions tab 用的实时汇总：session_active / collect_session_summaries / 时间线过滤。"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cx.model import Ctx  # noqa: E402
from cx.sessions import (  # noqa: E402
    ACTIVE_WINDOW_SECONDS,
    MAX_SERIES_POINTS,
    collect_debug_log,
    collect_session_summaries,
    encode_project_dir,
    session_active,
    session_timeline_payload,
)


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


def write_subagent(home: Path, cwd: Path, session_id: str, agent_id: str, records) -> Path:
    d = home / ".claude" / "projects" / encode_project_dir(cwd) / session_id
    return _dump(d / "subagents" / f"agent-{agent_id}.jsonl", session_id, records)


def assistant_record(*, cwd, ts="2026-08-16T00:00:00.000Z", model="claude-opus-5",
                     stop_reason="end_turn", request_id="req_1",
                     content=None, msg_id=None, inp=0, out=0, agent=None) -> dict:
    msg = {
        "id": msg_id or f"m-{ts}-{request_id}",
        "model": model,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": inp, "output_tokens": out,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        "content": content or [{"type": "text", "text": "hi"}],
    }
    rec = {
        "type": "assistant",
        "timestamp": ts,
        "cwd": str(cwd),
        "requestId": request_id,
        "message": msg,
    }
    if agent is not None:
        rec["isSidechain"] = True
        rec["attributionAgent"] = agent
    return rec


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)

    def ctx() -> Ctx:
        return Ctx(cwd=proj, repo_root=proj, home=home)

    return home, proj, ctx


NOW = datetime(2026, 8, 16, 0, 10, 0, tzinfo=timezone.utc)


# --- session_active -----------------------------------------------------------

def test_session_active_true_within_window():
    ts = "2026-08-16T00:09:00.000Z"  # 60s before NOW
    assert session_active(ts, now=NOW) is True


def test_session_active_false_outside_window():
    ts = "2026-08-16T00:00:00.000Z"  # 600s before NOW, window is 300s
    assert session_active(ts, now=NOW) is False


def test_session_active_false_for_empty_timestamp():
    assert session_active("", now=NOW) is False
    assert session_active(None, now=NOW) is False


def test_active_window_boundary_is_default_300s():
    assert ACTIVE_WINDOW_SECONDS == 300


# --- collect_session_summaries -------------------------------------------------

def test_summary_reports_active_and_idle_sessions(env):
    home, proj, ctx = env
    write_session(home, proj, "s-active", [
        assistant_record(cwd=proj, ts="2026-08-16T00:09:30.000Z", request_id="req_a"),
    ])
    write_session(home, proj, "s-idle", [
        assistant_record(cwd=proj, ts="2026-08-16T00:00:00.000Z", request_id="req_b"),
    ])
    summaries = {s.session_id: s for s in collect_session_summaries(ctx(), now=NOW)}
    assert summaries["s-active"].active is True
    assert summaries["s-idle"].active is False


def test_summary_aggregates_messages_tokens_and_last_tool(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts="2026-08-16T00:09:00.000Z", request_id="req_1",
                         inp=5, out=5, stop_reason="tool_use",
                         content=[{"type": "tool_use", "name": "Read", "input": {}}]),
        assistant_record(cwd=proj, ts="2026-08-16T00:09:30.000Z", request_id="req_2",
                         inp=10, out=10, stop_reason="end_turn",
                         content=[{"type": "tool_use", "name": "Edit", "input": {}}]),
    ])
    [summary] = collect_session_summaries(ctx(), now=NOW)
    assert summary.messages == 2
    assert summary.total_tokens == 30
    assert summary.last_tool == "Edit"
    assert summary.last_stop_reason == "end_turn"
    assert summary.last_agent == ""


def test_summary_includes_subagent_tree(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts="2026-08-16T00:09:00.000Z", request_id="req_main"),
    ])
    write_subagent(home, proj, "s1", "a1", [
        assistant_record(cwd=proj, ts="2026-08-16T00:09:10.000Z", request_id="req_sub",
                         inp=2, out=3, agent="planner"),
    ])
    [summary] = collect_session_summaries(ctx(), now=NOW)
    assert len(summary.agents) == 1
    assert summary.agents[0].name == "planner"
    assert summary.agents[0].messages == 1
    assert summary.agents[0].tokens == 5


def test_summary_sorted_by_last_activity_descending(env):
    home, proj, ctx = env
    write_session(home, proj, "s-older", [
        assistant_record(cwd=proj, ts="2026-08-16T00:00:00.000Z", request_id="req_1"),
    ])
    write_session(home, proj, "s-newer", [
        assistant_record(cwd=proj, ts="2026-08-16T00:09:00.000Z", request_id="req_2"),
    ])
    summaries = collect_session_summaries(ctx(), now=NOW)
    assert [s.session_id for s in summaries] == ["s-newer", "s-older"]


def test_token_series_downsamples_but_keeps_first_and_last_point(env):
    home, proj, ctx = env
    n = MAX_SERIES_POINTS + 20
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts=f"2026-08-16T00:{i:02d}:00.000Z",
                         request_id=f"req_{i}", inp=1, out=0)
        for i in range(n)
    ])
    [summary] = collect_session_summaries(ctx(), now=NOW)
    assert len(summary.token_series) <= MAX_SERIES_POINTS + 1
    assert summary.token_series[0][0] == "2026-08-16T00:00:00.000Z"
    assert summary.token_series[-1][1] == n  # 累计 token = 消息数（每条 input=1）


def test_no_sessions_returns_empty_summaries(env):
    _, _, ctx = env
    assert collect_session_summaries(ctx(), now=NOW) == []


# --- session_timeline_payload ---------------------------------------------------

def test_timeline_scoped_to_one_session(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts="2026-08-16T00:01:00.000Z", request_id="req_s1"),
    ])
    write_session(home, proj, "s2", [
        assistant_record(cwd=proj, ts="2026-08-16T00:02:00.000Z", request_id="req_s2"),
    ])
    payload = session_timeline_payload(ctx(), "s1")
    assert [e["request_id"] for e in payload["entries"]] == ["req_s1"]


def test_timeline_orders_ascending(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts="2026-08-16T00:03:00.000Z", request_id="req_3"),
        assistant_record(cwd=proj, ts="2026-08-16T00:01:00.000Z", request_id="req_1"),
    ])
    payload = session_timeline_payload(ctx(), "s1")
    assert [e["request_id"] for e in payload["entries"]] == ["req_1", "req_3"]


def test_timeline_unknown_session_id_returns_empty(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [assistant_record(cwd=proj)])
    payload = session_timeline_payload(ctx(), "does-not-exist")
    assert payload["entries"] == []


# --- collect_debug_log(limit=None) ----------------------------------------------

def test_collect_debug_log_limit_none_returns_everything(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts=f"2026-08-16T00:00:{i:02d}.000Z", request_id=f"req_{i}")
        for i in range(1, 6)
    ])
    entries = collect_debug_log(ctx(), limit=None)
    assert len(entries) == 5


def test_collect_debug_log_limit_none_with_after_ts_returns_everything(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts=f"2026-08-16T00:00:{i:02d}.000Z", request_id=f"req_{i}")
        for i in range(1, 6)
    ])
    entries = collect_debug_log(ctx(), limit=None, after_ts="2026-08-16T00:00:00.000Z")
    assert len(entries) == 5

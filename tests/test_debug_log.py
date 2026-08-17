"""cx debug —— jsonl 里 token 用量之外的调试字段。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cx.model import Ctx  # noqa: E402
from cx.sessions import (  # noqa: E402
    collect_debug_log,
    debug_log_payload,
    encode_project_dir,
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


def assistant_record(*, cwd, ts="2026-08-16T00:00:00.000Z", model="claude-opus-5",
                     stop_reason="end_turn", request_id="req_1", effort=None,
                     content=None, is_error=False, extra_message=None) -> dict:
    msg = {
        "id": f"m-{ts}",
        "model": model,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 1, "output_tokens": 1,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        "content": content or [{"type": "text", "text": "hi"}],
    }
    if extra_message:
        msg.update(extra_message)
    rec = {
        "type": "assistant",
        "timestamp": ts,
        "cwd": str(cwd),
        "requestId": request_id,
        "message": msg,
    }
    if effort is not None:
        rec["effort"] = effort
    if is_error:
        rec["isApiErrorMessage"] = True
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


def test_collects_stop_reason_and_request_id(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, stop_reason="tool_use", request_id="req_abc"),
    ])
    entries = collect_debug_log(ctx())
    assert len(entries) == 1
    assert entries[0].stop_reason == "tool_use"
    assert entries[0].request_id == "req_abc"


def test_extracts_tool_use_names(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, content=[
            {"type": "text", "text": "thinking"},
            {"type": "tool_use", "name": "Read", "input": {}},
            {"type": "tool_use", "name": "Edit", "input": {}},
        ]),
    ])
    entries = collect_debug_log(ctx())
    assert entries[0].tool_uses == ("Read", "Edit")


def test_synthetic_model_counts_as_error(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, model="<synthetic>",
                         content=[{"type": "text", "text": "Failed to connect"}]),
    ])
    entries = collect_debug_log(ctx())
    assert entries[0].is_error is True
    assert "Failed to connect" in entries[0].error_text


def test_real_api_error_message_is_flagged(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, is_error=True,
                         content=[{"type": "text", "text": "rate limited"}]),
    ])
    entries = collect_debug_log(ctx())
    assert entries[0].is_error is True
    assert entries[0].error_text == "rate limited"


def test_non_error_entries_have_no_error_text(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, content=[{"type": "text", "text": "should not leak"}]),
    ])
    entries = collect_debug_log(ctx())
    assert entries[0].is_error is False
    assert entries[0].error_text == ""


def test_sorted_newest_first_and_limit_applies(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts="2026-08-16T00:00:01.000Z", request_id="req_1"),
        assistant_record(cwd=proj, ts="2026-08-16T00:00:03.000Z", request_id="req_3"),
        assistant_record(cwd=proj, ts="2026-08-16T00:00:02.000Z", request_id="req_2"),
    ])
    entries = collect_debug_log(ctx(), limit=2)
    assert [e.request_id for e in entries] == ["req_3", "req_2"]


def test_neighbor_project_with_shared_encoding_prefix_is_excluded(env):
    """/proj 与 /proj-other 编码前缀相同，必须靠记录里的 cwd 排除邻居项目。"""
    home, proj, ctx = env
    other = proj.parent / (proj.name + "-other")
    write_session(home, other, "s-other", [
        assistant_record(cwd=other, request_id="req_other"),
    ])
    entries = collect_debug_log(ctx())
    assert entries == []


def test_debug_log_payload_shape(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [assistant_record(cwd=proj)])
    payload = debug_log_payload(collect_debug_log(ctx()))
    assert "entries" in payload
    assert payload["entries"][0]["request_id"] == "req_1"
    assert isinstance(payload["entries"][0]["tool_uses"], list)


def test_no_sessions_returns_empty_list(env):
    _, _, ctx = env
    assert collect_debug_log(ctx()) == []


# --- after_ts 增量轮询（--follow / dashboard 用）------------------------------
def test_after_ts_returns_only_strictly_newer_entries(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts="2026-08-16T00:00:01.000Z", request_id="req_1"),
        assistant_record(cwd=proj, ts="2026-08-16T00:00:02.000Z", request_id="req_2"),
        assistant_record(cwd=proj, ts="2026-08-16T00:00:03.000Z", request_id="req_3"),
    ])
    fresh = collect_debug_log(ctx(), after_ts="2026-08-16T00:00:01.000Z")
    assert [e.request_id for e in fresh] == ["req_2", "req_3"]


def test_after_ts_orders_ascending(env):
    """轮询/追加场景要按时间正序，不是默认那套倒序。"""
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts="2026-08-16T00:00:03.000Z", request_id="req_3"),
        assistant_record(cwd=proj, ts="2026-08-16T00:00:02.000Z", request_id="req_2"),
    ])
    fresh = collect_debug_log(ctx(), after_ts="2026-08-16T00:00:00.000Z")
    assert [e.request_id for e in fresh] == ["req_2", "req_3"]


def test_after_ts_empty_string_returns_everything(env):
    """dashboard 首次轮询、还没见过任何记录时用空串当游标。"""
    home, proj, ctx = env
    write_session(home, proj, "s1", [assistant_record(cwd=proj)])
    assert len(collect_debug_log(ctx(), after_ts="")) == 1


def test_after_ts_respects_limit(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts=f"2026-08-16T00:00:0{i}.000Z", request_id=f"req_{i}")
        for i in range(1, 6)
    ])
    fresh = collect_debug_log(ctx(), limit=2, after_ts="2026-08-16T00:00:00.000Z")
    assert [e.request_id for e in fresh] == ["req_1", "req_2"]


def test_entry_has_stable_id_from_uuid(env):
    home, proj, ctx = env
    d = home / ".claude" / "projects" / encode_project_dir(proj)
    d.mkdir(parents=True)
    rec = assistant_record(cwd=proj, request_id="req_1")
    rec["uuid"] = "fixed-uuid-123"
    _dump(d / "s1.jsonl", "s1", [rec])
    entries = collect_debug_log(ctx())
    assert entries[0].id == "fixed-uuid-123"


# --- token 用量与花费 --------------------------------------------------------
def test_entry_carries_token_usage_and_cost(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, model="claude-sonnet-5", extra_message={
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "cache_creation_input_tokens": 10, "cache_read_input_tokens": 20},
        }),
    ])
    e = collect_debug_log(ctx())[0]
    assert e.input_tokens == 100
    assert e.output_tokens == 50
    assert e.cache_creation_tokens == 10
    assert e.cache_read_tokens == 20
    assert e.cost_usd is not None and e.cost_usd > 0


def test_unpriced_model_has_no_cost(env):
    home, proj, ctx = env
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, model="claude-fable-5"),
    ])
    assert collect_debug_log(ctx())[0].cost_usd is None


def test_split_response_chunks_merge_into_one_entry(env):
    """同一 message.id 的多个分块（思考 + 工具调用）应合并成一条记录，
    而不是把同一份 usage 重复展示两次。"""
    home, proj, ctx = env
    ts = "2026-08-16T00:00:01.000Z"
    shared_msg = {"id": "shared-msg-1"}
    write_session(home, proj, "s1", [
        assistant_record(cwd=proj, ts=ts, content=[{"type": "thinking", "text": "..."}],
                         extra_message=shared_msg),
        assistant_record(cwd=proj, ts=ts, content=[
            {"type": "tool_use", "name": "Read", "input": {}},
        ], extra_message=shared_msg),
    ])
    entries = collect_debug_log(ctx())
    assert len(entries) == 1
    assert entries[0].tool_uses == ("Read",)

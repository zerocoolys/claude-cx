"""cx server：payload builder 与 HTTP 路由。"""

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cx.server import (  # noqa: E402
    config_payload,
    create_server,
    debug_log_payload_for,
    doctor_payload_for,
    model_payload,
)


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
def proj_with_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    p = tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    (p / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home, p


def _write_debug_session(home: Path, proj: Path, count: int) -> None:
    from cx.sessions import encode_project_dir

    d = home / ".claude" / "projects" / encode_project_dir(proj)
    d.mkdir(parents=True, exist_ok=True)
    records = []
    for i in range(1, count + 1):
        records.append({
            "sessionId": "s1",
            "type": "assistant",
            "timestamp": f"2026-08-16T00:00:{i:02d}.000Z",
            "cwd": str(proj),
            "requestId": f"req_{i}",
            "message": {
                "id": f"m{i}",
                "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                "content": [{"type": "text", "text": "hi"}],
            },
        })
    (d / "s1.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def test_api_debug_without_after_defaults_to_last_ten(proj_with_home):
    home, proj = proj_with_home
    _write_debug_session(home, proj, 15)
    httpd = create_server(proj, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        status, payload = _get_json(base + "/api/debug")
        assert status == 200
        assert len(payload["entries"]) == 10
        assert payload["entries"][0]["request_id"] == "req_15"  # 倒序，最新在前

        status, payload = _get_json(base + "/api/debug?after=2026-08-16T00:00:00.000Z")
        assert status == 200
        assert len(payload["entries"]) == 15  # 带 after 时默认放宽到 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.fixture
def running_server(proj):
    """在随机端口起一个真实服务器，跑在后台线程；测试结束自动关掉。"""
    httpd = create_server(proj, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.status, res.read()


def _get_json(url):
    status, body = _get(url)
    return status, json.loads(body)


# --- payload builders --------------------------------------------------------

def test_config_payload_matches_cli_json(proj, capsys):
    from cx.cli import main

    main(["--path", str(proj), "--json"])
    cli_payload = json.loads(capsys.readouterr().out)
    server_side = config_payload(proj, show_secrets=False)
    assert server_side == cli_payload


def test_doctor_payload_for_matches_cli_json(proj, capsys):
    from cx.cli import main

    main(["doctor", "--path", str(proj), "--json"])
    cli_payload = json.loads(capsys.readouterr().out)
    server_side = doctor_payload_for(proj, show_secrets=False)
    assert server_side == cli_payload


def test_model_payload_has_expected_shape(proj):
    payload = model_payload(proj, show_secrets=False, detail=False)
    assert "cx_version" in payload
    assert payload["models"] == []
    assert payload["total"]["model"] == "合计"


def test_debug_log_payload_for_matches_cli_json(proj, capsys):
    from cx.cli import main

    main(["debug", "--path", str(proj), "--json"])
    cli_payload = json.loads(capsys.readouterr().out)
    server_side = debug_log_payload_for(proj, show_secrets=False, limit=200)
    assert server_side == cli_payload


# --- HTTP routes --------------------------------------------------------------

def test_index_serves_dashboard_html(running_server):
    status, body = _get(running_server + "/")
    assert status == 200
    assert b"<title>cx dashboard</title>" in body


def test_static_js_is_served(running_server):
    status, body = _get(running_server + "/static/app.js")
    assert status == 200
    assert b"cx dashboard" in body


def test_static_path_traversal_is_blocked(running_server):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(running_server + "/static/..%2Fcli.py")
    assert e.value.code == 404


def test_api_config_returns_json(running_server):
    status, payload = _get_json(running_server + "/api/config")
    assert status == 200
    assert "sources" in payload


def test_api_doctor_returns_json(running_server):
    status, payload = _get_json(running_server + "/api/doctor")
    assert status == 200
    assert "summary" in payload


def test_api_model_returns_json(running_server):
    status, payload = _get_json(running_server + "/api/model")
    assert status == 200
    assert payload["models"] == []


def test_api_debug_returns_json(running_server):
    status, payload = _get_json(running_server + "/api/debug")
    assert status == 200
    assert payload["entries"] == []


def test_api_debug_limit_query_param(running_server):
    status, payload = _get_json(running_server + "/api/debug?limit=5")
    assert status == 200
    assert payload["entries"] == []


def test_api_debug_after_param_returns_json(running_server):
    status, payload = _get_json(running_server + "/api/debug?after=&limit=200")
    assert status == 200
    assert payload["entries"] == []


def test_debug_log_payload_for_after_ts_matches_sessions_call(proj):
    from cx.sessions import collect_debug_log
    from cx.discovery import find_repo_root
    from cx.model import Ctx as _Ctx

    ctx = _Ctx(cwd=proj, repo_root=find_repo_root(proj), home=Path.home())
    direct = [e.id for e in collect_debug_log(ctx, after_ts="")]
    via_server = [e["id"] for e in
                 debug_log_payload_for(proj, show_secrets=False, limit=200, after_ts="")["entries"]]
    assert direct == via_server


def test_unknown_route_is_404(running_server):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(running_server + "/nope")
    assert e.value.code == 404

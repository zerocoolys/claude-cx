"""cx server —— 本地只读 HTTP dashboard。

/api/* 每次请求都重新扫盘（跟 cx 本体一样只读），dashboard 反映的是当前磁盘
状态而不是启动时的快照。JSON 形状复用 cli.py 里的 build_report_payload /
build_doctor_payload，避免跟 `cx --json` / `cx doctor --json` 出现两套口径。
静态文件在 cx/static/ 下，随包分发，不写入任何文件。
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from cx.model import VERSION
from cx.sessions import (
    collect_debug_log,
    collect_session_summaries,
    collect_usage,
    debug_log_payload,
    session_timeline_payload,
    sessions_summary_payload,
    usage_payload,
)

STATIC_DIR = Path(__file__).parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def _report_context(path: Path, show_secrets: bool):
    """复用 cli.build_context：它只读 args.path / args.show_secrets。"""
    from cx.cli import build_context

    return build_context(SimpleNamespace(path=str(path), show_secrets=show_secrets))


def config_payload(path: Path, show_secrets: bool) -> dict:
    from cx.cli import build_report_payload

    ctx, merged, prov, assets = _report_context(path, show_secrets)
    return build_report_payload(ctx, merged, prov, assets)


def doctor_payload_for(path: Path, show_secrets: bool) -> dict:
    from cx.cli import build_doctor_payload

    ctx, merged, prov, assets = _report_context(path, show_secrets)
    return build_doctor_payload(ctx, merged, prov, assets, budget=20000,
                                ignore=None, fail_on="error")


def model_payload(path: Path, show_secrets: bool, detail: bool) -> dict:
    from cx.discovery import find_repo_root
    from cx.model import Ctx

    ctx = Ctx(cwd=path, repo_root=find_repo_root(path), home=Path.home(),
              show_secrets=show_secrets)
    usage = collect_usage(ctx)
    return {"cx_version": VERSION, **usage_payload(usage, detail)}


def debug_log_payload_for(path: Path, show_secrets: bool, limit: int,
                          after_ts: str | None = None) -> dict:
    from cx.discovery import find_repo_root
    from cx.model import Ctx

    ctx = Ctx(cwd=path, repo_root=find_repo_root(path), home=Path.home(),
              show_secrets=show_secrets)
    entries = collect_debug_log(ctx, limit=limit, after_ts=after_ts)
    return {"cx_version": VERSION, **debug_log_payload(entries)}


def sessions_payload(path: Path, show_secrets: bool) -> dict:
    from cx.discovery import find_repo_root
    from cx.model import Ctx

    ctx = Ctx(cwd=path, repo_root=find_repo_root(path), home=Path.home(),
              show_secrets=show_secrets)
    return {"cx_version": VERSION, **sessions_summary_payload(collect_session_summaries(ctx))}


def session_timeline_payload_for(path: Path, show_secrets: bool, session_id: str,
                                 limit: int) -> dict:
    from cx.discovery import find_repo_root
    from cx.model import Ctx

    ctx = Ctx(cwd=path, repo_root=find_repo_root(path), home=Path.home(),
              show_secrets=show_secrets)
    return {"cx_version": VERSION, **session_timeline_payload(ctx, session_id, limit)}


def _parse_limit(raw: str, default: int = 200) -> int:
    try:
        return int(raw)
    except ValueError:
        return default


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "cx-dashboard/1"

    def log_message(self, fmt, *args):  # noqa: A003 - BaseHTTPRequestHandler 接口
        pass  # 静默：dashboard 是交互工具，不需要访问日志刷屏

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        parsed = urlsplit(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        try:
            if route in ("/", "/index.html"):
                self._serve_static("index.html")
            elif route.startswith("/static/"):
                self._serve_static(route[len("/static/"):])
            elif route == "/api/config":
                self._serve_json(config_payload(self.server.cx_path,
                                                self.server.show_secrets))
            elif route == "/api/doctor":
                self._serve_json(doctor_payload_for(self.server.cx_path,
                                                    self.server.show_secrets))
            elif route == "/api/model":
                detail = query.get("detail", ["0"])[0].lower() in ("1", "true")
                self._serve_json(model_payload(self.server.cx_path,
                                               self.server.show_secrets, detail))
            elif route == "/api/debug":
                after_ts = query.get("after", [None])[0]
                # 没带 after：首次加载，只给最近 10 条；带了 after：增量轮询，
                # 默认放宽到 200 条，免得一次轮询周期内新增太多被截断漏掉。
                default_limit = 200 if after_ts is not None else 10
                limit = _parse_limit(query.get("limit", [str(default_limit)])[0],
                                     default_limit)
                self._serve_json(debug_log_payload_for(self.server.cx_path,
                                                       self.server.show_secrets,
                                                       limit, after_ts))
            elif route == "/api/sessions":
                self._serve_json(sessions_payload(self.server.cx_path,
                                                  self.server.show_secrets))
            elif route.startswith("/api/sessions/") and route.endswith("/timeline"):
                session_id = route[len("/api/sessions/"):-len("/timeline")].strip("/")
                limit = _parse_limit(query.get("limit", ["300"])[0], 300)
                self._serve_json(session_timeline_payload_for(self.server.cx_path,
                                                               self.server.show_secrets,
                                                               session_id, limit))
            else:
                self.send_error(404, "Not Found")
        except Exception as e:  # noqa: BLE001 - 转成 500，不让 handler 崩掉线程
            self.send_error(500, str(e))

    def _serve_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, name: str) -> None:
        # Path(...).name 只取文件名部分，天然挡掉 "../" 路径穿越
        safe_name = Path(name).name or "index.html"
        target = STATIC_DIR / safe_name
        if not target.is_file():
            self.send_error(404, "Not Found")
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(path: Path, host: str = "127.0.0.1", port: int = 8765,
                  show_secrets: bool = False) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    httpd.cx_path = path
    httpd.show_secrets = show_secrets
    return httpd


def serve(path: Path, *, host: str = "127.0.0.1", port: int = 8765,
          show_secrets: bool = False, open_browser: bool = True) -> int:
    httpd = create_server(path, host=host, port=port, show_secrets=show_secrets)
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"cx: dashboard 已启动 {url} (Ctrl+C 退出)")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0

"""从 ~/.claude/projects/ 的会话记录里统计每个模型的用量。

Claude Code 把每个工作目录的会话写进 `~/.claude/projects/<编码后的路径>/<sessionId>.jsonl`，
路径编码规则是把所有非字母数字字符换成 `-`（`/a/b_c/.claude` → `-a-b-c--claude`）。
编码不可逆，所以这里只用它筛出候选目录，再用记录里的 `cwd` 字段做二次确认。

jsonl 里一次 API 响应会被拆成多行写入（内容分块），每行都带同一个 `message.id`
和同一份**累计** usage——按行累加会翻倍，所以按 message.id 去重，保留最后一次出现的值。

子 agent 的记录不在主线文件里，而在 `<sessionId>/subagents/agent-<agentId>.jsonl`，
带 `isSidechain: true` 和 `attributionAgent`（agent 名）。这些文件的 `sessionId`
指回主线会话，所以按 sessionId 合并；漏读它们会少算一大截用量。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from cx.model import Ctx
from cx.pricing import estimate_stat_cost_usd

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

# dashboard Sessions tab 的"活跃"判定窗口：最后一条消息在这么多秒内算 active。
# 这是纯粹基于时间戳的启发式——cx 只读 jsonl，没有进程存活信号，超过窗口
# 不代表 Claude Code 真的退出了，只代表"最近没有新消息"。
ACTIVE_WINDOW_SECONDS = 300

# 每个 session 的 token 用量曲线最多采样这么多个点，避免会话很长时 payload 太大。
MAX_SERIES_POINTS = 40

# model 为 <synthetic> 的记录是本地合成的错误提示（如认证失败），没有真实用量
SYNTHETIC_MODEL = "<synthetic>"
# 极少数子 agent 记录没写 attributionAgent
UNNAMED_AGENT = "(未命名 agent)"


def encode_project_dir(path: Path | str) -> str:
    """把工作目录路径编码成 ~/.claude/projects 下的目录名。"""
    return _NON_ALNUM.sub("-", str(path))


def projects_root(ctx: Ctx) -> Path:
    return ctx.home / ".claude" / "projects"


@dataclass(frozen=True)
class Message:
    """一次 API 响应的用量。agent 为空表示主线，否则是子 agent 的名字。"""

    id: str
    model: str
    timestamp: str
    input: int
    output: int
    cache_creation: int
    cache_read: int
    agent: str = ""

    @property
    def sidechain(self) -> bool:
        return bool(self.agent)

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_creation + self.cache_read


@dataclass(frozen=True)
class Session:
    """一次会话：主线 jsonl 加上它 subagents/ 目录下的子 agent 记录。"""

    path: Path
    session_id: str
    cwd: str | None
    messages: tuple[Message, ...]
    branch: str = ""
    cli_version: str = ""
    custom_title: str = ""
    files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ModelStat:
    model: str
    sessions: int
    messages: int
    input: int
    output: int
    cache_creation: int
    cache_read: int
    first: str | None
    last: str | None

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_creation + self.cache_read


@dataclass(frozen=True)
class Usage:
    base: Path
    dirs: tuple[Path, ...]
    sessions: tuple[Session, ...]
    models: tuple[ModelStat, ...]
    total: ModelStat
    main: ModelStat                  # 主线合计
    sub: ModelStat                   # 子 agent 合计
    agents: tuple[ModelStat, ...]    # 按 agent 名聚合，model 字段放 agent 名


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _iter_records(path: Path) -> Iterator[dict]:
    """逐行读。坏行跳过——会话文件可能正在被写入，最后一行常是半截 JSON。"""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def _agent_name(rec: dict) -> str:
    """子 agent 记录带 attributionAgent（agent 名）；主线记录没有这个字段。"""
    if not rec.get("isSidechain"):
        return ""
    name = rec.get("attributionAgent")
    return str(name) if isinstance(name, str) and name else UNNAMED_AGENT


def parse_file(path: Path) -> Session:
    """解析单个 jsonl。按 message.id 去重，保留最后一次出现的 usage。"""
    cwd: str | None = None
    session_id = ""
    branch = ""
    cli_version = ""
    custom_title = ""
    by_id: dict[str, Message] = {}

    for rec in _iter_records(path):
        if cwd is None and isinstance(rec.get("cwd"), str):
            cwd = rec["cwd"]
        if not session_id and isinstance(rec.get("sessionId"), str):
            session_id = rec["sessionId"]
        if isinstance(rec.get("gitBranch"), str) and rec["gitBranch"]:
            # 取最后一次出现的分支名，而非第一次：同一个 worktree 目录可能
            # 在会话过程中被重命名或切换到别的分支，first-seen 会显示过期的名字
            # （dashboard Sessions tab 的卡片标题就靠这个字段，见 static/app.js）。
            branch = rec["gitBranch"]
        if not cli_version and isinstance(rec.get("version"), str):
            cli_version = rec["version"]
        if rec.get("type") == "custom-title" and isinstance(rec.get("customTitle"), str):
            # 人写的会话标题（sidebar 任务列表里那个），跟 gitBranch 同理取最后一次。
            custom_title = rec["customTitle"]
        if rec.get("type") != "assistant" or rec.get("isApiErrorMessage"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        model = msg.get("model")
        if not isinstance(model, str) or not model or model == SYNTHETIC_MODEL:
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        key = str(msg.get("id") or rec.get("uuid") or f"{path.name}-{len(by_id)}")
        by_id[key] = Message(
            id=key,
            model=model,
            timestamp=str(rec.get("timestamp") or ""),
            input=_int(usage.get("input_tokens")),
            output=_int(usage.get("output_tokens")),
            cache_creation=_int(usage.get("cache_creation_input_tokens")),
            cache_read=_int(usage.get("cache_read_input_tokens")),
            agent=_agent_name(rec),
        )

    return Session(path=path, session_id=session_id or path.stem, cwd=cwd,
                   messages=tuple(by_id.values()), branch=branch,
                   cli_version=cli_version, custom_title=custom_title, files=(path,))


# 保留旧名字：单文件解析
parse_session = parse_file


def merge_files(parsed: list[Session]) -> list[Session]:
    """把同一 sessionId 的主线文件和 subagents/*.jsonl 合成一个 Session。

    子 agent 的记录不出现在主线文件里，message.id 也不重叠，所以合并只需
    按 id 去重兜底即可。主线文件（文件名 = sessionId）当作 Session 的代表路径。
    """
    order: list[str] = []
    groups: dict[str, list[Session]] = {}
    for s in parsed:
        if s.session_id not in groups:
            order.append(s.session_id)
            groups[s.session_id] = []
        groups[s.session_id].append(s)

    out = []
    for sid in order:
        parts = groups[sid]
        main = next((p for p in parts if p.path.stem == sid), parts[0])
        by_id: dict[str, Message] = {}
        for p in parts:
            for m in p.messages:
                by_id[m.id] = m
        out.append(Session(
            path=main.path,
            session_id=sid,
            cwd=next((p.cwd for p in parts if p.cwd), None),
            messages=tuple(by_id.values()),
            branch=next((p.branch for p in parts if p.branch), ""),
            cli_version=next((p.cli_version for p in parts if p.cli_version), ""),
            custom_title=next((p.custom_title for p in parts if p.custom_title), ""),
            files=tuple(p.path for p in parts),
        ))
    return out


def session_dirs(ctx: Ctx) -> list[Path]:
    """当前项目对应的会话目录：自身，外加它下面的 worktree / 子目录。"""
    root = projects_root(ctx)
    if not root.is_dir():
        return []
    prefix = encode_project_dir(ctx.repo_root or ctx.cwd)
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    return [d for d in entries
            if d.name == prefix or d.name.startswith(prefix + "-")]


def _under(child: str, base: Path) -> bool:
    try:
        return Path(child) == base or Path(child).is_relative_to(base)
    except (ValueError, OSError):
        return False


@dataclass(frozen=True)
class DebugEntry:
    """一次 assistant 响应里，token 用量之外的调试字段。

    isApiErrorMessage 或 model 为 <synthetic> 都算一次出错请求——前者是真实
    API 报错，后者是本地合成的提示（认证失败等），对排障来说都值得看见，
    跟 sessions.py 其余统计口径里把它们排除在外的处理刻意不同。
    """

    id: str             # 记录的 uuid；--follow / dashboard 轮询用它做去重
    timestamp: str
    session_id: str
    agent: str          # 空串表示主线
    model: str
    request_id: str
    effort: str
    stop_reason: str
    is_error: bool
    error_text: str
    tool_uses: tuple[str, ...]
    cache_miss_reason: str


def _content_tool_uses(content) -> tuple[str, ...]:
    if not isinstance(content, list):
        return ()
    names = [item.get("name") for item in content
             if isinstance(item, dict) and item.get("type") == "tool_use"]
    return tuple(n for n in names if isinstance(n, str))


def _content_text(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = [item.get("text", "") for item in content
             if isinstance(item, dict) and item.get("type") == "text"]
    return "".join(str(p) for p in parts).strip()


def _parse_debug_entries(path: Path) -> tuple[str | None, list[DebugEntry]]:
    """跟 parse_file 一样单遍扫描；返回 (cwd, entries)，cwd 供事后按项目过滤。"""
    cwd: str | None = None
    session_id = ""
    entries: list[DebugEntry] = []

    for rec in _iter_records(path):
        if cwd is None and isinstance(rec.get("cwd"), str):
            cwd = rec["cwd"]
        if not session_id and isinstance(rec.get("sessionId"), str):
            session_id = rec["sessionId"]
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        model = msg.get("model")
        if not isinstance(model, str) or not model:
            continue
        content = msg.get("content")
        diagnostics = msg.get("diagnostics")
        cache_miss = (diagnostics.get("cache_miss_reason")
                      if isinstance(diagnostics, dict) else None)
        is_error = bool(rec.get("isApiErrorMessage")) or model == SYNTHETIC_MODEL
        entries.append(DebugEntry(
            id=str(rec.get("uuid") or f"{path.stem}-{len(entries)}"),
            timestamp=str(rec.get("timestamp") or ""),
            session_id=session_id or path.stem,
            agent=_agent_name(rec),
            model=model,
            request_id=str(rec.get("requestId") or ""),
            effort=str(rec.get("effort") or ""),
            stop_reason=str(msg.get("stop_reason") or ""),
            is_error=is_error,
            error_text=_content_text(content) if is_error else "",
            tool_uses=_content_tool_uses(content),
            cache_miss_reason=json.dumps(cache_miss, ensure_ascii=False) if cache_miss else "",
        ))
    return cwd, entries


def collect_debug_log(ctx: Ctx, limit: int | None = 200,
                      after_ts: str | None = None) -> list[DebugEntry]:
    """取调试记录，跨主线与子 agent；目录/cwd 过滤同 collect_sessions。

    默认（after_ts 为 None）按时间倒序取最近 limit 条，给 `cx debug` 一次性查看用。
    传 after_ts 时改成正序返回严格晚于它的新记录（最多 limit 条），
    给 `--follow` 和 dashboard 轮询增量拉取用——时间戳字符串是 ISO 8601，
    天然可比较排序，不需要额外的游标状态。

    limit=None 表示不截断，返回全部记录——按 session 分组统计（Sessions tab）
    需要完整历史，不能只看最近 limit 条。
    """
    base = ctx.repo_root or ctx.cwd
    out: list[DebugEntry] = []
    for d in session_dirs(ctx):
        try:
            files = sorted(d.rglob("*.jsonl"))
        except OSError:
            continue
        for f in files:
            cwd, entries = _parse_debug_entries(f)
            if cwd is not None and not _under(cwd, base):
                continue
            out.extend(entries)
    if after_ts is not None:
        fresh = sorted((e for e in out if e.timestamp > after_ts), key=lambda e: e.timestamp)
        return fresh if limit is None else fresh[:limit]
    out.sort(key=lambda e: e.timestamp, reverse=True)
    return out if limit is None else out[:limit]


def debug_entry_payload(e: DebugEntry) -> dict:
    return {
        "id": e.id,
        "timestamp": e.timestamp,
        "session_id": e.session_id,
        "agent": e.agent,
        "model": e.model,
        "request_id": e.request_id,
        "effort": e.effort,
        "stop_reason": e.stop_reason,
        "is_error": e.is_error,
        "error_text": e.error_text,
        "tool_uses": list(e.tool_uses),
        "cache_miss_reason": e.cache_miss_reason,
    }


def debug_log_payload(entries: list[DebugEntry]) -> dict:
    return {"entries": [debug_entry_payload(e) for e in entries]}


def collect_sessions(ctx: Ctx) -> tuple[list[Path], list[Session]]:
    base = ctx.repo_root or ctx.cwd
    dirs = session_dirs(ctx)
    sessions = []
    for d in dirs:
        try:
            # rglob 而非 glob：子 agent 的记录在 <sessionId>/subagents/ 下
            files = sorted(d.rglob("*.jsonl"))
        except OSError:
            continue
        for s in merge_files([parse_file(f) for f in files]):
            # 目录名编码不可逆，可能误配（/a/b 与 /a/b-c 编码前缀相同），
            # 用记录里的真实 cwd 复核；文件里没有 cwd 才退回目录匹配。
            if s.cwd is not None and not _under(s.cwd, base):
                continue
            if s.messages:
                sessions.append(s)
    return dirs, sessions


def _fold(model: str, rows: list[tuple[str, Message]]) -> ModelStat:
    stamps = sorted(m.timestamp for _, m in rows if m.timestamp)
    return ModelStat(
        model=model,
        sessions=len({sid for sid, _ in rows}),
        messages=len(rows),
        input=sum(m.input for _, m in rows),
        output=sum(m.output for _, m in rows),
        cache_creation=sum(m.cache_creation for _, m in rows),
        cache_read=sum(m.cache_read for _, m in rows),
        first=stamps[0] if stamps else None,
        last=stamps[-1] if stamps else None,
    )


def aggregate(sessions: list[Session], keep=None) -> tuple[list[ModelStat], ModelStat]:
    """按模型聚合，返回 (各模型统计, 合计)。

    keep 是可选的 Message 谓词，用来只统计主线或只统计子 agent。
    各模型按总 token 降序。
    """
    rows: dict[str, list[tuple[str, Message]]] = {}
    everything: list[tuple[str, Message]] = []
    for s in sessions:
        for m in s.messages:
            if keep is not None and not keep(m):
                continue
            rows.setdefault(m.model, []).append((s.session_id, m))
            everything.append((s.session_id, m))

    stats = [_fold(model, items) for model, items in rows.items()]
    stats.sort(key=lambda s: (-s.total, s.model))
    return stats, _fold("合计", everything)


def aggregate_agents(sessions: list[Session]) -> list[ModelStat]:
    """按子 agent 名聚合用量（ModelStat.model 字段放 agent 名），总量降序。"""
    rows: dict[str, list[tuple[str, Message]]] = {}
    for s in sessions:
        for m in s.messages:
            if m.agent:
                rows.setdefault(m.agent, []).append((s.session_id, m))
    stats = [_fold(name, items) for name, items in rows.items()]
    stats.sort(key=lambda a: (-a.total, a.model))
    return stats


def collect_usage(ctx: Ctx) -> Usage:
    dirs, sessions = collect_sessions(ctx)
    models, total = aggregate(sessions)
    _, main = aggregate(sessions, keep=lambda m: not m.sidechain)
    _, sub = aggregate(sessions, keep=lambda m: m.sidechain)
    return Usage(
        base=ctx.repo_root or ctx.cwd,
        dirs=tuple(dirs),
        sessions=tuple(sessions),
        models=tuple(models),
        total=total,
        main=main,
        sub=sub,
        agents=tuple(aggregate_agents(sessions)),
    )


def stat_payload(s: ModelStat, label: str = "model") -> dict:
    """label 决定名字放在哪个键下：模型统计用 model，agent 统计用 agent。"""
    return {
        label: s.model, "sessions": s.sessions, "messages": s.messages,
        "input_tokens": s.input, "output_tokens": s.output,
        "cache_creation_tokens": s.cache_creation,
        "cache_read_tokens": s.cache_read, "total_tokens": s.total,
        "first": s.first, "last": s.last,
    }


def session_payload(s: Session) -> dict:
    models, total = aggregate([s])
    return {
        "session_id": s.session_id,
        "path": str(s.path),
        "files": [str(f) for f in s.files],
        "cwd": s.cwd,
        "branch": s.branch,
        "custom_title": s.custom_title,
        "cli_version": s.cli_version,
        "messages": total.messages,
        "total_tokens": total.total,
        "first": total.first,
        "last": total.last,
        "models": [stat_payload(m) for m in models],
        "agents": [stat_payload(a, "agent") for a in aggregate_agents([s])],
    }


def usage_payload(usage: Usage, detail: bool = False) -> dict:
    payload = {
        "base": str(usage.base),
        "session_dirs": [str(d) for d in usage.dirs],
        "sessions": len(usage.sessions),
        "models": [stat_payload(s) for s in usage.models],
        "total": stat_payload(usage.total),
    }
    if detail:
        payload["main"] = stat_payload(usage.main)
        payload["sub_agent"] = stat_payload(usage.sub)
        payload["agents"] = [stat_payload(a, "agent") for a in usage.agents]
        payload["session_detail"] = [session_payload(s) for s in usage.sessions]
    return payload


# --- dashboard Sessions tab：实时会话摘要 -----------------------------------------
#
# 跟上面的 Usage/ModelStat 口径不同，这组函数关心的是"现在每个会话在做什么"，
# 而不是历史用量汇总：谁活跃、最近调用了什么工具、子 agent 分布、用量曲线。
# 全部基于 collect_sessions() / collect_debug_log() 的既有结果二次聚合，不
# 重新解析文件。


def session_active(last_ts: str | None, now: datetime | None = None,
                   window: int = ACTIVE_WINDOW_SECONDS) -> bool:
    """最后一条消息在 window 秒内算 active。空/非法时间戳一律 False。"""
    if not last_ts:
        return False
    try:
        ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - ts).total_seconds() <= window


@dataclass(frozen=True)
class AgentSummary:
    name: str
    messages: int
    tokens: int
    last: str | None


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    cwd: str | None
    branch: str
    custom_title: str
    cli_version: str
    active: bool
    messages: int
    total_tokens: int
    first: str | None
    last: str | None
    last_tool: str
    last_stop_reason: str
    last_agent: str
    last_effort: str
    error_count: int
    agents: tuple[AgentSummary, ...]
    models: tuple[ModelStat, ...]
    estimated_cost_usd: float
    cost_has_unpriced_model: bool
    token_series: tuple[tuple[str, int], ...]


def _token_series(s: Session) -> tuple[tuple[str, int], ...]:
    """会话内按时间正序的累计 token 曲线，降采样到最多 MAX_SERIES_POINTS 个点。"""
    msgs = sorted(s.messages, key=lambda m: m.timestamp)
    points: list[tuple[str, int]] = []
    cumulative = 0
    for m in msgs:
        cumulative += m.total
        points.append((m.timestamp, cumulative))
    if len(points) <= MAX_SERIES_POINTS:
        return tuple(points)
    step = len(points) / MAX_SERIES_POINTS
    sampled = [points[int(i * step)] for i in range(MAX_SERIES_POINTS)]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return tuple(sampled)


def collect_session_summaries(ctx: Ctx, now: datetime | None = None) -> list[SessionSummary]:
    """按会话聚合出 Sessions tab 要展示的摘要，按最后活动时间降序排列。"""
    _, sessions = collect_sessions(ctx)
    by_session: dict[str, list[DebugEntry]] = {}
    for e in collect_debug_log(ctx, limit=None):
        by_session.setdefault(e.session_id, []).append(e)

    out = []
    for s in sessions:
        entries = sorted(by_session.get(s.session_id, ()), key=lambda e: e.timestamp)
        last_entry = entries[-1] if entries else None
        models, total = aggregate([s])
        agents = [AgentSummary(a.model, a.messages, a.total, a.last)
                  for a in aggregate_agents([s])]
        # 未知档位（如 fable）的模型没有公开定价，estimate_stat_cost_usd 返回 None——
        # 花费按已知档位求和，cost_has_unpriced_model 提示前端这不是全量估算。
        model_costs = [estimate_stat_cost_usd(m) for m in models]
        out.append(SessionSummary(
            session_id=s.session_id,
            cwd=s.cwd,
            branch=s.branch,
            custom_title=s.custom_title,
            cli_version=s.cli_version,
            active=session_active(total.last, now),
            messages=total.messages,
            total_tokens=total.total,
            first=total.first,
            last=total.last,
            last_tool=(last_entry.tool_uses[-1] if last_entry and last_entry.tool_uses else ""),
            last_stop_reason=last_entry.stop_reason if last_entry else "",
            last_agent=last_entry.agent if last_entry else "",
            last_effort=last_entry.effort if last_entry else "",
            error_count=sum(1 for e in entries if e.is_error),
            agents=tuple(agents),
            models=tuple(models),
            estimated_cost_usd=sum(c for c in model_costs if c is not None),
            cost_has_unpriced_model=any(c is None for c in model_costs),
            token_series=_token_series(s),
        ))
    out.sort(key=lambda s: s.last or "", reverse=True)
    return out


def session_timeline_payload(ctx: Ctx, session_id: str, limit: int = 300) -> dict:
    """单个 session 的完整调用时间线（正序），跟 Debug tab 同一种 entry 形状。"""
    entries = sorted(
        (e for e in collect_debug_log(ctx, limit=None) if e.session_id == session_id),
        key=lambda e: e.timestamp,
    )
    return {"entries": [debug_entry_payload(e) for e in entries[-limit:]]}


def agent_summary_payload(a: AgentSummary) -> dict:
    return {"agent": a.name, "messages": a.messages, "total_tokens": a.tokens, "last": a.last}


def session_summary_payload(s: SessionSummary) -> dict:
    return {
        "session_id": s.session_id,
        "cwd": s.cwd,
        "branch": s.branch,
        "custom_title": s.custom_title,
        "cli_version": s.cli_version,
        "active": s.active,
        "messages": s.messages,
        "total_tokens": s.total_tokens,
        "first": s.first,
        "last": s.last,
        "last_tool": s.last_tool,
        "last_stop_reason": s.last_stop_reason,
        "last_agent": s.last_agent,
        "last_effort": s.last_effort,
        "error_count": s.error_count,
        "agents": [agent_summary_payload(a) for a in s.agents],
        "models": [stat_payload(m) for m in s.models],
        "estimated_cost_usd": round(s.estimated_cost_usd, 4),
        "cost_has_unpriced_model": s.cost_has_unpriced_model,
        "token_series": [{"ts": ts, "total": total} for ts, total in s.token_series],
    }


def sessions_summary_payload(summaries: list[SessionSummary]) -> dict:
    return {"sessions": [session_summary_payload(s) for s in summaries]}

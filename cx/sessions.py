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
from pathlib import Path
from typing import Iterator

from cx.model import Ctx

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

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
    by_id: dict[str, Message] = {}

    for rec in _iter_records(path):
        if cwd is None and isinstance(rec.get("cwd"), str):
            cwd = rec["cwd"]
        if not session_id and isinstance(rec.get("sessionId"), str):
            session_id = rec["sessionId"]
        if not branch and isinstance(rec.get("gitBranch"), str):
            branch = rec["gitBranch"]
        if not cli_version and isinstance(rec.get("version"), str):
            cli_version = rec["version"]
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
                   cli_version=cli_version, files=(path,))


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

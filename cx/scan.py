from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from cx.model import Ctx, SourceFile
from cx.util import count_tokens_rough, load_json


def parse_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta = {}
    for line in text[3:end].splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("\"'")
    return meta


def scan_md_assets(ctx: Ctx, subdir: str) -> list[dict]:
    """扫描 agents / commands / skills 这类 markdown 资产目录。"""
    out = []
    locations = [
        ("user", ctx.home / ".claude" / subdir),
        ("project", ctx.cwd / ".claude" / subdir),
    ]
    for scope, base in locations:
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.md")):
            meta = parse_frontmatter(f)
            try:
                size = f.stat().st_size
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                size, text = 0, ""
            out.append(
                {
                    "name": meta.get("name") or f.stem,
                    "scope": scope,
                    "path": str(f),
                    "description": meta.get("description", ""),
                    "model": meta.get("model", ""),
                    "tools": meta.get("tools", ""),
                    "bytes": size,
                    "tokens": count_tokens_rough(text),
                }
            )
    return out


def scan_memory(ctx: Ctx) -> list[dict]:
    """CLAUDE.md 各层。user / project / local，外加向上逐级目录。"""
    out = []
    seen: set[Path] = set()

    def add(scope: str, p: Path):
        if p in seen or not p.is_file():
            return
        seen.add(p)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        imports = re.findall(r"^@([^\s]+)", text, re.M)
        out.append(
            {
                "scope": scope,
                "path": str(p),
                "lines": text.count("\n") + 1,
                "bytes": len(text.encode()),
                "tokens": count_tokens_rough(text),
                "imports": imports,
            }
        )

    add("user", ctx.home / ".claude" / "CLAUDE.md")
    add("project", ctx.cwd / "CLAUDE.md")
    add("project", ctx.cwd / ".claude" / "CLAUDE.md")
    add("local", ctx.cwd / "CLAUDE.local.md")

    # 向上逐级（不含 home 本身）
    stop = ctx.repo_root or ctx.cwd
    cur = ctx.cwd.parent
    while True:
        if cur == ctx.home or cur == cur.parent:
            break
        add("project", cur / "CLAUDE.md")
        if cur == stop:
            break
        cur = cur.parent
    return out


def scan_mcp(ctx: Ctx, merged: dict) -> list[dict]:
    """MCP server 分布在三处：~/.claude.json (user)、~/.claude.json projects[cwd] (local)、.mcp.json (project)。"""
    out = []

    user_cfg_path = ctx.home / ".claude.json"
    user_cfg, err = load_json(user_cfg_path)
    if err:
        ctx.problems.append(f"~/.claude.json: {err}")
        ctx.sources.append(SourceFile(
            scope="user", path=user_cfg_path, exists=user_cfg_path.exists(), error=err
        ))
    user_cfg = user_cfg or {}

    for name, spec in (user_cfg.get("mcpServers") or {}).items():
        out.append({"name": name, "scope": "user", "source": "~/.claude.json", "spec": spec})

    proj_entry = (user_cfg.get("projects") or {}).get(str(ctx.cwd)) or {}
    for name, spec in (proj_entry.get("mcpServers") or {}).items():
        out.append(
            {"name": name, "scope": "local", "source": "~/.claude.json → projects[cwd]", "spec": spec}
        )

    mcp_json_path = ctx.cwd / ".mcp.json"
    mcp_json, err = load_json(mcp_json_path)
    if err:
        ctx.problems.append(f"./.mcp.json: {err} → 该文件的 MCP server 不会加载")
        ctx.sources.append(SourceFile(
            scope="project", path=mcp_json_path, exists=mcp_json_path.exists(), error=err
        ))
    for name, spec in ((mcp_json or {}).get("mcpServers") or {}).items():
        out.append({"name": name, "scope": "project", "source": "./.mcp.json", "spec": spec})

    # 审批状态：.mcp.json 的 server 需要批准，除非 enableAllProjectMcpServers
    enabled = set(merged.get("enabledMcpjsonServers") or [])
    disabled = set(merged.get("disabledMcpjsonServers") or [])
    allow_all = bool(merged.get("enableAllProjectMcpServers"))

    for s in out:
        spec = s["spec"] if isinstance(s["spec"], dict) else {}
        s["transport"] = spec.get("type") or ("http" if spec.get("url") else "stdio")
        s["target"] = spec.get("url") or spec.get("command") or ""
        if spec.get("args"):
            s["target"] = f"{s['target']} {' '.join(map(str, spec['args']))}"
        if s["scope"] == "project":
            if s["name"] in disabled:
                s["status"] = "被 disabledMcpjsonServers 拒绝"
            elif s["name"] in enabled or allow_all:
                s["status"] = "已批准"
            else:
                s["status"] = "待批准"
        else:
            s["status"] = "已批准"
    return out


def scan_plugins(ctx: Ctx, merged: dict) -> list[dict]:
    out = []
    cfg = merged.get("enabledPlugins") or merged.get("plugins") or {}
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            out.append({"name": k, "enabled": bool(v), "source": "settings"})
    elif isinstance(cfg, list):
        for k in cfg:
            out.append({"name": str(k), "enabled": True, "source": "settings"})

    pdir = ctx.home / ".claude" / "plugins"
    if pdir.is_dir():
        for repo in sorted(p for p in pdir.iterdir() if p.is_dir()):
            if repo.name in {"cache", "repos"}:
                for sub in sorted(p for p in repo.iterdir() if p.is_dir()):
                    out.append({"name": sub.name, "enabled": None, "source": f"~/.claude/plugins/{repo.name}"})
            else:
                out.append({"name": repo.name, "enabled": None, "source": "~/.claude/plugins"})
    return out


def gitignore_status(ctx: Ctx) -> str | None:
    """settings.local.json 含个人权限批准，不该进版本库。"""
    root = ctx.repo_root
    if not root:
        return None
    rel = ".claude/settings.local.json"
    target = root / rel
    if not target.exists():
        return None
    try:
        r = subprocess.run(
            ["git", "check-ignore", "-q", str(target)],
            cwd=root,
            capture_output=True,
            timeout=5,
        )
        if r.returncode == 0:
            return "ignored"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    try:
        r = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            cwd=root,
            capture_output=True,
            timeout=5,
        )
        if r.returncode == 0:
            return "tracked"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return "untracked"


def claude_version() -> str:
    exe = shutil.which("claude")
    if not exe:
        return "未安装 / 不在 PATH"
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or r.stderr.strip() or "未知"
    except (OSError, subprocess.SubprocessError):
        return "未知"

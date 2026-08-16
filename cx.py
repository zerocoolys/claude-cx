#!/usr/bin/env python3
"""
cx - Claude Code 配置状态检视器

一屏展示当前目录下 Claude Code 的完整生效配置，每一项都标注来源 scope。
零依赖，只读，不修改任何文件。

用法:
    cx                  # 完整报告
    cx --json           # 机器可读输出
    cx --section perms  # 只看某一节
    cx --show-secrets   # 不脱敏（默认脱敏）
    cx --path DIR       # 检视指定目录而非 cwd
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import unicodedata
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# scope 定义：数字越大优先级越高
# 参考 https://code.claude.com/docs/en/settings
# ---------------------------------------------------------------------------
SCOPES = ["user", "project", "local", "managed"]
SCOPE_RANK = {name: i for i, name in enumerate(SCOPES)}
SCOPE_LABEL = {
    "user": "user ",
    "project": "proj ",
    "local": "local",
    "managed": "MGMT ",
}

# permission 规则跨 scope 合并而非覆盖
MERGE_ONLY_KEYS = {"permissions.allow", "permissions.deny", "permissions.ask"}
# fallbackModel 是整链替换，不拼接
REPLACE_WHOLE_KEYS = {"fallbackModel"}

SECRET_PAT = re.compile(
    r"(key|token|secret|password|passwd|credential|auth|bearer|session)", re.I
)


# ---------------------------------------------------------------------------
# 终端上色
# ---------------------------------------------------------------------------
class C:
    on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls.on else s

    @classmethod
    def dim(cls, s):
        return cls._w("2", s)

    @classmethod
    def bold(cls, s):
        return cls._w("1", s)

    @classmethod
    def red(cls, s):
        return cls._w("31", s)

    @classmethod
    def green(cls, s):
        return cls._w("32", s)

    @classmethod
    def yellow(cls, s):
        return cls._w("33", s)

    @classmethod
    def blue(cls, s):
        return cls._w("34", s)

    @classmethod
    def magenta(cls, s):
        return cls._w("35", s)

    @classmethod
    def cyan(cls, s):
        return cls._w("36", s)


SCOPE_COLOR = {
    "user": C.blue,
    "project": C.green,
    "local": C.yellow,
    "managed": C.magenta,
}


def tag(scope: str) -> str:
    return SCOPE_COLOR.get(scope, C.dim)(f"[{SCOPE_LABEL.get(scope, scope)}]")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class SourceFile:
    scope: str
    path: Path
    exists: bool = False
    data: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class Ctx:
    cwd: Path
    repo_root: Path | None
    home: Path
    sources: list[SourceFile] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    show_secrets: bool = False


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def load_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"读取失败: {e}"
    if not raw.strip():
        return {}, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        # JSON 语法错误会导致整个文件被静默忽略，是 hook/权限不生效最常见的原因
        return None, f"JSON 语法错误 (行 {e.lineno} 列 {e.colno}): {e.msg}"


def find_repo_root(start: Path) -> Path | None:
    p = start.resolve()
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def redact(value: Any, key: str = "", show: bool = False) -> Any:
    if show:
        return value
    if isinstance(value, dict):
        return {k: redact(v, k, show) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key, show) for v in value]
    if isinstance(value, str) and SECRET_PAT.search(key) and value:
        if len(value) <= 8:
            return "••••"
        return f"{value[:4]}••••{value[-2:]} ({len(value)} chars)"
    return value


def short(path: Path, ctx: Ctx) -> str:
    s = str(path)
    try:
        if path.is_relative_to(ctx.cwd):
            return "./" + str(path.relative_to(ctx.cwd))
    except (ValueError, AttributeError):
        pass
    try:
        if path.is_relative_to(ctx.home):
            return "~/" + str(path.relative_to(ctx.home))
    except (ValueError, AttributeError):
        pass
    return s


def fmt_value(v: Any, width: int = 60) -> str:
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(v, bool):
        s = "true" if v else "false"
    elif v is None:
        s = "null"
    else:
        s = str(v)
    if len(s) > width:
        s = s[: width - 1] + "…"
    return s


def count_tokens_rough(text: str) -> int:
    """粗略估算 token：中文按字符计，其余按 ~4 字符/token。够用于排序。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", text))
    rest = len(text) - cjk
    return cjk + rest // 4


# ---------------------------------------------------------------------------
# 发现所有 settings 来源
# ---------------------------------------------------------------------------
def managed_dirs() -> list[Path]:
    sysname = platform.system()
    if sysname == "Darwin":
        return [Path("/Library/Application Support/ClaudeCode")]
    if sysname == "Windows":
        return [Path(r"C:\Program Files\ClaudeCode")]
    return [Path("/etc/claude-code")]


def discover_sources(ctx: Ctx) -> None:
    settings_root = ctx.repo_root or ctx.cwd

    candidates: list[tuple[str, Path]] = [
        ("user", ctx.home / ".claude" / "settings.json"),
        ("project", ctx.cwd / ".claude" / "settings.json"),
        ("local", settings_root / ".claude" / "settings.local.json"),
    ]
    # 2.1.211 之前 settings.local.json 总是落在启动目录，旧文件仍会被读取
    legacy_local = ctx.cwd / ".claude" / "settings.local.json"
    if legacy_local != settings_root / ".claude" / "settings.local.json":
        candidates.insert(2, ("local", legacy_local))

    # managed: managed-settings.json 作为基底，随后 managed-settings.d/*.json 按字母序叠加
    for d in managed_dirs():
        candidates.append(("managed", d / "managed-settings.json"))
        dropin = d / "managed-settings.d"
        if dropin.is_dir():
            for f in sorted(dropin.glob("*.json")):
                if not f.name.startswith("."):
                    candidates.append(("managed", f))

    for scope, path in candidates:
        data, err = load_json(path)
        sf = SourceFile(scope=scope, path=path, exists=path.exists())
        if err:
            sf.error = err
            lvl = "managed 配置" if scope == "managed" else "配置"
            ctx.problems.append(f"{short(path, ctx)}: {err} → 该{lvl}文件会被整体忽略")
        elif data is not None:
            sf.data = data
        ctx.sources.append(sf)


# ---------------------------------------------------------------------------
# 带 provenance 的深度合并
# ---------------------------------------------------------------------------
def merge_with_provenance(sources: list[SourceFile]) -> tuple[dict, dict]:
    """返回 (合并后配置, {点分路径: [(scope, path, value), ...]})。

    provenance 里保留全部贡献者，末位即为生效值。
    """
    merged: dict = {}
    prov: dict[str, list] = {}

    ordered = sorted(
        [s for s in sources if s.data],
        key=lambda s: (SCOPE_RANK[s.scope], sources.index(s)),
    )

    def walk(node: dict, into: dict, prefix: str, sf: SourceFile) -> None:
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and isinstance(into.get(k), dict):
                walk(v, into[k], path, sf)
                continue
            if isinstance(v, dict):
                into[k] = {}
                walk(v, into[k], path, sf)
                continue

            if isinstance(v, list) and path not in REPLACE_WHOLE_KEYS:
                # 数组拼接去重（permission 规则也走这条路，正是它们跨 scope 合并的原因）
                base = into.get(k, [])
                base = base if isinstance(base, list) else []
                seen = {json.dumps(x, sort_keys=True) for x in base}
                for item in v:
                    key = json.dumps(item, sort_keys=True)
                    if key not in seen:
                        base.append(item)
                        seen.add(key)
                into[k] = base
            else:
                into[k] = v

            prov.setdefault(path, []).append((sf.scope, sf.path, v))

    for sf in ordered:
        walk(sf.data, merged, "", sf)

    return merged, prov


def effective_scope(prov_entries: list) -> str:
    return prov_entries[-1][0] if prov_entries else "?"


# ---------------------------------------------------------------------------
# 各类资产扫描
# ---------------------------------------------------------------------------
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

    user_cfg, err = load_json(ctx.home / ".claude.json")
    if err:
        ctx.problems.append(f"~/.claude.json: {err}")
    user_cfg = user_cfg or {}

    for name, spec in (user_cfg.get("mcpServers") or {}).items():
        out.append({"name": name, "scope": "user", "source": "~/.claude.json", "spec": spec})

    proj_entry = (user_cfg.get("projects") or {}).get(str(ctx.cwd)) or {}
    for name, spec in (proj_entry.get("mcpServers") or {}).items():
        out.append(
            {"name": name, "scope": "local", "source": "~/.claude.json → projects[cwd]", "spec": spec}
        )

    mcp_json, err = load_json(ctx.cwd / ".mcp.json")
    if err:
        ctx.problems.append(f"./.mcp.json: {err} → 该文件的 MCP server 不会加载")
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


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------
def disp_width(s: str) -> int:
    """CJK 字符占两个终端列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - disp_width(s))


def hr(title: str) -> str:
    line = "─" * max(4, 62 - disp_width(title))
    return C.bold(f"\n── {title} ") + C.dim(line)


def render(ctx: Ctx, merged: dict, prov: dict, assets: dict, sections: set[str]) -> None:
    p = print

    if "env" in sections:
        p(C.bold(f"cx {VERSION}") + C.dim("  ·  Claude Code 配置状态"))
        p(f"  目录      {ctx.cwd}")
        p(f"  仓库根    {ctx.repo_root or C.dim('(不在 git 仓库中)')}")
        p(f"  CLI 版本  {claude_version()}")
        gi = assets["gitignore"]
        if gi == "tracked":
            p("  " + C.red("⚠ .claude/settings.local.json 已被 git 跟踪 —— 个人权限批准会泄漏给团队"))
        elif gi == "untracked":
            p("  " + C.yellow("⚠ .claude/settings.local.json 未被 gitignore 覆盖"))

        p(hr("配置来源 (优先级由低到高)"))
        for sf in sorted(ctx.sources, key=lambda s: SCOPE_RANK[s.scope]):
            if sf.error:
                mark, note = C.red("✗"), C.red(sf.error)
            elif not sf.exists:
                mark, note = C.dim("·"), C.dim("不存在")
            elif not sf.data:
                mark, note = C.dim("·"), C.dim("空")
            else:
                mark, note = C.green("✓"), C.dim(f"{len(sf.data)} 个顶层键")
            p(f"  {mark} {tag(sf.scope)} {pad(short(sf.path, ctx), 46)} {note}")
        p(C.dim("  注：命令行参数优先级高于 local/project/user，低于 managed，本工具无法探知。"))

    if "settings" in sections:
        p(hr("生效配置"))
        rows = []
        for path in sorted(prov):
            if path.startswith("permissions.") or path.startswith("hooks"):
                continue
            entries = prov[path]
            scope = effective_scope(entries)
            val = entries[-1][2]
            if path.split(".")[-1] or True:
                val = redact(val, path.split(".")[-1], ctx.show_secrets)
            overridden = len(entries) > 1
            rows.append((path, fmt_value(val), scope, overridden, entries))
        if not rows:
            p(C.dim("  (无)"))
        for path, val, scope, overridden, entries in rows:
            flag = C.dim(" ←覆盖了 " + ",".join(SCOPE_LABEL[e[0]].strip() for e in entries[:-1])) if overridden else ""
            p(f"  {tag(scope)} {C.cyan(pad(path, 34))} {val}{flag}")

    if "perms" in sections:
        p(hr("权限规则 (跨 scope 合并，不覆盖)"))
        any_rule = False
        for kind, color in (("deny", C.red), ("ask", C.yellow), ("allow", C.green)):
            key = f"permissions.{kind}"
            entries = prov.get(key, [])
            if not entries:
                continue
            any_rule = True
            for scope, _, rules in entries:
                for r in rules if isinstance(rules, list) else [rules]:
                    label = color(f"{kind.upper():<5}")
                    p(f"  {tag(scope)} {label} {r}")
        mode = merged.get("defaultMode") or (merged.get("permissions") or {}).get("defaultMode")
        if mode:
            p(f"  {C.dim('默认模式')} {mode}")
        if not any_rule:
            p(C.dim("  (未定义任何规则)"))

    if "hooks" in sections:
        p(hr("Hooks"))
        hooks = merged.get("hooks") or {}
        if merged.get("disableAllHooks"):
            p("  " + C.red("disableAllHooks=true —— 以下全部不执行"))
        if not hooks:
            p(C.dim("  (无)"))
        for event, matchers in hooks.items():
            p(f"  {C.bold(event)}")
            for m in matchers if isinstance(matchers, list) else [matchers]:
                pat = m.get("matcher", "*") if isinstance(m, dict) else "*"
                for h in (m.get("hooks") or []) if isinstance(m, dict) else []:
                    cmd = h.get("command") or h.get("url") or ""
                    src = prov.get(f"hooks.{event}") or prov.get("hooks") or []
                    sc = effective_scope(src) if src else "?"
                    p(f"    {tag(sc)} {C.dim(pat):<18} {fmt_value(cmd, 70)}")

    if "mcp" in sections:
        p(hr("MCP Servers"))
        mcp = assets["mcp"]
        if not mcp:
            p(C.dim("  (无)"))
        for s in mcp:
            status = s["status"]
            sc = C.green if status == "已批准" else (C.yellow if status == "待批准" else C.red)
            target = fmt_value(s["target"], 44)
            p(f"  {tag(s['scope'])} {C.cyan(pad(s['name'], 20))} {s['transport']:<6} {target}")
            p(f"       {C.dim(pad(s['source'], 40))} {sc(status)}")
        if merged.get("disableClaudeAiConnectors"):
            p("  " + C.dim("claude.ai 连接器已禁用 (disableClaudeAiConnectors)"))

    if "memory" in sections:
        p(hr("记忆 (CLAUDE.md)"))
        mem = assets["memory"]
        if not mem:
            p(C.dim("  (无)"))
        total = 0
        for m in mem:
            total += m["tokens"]
            imp = C.dim(f"  @导入 {len(m['imports'])}") if m["imports"] else ""
            p(f"  {tag(m['scope'])} {pad(short(Path(m['path']), ctx), 44)} "
              f"{m['lines']:>4} 行  ~{m['tokens']:>5} tok{imp}")
        if mem:
            p(C.dim(f"  合计 ~{total} tokens 常驻上下文"))

    for kind, title in (("agents", "Subagents"), ("commands", "自定义命令"), ("skills", "Skills")):
        if kind not in sections:
            continue
        p(hr(title))
        items = assets[kind]
        if not items:
            p(C.dim("  (无)"))
        for a in items:
            desc = fmt_value(a["description"], 46) if a["description"] else C.dim("(无描述)")
            extra = []
            if a.get("model"):
                extra.append(a["model"])
            if a.get("tools"):
                extra.append(f"tools:{fmt_value(a['tools'], 20)}")
            suffix = C.dim("  " + " ".join(extra)) if extra else ""
            p(f"  {tag(a['scope'])} {C.cyan(pad(a['name'], 24))} {desc}{suffix}")

    if "plugins" in sections:
        p(hr("插件"))
        items = assets["plugins"]
        if not items:
            p(C.dim("  (无)"))
        for pl in items:
            state = "" if pl["enabled"] is None else (C.green("启用") if pl["enabled"] else C.dim("停用"))
            p(f"  {C.cyan(pad(pl['name'], 28))} {state:<10} {C.dim(pl['source'])}")

    if ctx.problems and "env" in sections:
        p(hr("需要注意"))
        for prob in ctx.problems:
            p(f"  {C.red('✗')} {prob}")

    p()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
ALL_SECTIONS = ["env", "settings", "perms", "hooks", "mcp", "memory",
                "agents", "commands", "skills", "plugins"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="cx", description="检视当前目录下 Claude Code 的生效配置"
    )
    ap.add_argument("--path", default=".", help="要检视的目录 (默认: 当前目录)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--show-secrets", action="store_true", help="不脱敏 env / MCP 中的密钥")
    ap.add_argument("--section", action="append", choices=ALL_SECTIONS,
                    help="只输出指定小节，可重复")
    ap.add_argument("--version", action="version", version=f"cx {VERSION}")
    args = ap.parse_args(argv)

    cwd = Path(args.path).resolve()
    if not cwd.is_dir():
        print(f"cx: 目录不存在: {cwd}", file=sys.stderr)
        return 2

    ctx = Ctx(cwd=cwd, repo_root=find_repo_root(cwd), home=Path.home(),
              show_secrets=args.show_secrets)
    discover_sources(ctx)
    merged, prov = merge_with_provenance(ctx.sources)

    assets = {
        "memory": scan_memory(ctx),
        "agents": scan_md_assets(ctx, "agents"),
        "commands": scan_md_assets(ctx, "commands"),
        "skills": scan_md_assets(ctx, "skills"),
        "mcp": scan_mcp(ctx, merged),
        "plugins": scan_plugins(ctx, merged),
        "gitignore": gitignore_status(ctx),
    }

    if args.json:
        payload = {
            "cx_version": VERSION,
            "cwd": str(ctx.cwd),
            "repo_root": str(ctx.repo_root) if ctx.repo_root else None,
            "sources": [
                {"scope": s.scope, "path": str(s.path), "exists": s.exists,
                 "error": s.error, "keys": sorted(s.data.keys())}
                for s in ctx.sources
            ],
            "effective": redact(merged, show=ctx.show_secrets),
            "provenance": {
                k: [{"scope": sc, "path": str(pp),
                     "value": redact(v, k.split(".")[-1], ctx.show_secrets)}
                    for sc, pp, v in entries]
                for k, entries in prov.items()
            },
            "assets": {k: v for k, v in assets.items() if k != "gitignore"},
            "settings_local_git_status": assets["gitignore"],
            "problems": ctx.problems,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    sections = set(args.section) if args.section else set(ALL_SECTIONS)
    render(ctx, merged, prov, assets, sections)
    return 0


if __name__ == "__main__":
    sys.exit(main())

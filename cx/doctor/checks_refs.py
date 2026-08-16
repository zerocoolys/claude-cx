"""refs —— 失效引用。这类问题会静默失败，用户最难自己发现。"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from cx.doctor.registry import Finding, Probe, check
from cx.util import fmt_value

# 出现这些字符就说明是 shell 片段，cx 不解析，降级为 info
SHELL_META = "|&;<>$`\n"


def _executable_exists(raw: str) -> bool:
    exe = os.path.expanduser(raw)
    if os.sep in exe or exe.startswith("."):
        return os.path.isfile(exe) and os.access(exe, os.X_OK)
    return bool(shutil.which(exe))


@check
def check_hook_commands(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    hooks = probe.merged.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, matchers in hooks.items():
        if not isinstance(matchers, list):
            continue
        for m in matchers:
            if not isinstance(m, dict):
                continue
            entries = m.get("hooks")
            if not isinstance(entries, list):
                continue
            for h in entries:
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command")
                if not isinstance(cmd, str) or not cmd.strip():
                    continue
                out.extend(_check_one_command(str(event), cmd))
    return out


def _check_one_command(event: str, cmd: str) -> list[Finding]:
    where = f"hooks.{event}"
    if any(ch in cmd for ch in SHELL_META):
        # 命令原文可能含内联密钥（如 curl -H "Authorization: Bearer sk-..."）。
        # redact() 只按字典 key 匹配，对裸字符串无能为力，所以这里改用截断
        # 降低（但不能消除）泄露面，而不是把整条命令原样塞进 finding。
        return [Finding(
            id="refs.hook-command-missing",
            severity="info",
            title=f"{event} hook 的命令含 shell 语法，cx 无法校验",
            detail=f"命令是 shell 片段，cx 不做解析：{fmt_value(cmd)}",
            where=where,
            fix="若该 hook 未生效，请手动确认其中每个可执行文件都存在且可执行",
        )]
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return []
    if not tokens:
        return []
    exe = tokens[0]
    if _executable_exists(exe):
        return []
    return [Finding(
        id="refs.hook-command-missing",
        severity="error",
        title=f"{event} hook 的命令找不到",
        detail=f"{exe} 既不在 PATH 中，也不是一个可执行文件。该 hook 会静默失败。",
        where=where,
        fix=f"确认 {exe} 存在且有执行权限，或从配置中移除这个 hook",
    )]


def _spec_of(server: dict) -> dict:
    spec = server.get("spec")
    return spec if isinstance(spec, dict) else {}


def _transport(spec: dict) -> str:
    return spec.get("type") or ("http" if spec.get("url") else "stdio")


@check
def check_mcp_commands(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    for server in probe.assets.get("mcp") or []:
        spec = _spec_of(server)
        if _transport(spec) != "stdio":
            continue
        cmd = spec.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        if _executable_exists(cmd):
            continue
        out.append(Finding(
            id="refs.mcp-command-missing",
            severity="error",
            title=f"MCP server {server.get('name')} 的命令找不到",
            detail=f"{cmd} 不在 PATH 中，该 server 无法启动。",
            where=str(server.get("source", "")),
            fix=f"安装 {cmd}，或修正 command 字段",
        ))
    return out


@check
def check_mcp_urls(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    for server in probe.assets.get("mcp") or []:
        spec = _spec_of(server)
        if _transport(spec) == "stdio":
            continue
        url = spec.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        parts = urlsplit(url.strip())
        if parts.scheme in {"http", "https"} and parts.netloc:
            continue
        out.append(Finding(
            id="refs.mcp-url-invalid",
            severity="warn",
            title=f"MCP server {server.get('name')} 的 URL 格式非法",
            detail=f"{url} 不是一个带 http/https 协议和主机名的 URL。"
                   f"（cx 只做格式校验，不发网络请求）",
            where=str(server.get("source", "")),
            fix="修正 url 字段，形如 https://host/path",
        ))
    return out


@check
def check_memory_imports(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    for mem in probe.assets.get("memory") or []:
        md_path = Path(str(mem.get("path", "")))
        base = md_path.parent
        for imp in mem.get("imports") or []:
            target = Path(os.path.expanduser(str(imp)))
            if not target.is_absolute():
                target = base / target
            if target.exists():
                continue
            out.append(Finding(
                id="refs.memory-import-missing",
                severity="error",
                title="CLAUDE.md 的 @导入 指向不存在的文件",
                detail=f"@{imp} 解析为 {target}，该路径不存在。这段记忆不会被加载。",
                where=str(md_path),
                fix=f"创建 {target}，或从 {md_path.name} 中删除这行 @导入",
            ))
    return out

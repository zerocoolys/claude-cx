"""conflicts —— 静默覆盖与冲突。

这类问题不会报错，只会让你的设置悄悄不生效。
"""

from __future__ import annotations

from cx.doctor.registry import Finding, Probe, check
from cx.model import MERGE_ONLY_KEYS, SCOPE_LABEL


def _scope_names(entries: list) -> list[str]:
    return [SCOPE_LABEL.get(e[0], e[0]).strip() for e in entries]


@check
def check_managed_override(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    for path, entries in probe.prov.items():
        if path in MERGE_ONLY_KEYS:
            continue
        if len(entries) < 2 or entries[-1][0] != "managed":
            continue
        losers = ", ".join(_scope_names(entries[:-1]))
        out.append(Finding(
            id="conflicts.managed-override",
            severity="warn",
            title=f"managed 策略覆盖了你的 {path} 设置",
            detail=f"你在 {losers} 里设过 {path}，但企业 managed 配置"
                   f"优先级最高，实际生效的是 managed 的值。",
            where=path,
            fix="这是企业策略，本地无法覆盖；如需变更请联系管理员",
        ))
    return out


@check
def check_shadowed_keys(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    for path, entries in probe.prov.items():
        if path in MERGE_ONLY_KEYS:
            continue
        if len(entries) < 2:
            continue
        if entries[-1][0] == "managed":
            continue  # 由 check_managed_override 负责，不重复报
        winner = SCOPE_LABEL.get(entries[-1][0], entries[-1][0]).strip()
        losers = ", ".join(_scope_names(entries[:-1]))
        out.append(Finding(
            id="conflicts.shadowed-key",
            severity="info",
            title=f"{path} 在多个 scope 都有定义",
            detail=f"{losers} 里的值被 {winner} 覆盖。这通常是有意为之，"
                   f"列出来只是让你知道其余几处不生效。",
            where=path,
            fix="若某处设置意外不生效，删除更高优先级 scope 里的同名键",
        ))
    return out


@check
def check_deny_shadows_allow(probe: Probe) -> list[Finding]:
    perms = probe.merged.get("permissions")
    if not isinstance(perms, dict):
        return []
    allow = {r for r in (perms.get("allow") or []) if isinstance(r, str)}
    deny = {r for r in (perms.get("deny") or []) if isinstance(r, str)}
    # 刻意只比字符串完全相等：判断 Bash(npm run *) 是否遮蔽 Bash(npm run test)
    # 需要完整的规则匹配语义，做不准就是误报源。
    return [
        Finding(
            id="conflicts.deny-shadows-allow",
            severity="warn",
            title=f"同一条规则同时出现在 allow 和 deny: {rule}",
            detail="deny 优先级高于 allow，这条 allow 永远不会生效。",
            where="permissions",
            fix=f"从 allow 或 deny 中删掉 {rule}，明确你要哪一个",
        )
        for rule in sorted(allow & deny)
    ]


@check
def check_legacy_local_settings(probe: Probe) -> list[Finding]:
    locals_present = [
        sf for sf in probe.ctx.sources
        if sf.scope == "local" and sf.exists and sf.data
    ]
    if len(locals_present) < 2:
        return []
    paths = ", ".join(str(sf.path) for sf in locals_present)
    return [Finding(
        id="conflicts.legacy-local-settings",
        severity="warn",
        title="存在多份 settings.local.json，它们会叠加生效",
        detail=f"检测到 {len(locals_present)} 份：{paths}。"
               f"Claude Code 2.1.211 之前 settings.local.json 落在启动目录，"
               f"之后改为仓库根；旧文件仍会被读取，两份都在影响你的配置。",
        where=paths,
        fix="确认哪一份是你想要的，删掉另一份",
    )]


@check
def check_mcp_name_collision(probe: Probe) -> list[Finding]:
    by_name: dict[str, set[str]] = {}
    for server in probe.assets.get("mcp") or []:
        name = str(server.get("name", ""))
        if not name:
            continue
        by_name.setdefault(name, set()).add(str(server.get("scope", "")))

    return [
        Finding(
            id="conflicts.mcp-name-collision",
            severity="warn",
            title=f"MCP server {name} 在多个 scope 重名",
            detail=f"{name} 同时定义在 {', '.join(sorted(scopes))}。"
                   f"重名会让你难以判断实际连上的是哪一个。",
            where=f"mcpServers.{name}",
            fix=f"给其中一个改名，或删掉多余的定义",
        )
        for name, scopes in sorted(by_name.items())
        if len(scopes) > 1
    ]

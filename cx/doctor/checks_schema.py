"""schema —— 拼写与结构错误。

未知键漂移的应对见 spec 4.3.1：只报与已知键编辑距离为 1 的同层级未知键。
距离 >= 2 或完全陌生的键一律沉默——那更可能是 Claude Code 新版本的新键。
"""

from __future__ import annotations

from cx.doctor.registry import Finding, Probe, check
from cx.util import fmt_value, redact

# 已知的顶层配置键。清单会随 Claude Code 版本漂移，因此只用于
# 「距离为 1」的近似匹配，不用于「未知即报错」。
KNOWN_TOP_KEYS = {
    "alwaysThinkingEnabled", "apiKeyHelper", "autoUpdates", "awsAuthRefresh",
    "awsCredentialExport", "cleanupPeriodDays", "disableAllHooks",
    "disableClaudeAiConnectors", "disabledMcpjsonServers", "editorMode",
    "enableAllProjectMcpServers", "enabledMcpjsonServers", "enabledPlugins",
    "env", "fallbackModel", "forceLoginMethod", "hooks", "includeCoAuthoredBy",
    "model", "outputStyle", "permissions", "plugins", "sandbox", "statusLine",
}

KNOWN_PERMISSION_KEYS = {
    "additionalDirectories", "allow", "ask", "defaultMode", "deny",
    "disableBypassPermissionsMode",
}

# 放错层级的键：{错误位置: 正确位置}
MISPLACED_KEYS = {
    "defaultMode": "permissions.defaultMode",
    "allow": "permissions.allow",
    "deny": "permissions.deny",
    "ask": "permissions.ask",
    "additionalDirectories": "permissions.additionalDirectories",
}

# 这些子树下是自由命名，不参与拼写检查
TYPO_EXEMPT_SUBTREES = {"env", "hooks", "enabledPlugins", "plugins"}

ASSET_KINDS = ("agents", "commands", "skills")


def _distance_one(a: str, b: str) -> bool:
    """a 与 b 的 Levenshtein 距离是否恰为 1。

    只判定「恰好 1」，不算出完整距离——这是唯一需要的语义，也更快。
    换位（modle vs model）算距离 2，故返回 False。
    """
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if not a or not b:
        return False
    if la == lb:  # 单字符替换
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if la > lb:  # 保证 a 是较短的那个
        a, b, la, lb = b, a, lb, la
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]  # 单字符插入


def _nearest(key: str, known: set[str]) -> str | None:
    for candidate in sorted(known):
        if _distance_one(key, candidate):
            return candidate
    return None


@check
def check_json_syntax(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    for sf in probe.ctx.sources:
        if not sf.error:
            continue
        out.append(Finding(
            id="schema.json-syntax",
            severity="error",
            title=f"{sf.scope} 配置文件无法解析",
            detail=f"{sf.error}。该文件会被 Claude Code 整体忽略，"
                   f"里面的权限、hook、MCP 设置全部不生效。",
            where=str(sf.path),
            fix="修正 JSON 语法（常见原因：尾随逗号、缺引号、注释）",
        ))
    return out


@check
def check_misplaced_keys(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    for wrong, right in MISPLACED_KEYS.items():
        if wrong not in probe.merged:
            continue
        out.append(Finding(
            id="schema.misplaced-key",
            severity="error",
            title=f"配置键 {wrong} 放错了层级",
            detail=f"{wrong} 出现在顶层，但 Claude Code 只读取 {right}。"
                   f"当前这份设置不生效。",
            where=wrong,
            fix=f"把 {wrong} 移到 {right}",
        ))
    return out


@check
def check_likely_typos(probe: Probe) -> list[Finding]:
    out: list[Finding] = []

    for key in probe.merged:
        if key in KNOWN_TOP_KEYS or key in TYPO_EXEMPT_SUBTREES:
            continue
        if key in MISPLACED_KEYS:
            continue  # 由 check_misplaced_keys 负责，避免重复报
        near = _nearest(key, KNOWN_TOP_KEYS)
        if near:
            out.append(_typo_finding(key, near, key))

    perms = probe.merged.get("permissions")
    if isinstance(perms, dict):
        for key in perms:
            if key in KNOWN_PERMISSION_KEYS:
                continue
            near = _nearest(key, KNOWN_PERMISSION_KEYS)
            if near:
                out.append(_typo_finding(
                    key, near, f"permissions.{key}", prefix="permissions."))
    return out


def _typo_finding(key: str, near: str, where: str, prefix: str = "") -> Finding:
    return Finding(
        id="schema.likely-typo",
        severity="warn",
        title=f"配置键 {key} 疑似拼写错误",
        detail=f"{key} 不是已知配置键，且与 {prefix}{near} 只差一个字符。"
               f"拼错的键会被静默忽略。",
        where=where,
        fix=f"如果本意是 {prefix}{near}，请改正；"
            f"如果这是 Claude Code 新版本的新键，用 "
            f"--ignore schema.likely-typo 抑制",
    )


@check
def check_asset_frontmatter(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    for kind in ASSET_KINDS:
        for asset in probe.assets.get(kind) or []:
            missing = [
                field for field in ("name", "description")
                if not str(asset.get(field) or "").strip()
            ]
            if not missing:
                continue
            out.append(Finding(
                id="schema.asset-missing-frontmatter",
                severity="warn",
                title=f"{kind} 资产缺少 frontmatter 字段: {', '.join(missing)}",
                detail=f"缺 description 的资产 Claude 无从判断何时该用它；"
                       f"缺 name 时会回退为文件名。",
                where=str(asset.get("path", "")),
                fix=f"在文件顶部的 --- 块里补上 {', '.join(missing)}",
            ))
    return out


@check
def check_hook_shape(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    hooks = probe.merged.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, matchers in hooks.items():
        where = f"hooks.{event}"
        if not isinstance(matchers, list):
            out.append(_shape_finding(where, "该事件的值不是数组"))
            continue
        for m in matchers:
            if not isinstance(m, dict):
                out.append(_shape_finding(
                    where, f"matcher 项不是对象: {_safe_repr(m)}"))
                continue
            entries = m.get("hooks")
            if not isinstance(entries, list):
                out.append(_shape_finding(where, "matcher 缺少 hooks 数组"))
                continue
            for h in entries:
                if not isinstance(h, dict):
                    out.append(_shape_finding(
                        where, f"hook 项不是对象: {_safe_repr(h)}"))
                elif not str(h.get("command") or h.get("url") or "").strip():
                    out.append(_shape_finding(where, "hook 项缺少 command/url 字段"))
    return out


def _safe_repr(value) -> str:
    """脱敏后再截断，避免把配置里的密钥值原样塞进 finding 文本。"""
    return fmt_value(redact(value))


def _shape_finding(where: str, detail: str) -> Finding:
    return Finding(
        id="schema.hook-malformed",
        severity="error",
        title="hooks 配置结构不符合预期",
        detail=f"{detail}。结构不符的 hook 不会执行。",
        where=where,
        fix="参考文档修正结构：hooks.<事件> 是数组，"
            "每项含 matcher 与 hooks，hooks 每项含 command",
    )

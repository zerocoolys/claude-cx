"""security —— 密钥泄漏、过宽权限、上下文预算。"""

from __future__ import annotations

import re

from cx.doctor.registry import Finding, Probe, check
from cx.model import SECRET_PAT

# 值形如 ${VAR} 或 $VAR 的是环境变量引用，不是明文密钥
VAR_REF = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$")

# 高危的过宽 allow 规则。刻意只列已知模式，不做通用宽度分析——
# 通用分析需要完整的规则匹配语义，做不准就是误报源。
BROAD_ALLOW_RULES = {
    "Bash", "Bash(*)", "Bash(*:*)", "Read(//**)", "Edit(//**)",
    "Write(//**)", "WebFetch", "*",
}


@check
def check_local_settings_git(probe: Probe) -> list[Finding]:
    status = probe.assets.get("gitignore")
    rel = ".claude/settings.local.json"
    if status == "tracked":
        return [Finding(
            id="security.local-settings-tracked",
            severity="error",
            title="settings.local.json 已被 git 跟踪",
            detail="这个文件保存你个人的权限批准记录。提交上去等于把"
                   "你批准过的所有工具调用权限推给整个团队。",
            where=rel,
            fix=f"git rm --cached {rel} 并把它加进 .gitignore",
        )]
    if status == "untracked":
        return [Finding(
            id="security.local-settings-unignored",
            severity="warn",
            title="settings.local.json 未被 .gitignore 覆盖",
            detail="目前还没被提交，但下一次 git add -A 就会带上去。",
            where=rel,
            fix=f"把 {rel} 加进 .gitignore",
        )]
    return []


@check
def check_plaintext_secrets(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    env = probe.merged.get("env")
    if not isinstance(env, dict):
        return out
    for key, value in env.items():
        if not isinstance(value, str) or not value.strip():
            continue
        if not SECRET_PAT.search(str(key)):
            continue
        if VAR_REF.match(value.strip()):
            continue
        # 注意：detail 里绝不能出现 value 本身——finding 会被打印、进 CI 日志
        out.append(Finding(
            id="security.plaintext-secret",
            severity="warn",
            title=f"env.{key} 疑似是明文密钥",
            detail=f"{key} 的值是字面量而非 ${{变量}} 引用。"
                   f"settings.json 常常进版本库，明文密钥会随之泄漏。",
            where=f"env.{key}",
            fix=f"改成 ${{{key}}} 并由 shell 环境提供，"
                f"或改用 apiKeyHelper",
        ))
    return out


@check
def check_broad_allow(probe: Probe) -> list[Finding]:
    out: list[Finding] = []
    perms = probe.merged.get("permissions")
    if not isinstance(perms, dict):
        return out
    rules = perms.get("allow")
    if not isinstance(rules, list):
        return out
    for rule in rules:
        if not isinstance(rule, str) or rule.strip() not in BROAD_ALLOW_RULES:
            continue
        out.append(Finding(
            id="security.broad-allow",
            severity="warn",
            title=f"allow 规则过宽: {rule}",
            detail=f"{rule} 会免确认地放行整类操作。一旦模型被"
                   f"提示注入误导，这条规则就是没有护栏的执行通道。",
            where="permissions.allow",
            fix=f"把 {rule} 收窄成具体的命令或路径，"
                f"例如 Bash(git status) 而非 Bash(*)",
        ))
    return out


@check
def check_context_budget(probe: Probe) -> list[Finding]:
    memory = probe.assets.get("memory") or []
    total = sum(int(m.get("tokens") or 0) for m in memory)
    if total <= probe.budget:
        return []
    files = len(memory)
    return [Finding(
        id="security.context-budget",
        severity="info",
        title=f"CLAUDE.md 常驻上下文约 {total} token，超出预算 {probe.budget}",
        detail=f"{files} 个记忆文件合计约 {total} token，每轮对话都会加载。"
               f"过大的常驻上下文会挤占可用窗口并抬高成本。",
        where="CLAUDE.md",
        fix="精简记忆文件，或用 --budget N 调整阈值",
    )]

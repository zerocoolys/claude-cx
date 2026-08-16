from __future__ import annotations

from pathlib import Path

from cx.merge import effective_scope
from cx.model import Ctx, SCOPE_LABEL, SCOPE_RANK, VERSION
from cx.scan import claude_version
from cx.term import C, hr, tag
from cx.util import fmt_value, pad, redact, short


def render(ctx: Ctx, merged: dict, prov: dict, assets: dict, sections: set[str],
           findings=None) -> None:
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

    if findings is not None and "env" in sections:
        errors = [f for f in findings if f.severity == "error"]
        if errors:
            p(hr("需要注意"))
            for f in errors:
                p(f"  {C.red('✗')} {C.cyan(f.id)}  {f.title}")
                p(f"      {C.dim(f.where)}")
                p(f"      {C.dim('修复')}  {f.fix}")
            rest = len(findings) - len(errors)
            if rest:
                p(C.dim(f"  另有 {rest} 项 warn/info，运行 cx doctor 查看全部"))
            else:
                p(C.dim("  运行 cx doctor 查看完整诊断"))
    elif findings is None and ctx.problems and "env" in sections:
        # 兼容未传 findings 的旧调用点
        p(hr("需要注意"))
        for prob in ctx.problems:
            p(f"  {C.red('✗')} {prob}")

    p()

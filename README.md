# cx

一屏看清当前目录下 Claude Code 的生效配置，每一项都标注来源 scope。

Claude Code 的配置分散在最多五个层级（managed / 命令行参数 / local / project / user），
不同功能的存放位置还不一致：subagents 只有 user 和 project 两层，MCP server 则同时散落在
`~/.claude.json`、`~/.claude.json` 的 per-project 段落和 `.mcp.json` 三处。
`cx` 把它们合并成一份带来源标注的报告。

零依赖，纯只读，不修改任何文件。

## 安装

```bash
uvx cx                      # 免安装直接跑
pipx install cx-claude      # 或装到本地
```

## 用法

```bash
cx                          # 完整报告
cx show skills               # 只看某个模块
cx show mcp,perms,hooks     # 逗号分隔看多个
cx doctor                   # 诊断配置问题
cx doctor --fail-on warn    # CI 里用，warn 及以上即失败
cx doctor --json            # 机器可读
cx --json                   # 完整配置的机器可读输出
cx --show-secrets           # 不脱敏（默认脱敏）
cx --path DIR               # 检视指定目录
```

小节名：`env` `settings` `perms` `hooks` `mcp` `memory` `agents` `commands` `skills` `plugins`

`--section` 已 deprecated，等价于 `cx show <模块>`，暂不移除。

### doctor

`cx doctor` 跑 19 项只读检查，分四类：

- `refs` —— hook 命令、MCP command、CLAUDE.md 的 `@导入` 是否指向真实存在的东西
- `conflicts` —— managed 策略的静默覆盖、跨 scope 遮蔽、MCP 重名
- `schema` —— JSON 语法、键放错层级、疑似拼写错误、frontmatter 缺字段
- `security` —— `settings.local.json` 泄漏、明文密钥、过宽 allow 规则、上下文预算

退出码：0 = 未触阈值，1 = 触阈值，2 = 用法错误。阈值由 `--fail-on` 控制，默认 `error`。

误报可以抑制：`cx doctor --ignore schema.likely-typo`（可重复、可逗号分隔）。
被抑制的项仍在 `--json` 输出里，标记 `ignored: true`。

## 输出样例

```
cx 0.2.0  ·  Claude Code 配置状态
  目录      /Users/me/projects/harness
  仓库根    /Users/me/projects/harness
  CLI 版本  2.1.x

── 配置来源 (优先级由低到高) ─────────────────────────────
  ✓ [user ] ~/.claude/settings.json                5 个顶层键
  ✓ [proj ] ./.claude/settings.json                4 个顶层键
  ✓ [local] ./.claude/settings.local.json          2 个顶层键
  · [MGMT ] /etc/claude-code/managed-settings.json 不存在

── 生效配置 ──────────────────────────────────────────────
  [user ] env.ANTHROPIC_API_KEY   sk-a••••yz (22 chars)
  [local] model                   claude-haiku-4-5 ←覆盖了 user,proj

── 权限规则 (跨 scope 合并，不覆盖) ───────────────────────
  [user ] DENY  Read(./.env)
  [proj ] DENY  Bash(curl *)
  [local] ALLOW Bash(rm -rf build)

── MCP Servers ───────────────────────────────────────────
  [proj ] kensho      http  https://kfinance.kensho.com/…
       ./.mcp.json                        已批准
  [proj ] cryptocom   http  https://mcp.crypto.com/…
       ./.mcp.json                        待批准
```

## 设计要点

**保留完整来源链。** `←覆盖了 user,proj` 说明这个值经过了三层。
「我明明在用户设置里写了 opus，为什么跑的是 haiku」这类问题一眼可见。
`--json` 模式下 `provenance.<key>` 给出全部贡献者的有序数组，末位为生效值。

**权限规则单独成节。** 它们的合并语义和其他 key 不同——跨 scope 累加而非覆盖，
三层的规则会同时生效。混在「生效配置」里显示会造成误解。
`fallbackModel` 反过来，是整条链替换而非拼接，也做了特殊处理。

**MCP 审批状态。** `.mcp.json` 里的 server 默认需要批准。工具对照
`enabledMcpjsonServers` / `disabledMcpjsonServers` / `enableAllProjectMcpServers`
算出每个 server 的实际状态。

**坏 JSON 显式报错。** user / project / local 三层是严格校验的，一个多余的逗号
会让整个文件被拒绝，而这在交互界面里不总是显眼。`cx` 直接标 ✗ 并给出行列号。
这是 hook 和权限规则「配了但不生效」最常见的原因。

**默认脱敏。** `env` 和 MCP 配置中键名匹配 key/token/secret/password 等字样的值会打码，
`--show-secrets` 才显示原文。所以输出可以放心贴给别人看。

## 已知边界

- 命令行参数那一层（优先级在 local 之上、managed 之下）无法探测，输出中已显式标注。
- managed scope 只读文件形式（`managed-settings.json` 及 `managed-settings.d/`），
  未读 macOS plist 和 Windows 注册表两种 MDM 下发方式。
- token 估算是粗略的（中文按字符计，其余按 4 字符/token），只用于横向比较占用，
  不要当精确值用。

## 相关

`cx` 不替代 Claude Code 内置的诊断能力，两者关注点不同：

- `claude doctor` — 校验配置合法性、列出 managed 设置中被剔除的条目
- `/status` — 确认本次会话实际加载了哪些设置源
- `/context` — 当前上下文占用

`cx` 补的是跨 scope 的来源追溯和机器可读输出，方便在 CI 或定时任务里无人值守地跑。

官方配置文档：https://code.claude.com/docs/en/settings

## License

MIT

# cx doctor 设计文档

- 日期：2026-08-16
- 状态：待实现
- 范围：子系统 C（doctor 诊断），以及它所依赖的 `cx.py` 拆包重构

## 1. 背景与定位变更

cx 原本的定位是**只读检视器**，`CLAUDE.md` 里写着「纯只读：cx 只读取和展示配置，不写入、不修改任何文件。任何写操作都是 bug」。

现决定把 cx 的定位改为**全面配置管理工具**：读和写地位对等，支持 env 变量编辑、诊断、配置导入导出。

这是一次产品定位变更，会连带打破两条既有约束：

1. **纯只读** —— 将来的写入能力直接推翻它。本文档涉及的 doctor 是只读的，因此**本轮不动这条约束**，留给子系统 A。
2. **单模块 / curl 单文件分发** —— `CLAUDE.md` 里「不要拆包」的真正理由是「用户是 `curl` 单文件直接跑的」。全面 CRUD 塞不进单个 800 行文件，因此**本轮放弃 curl 单文件分发**，改为 `uvx cx` / `pipx install cx`。零第三方依赖的约束保留不变。

### 1.1 子系统拆解

用户描述的能力是四个互相独立的子系统，各自走一轮 spec → plan → 实现：

| 子系统 | 内容 | 依赖 |
|---|---|---|
| A. 写入基础设施 | 目标文件定位、原子写、备份回滚、保留注释格式、dry-run、`--write` 门禁 | 无 |
| B. env 编辑 | `cx env set/unset/list`，选 scope，值校验，密钥脱敏 | A |
| **C. doctor** | **只读诊断（本文档）** | **无** |
| D. 导入导出 | 序列化生效配置、跨机迁移、导入时的权限管控与冲突合并 | A |

先做 C：零风险、不依赖写入能力、不改动现有约束，可立即交付价值。

### 1.2 doctor 的现状基线

cx 今天已有诊断雏形，doctor 的工作是把它们提升为一等公民并大幅扩充：

- `ctx.problems` 收集 JSON 语法错误（`cx.py:250`）
- `gitignore_status()` 检测 `settings.local.json` 泄漏（`cx.py:473`）
- MCP server 的待批准状态（`cx.py:441`）

## 2. 文件布局与拆包迁移

### 2.1 目标布局

```
cx/__init__.py              # 仅做再导出，保持 `import cx` 的公开 API 不变
cx/cli.py                   # argparse subparsers 分发，main()
cx/model.py                 # SourceFile / Ctx / SCOPES / SCOPE_RANK / 常量
cx/util.py                  # load_json / redact / short / fmt_value / count_tokens_rough / disp_width
cx/discovery.py             # managed_dirs / discover_sources / find_repo_root
cx/merge.py                 # merge_with_provenance / effective_scope
cx/scan.py                  # scan_md_assets / scan_memory / scan_mcp / scan_plugins / gitignore_status
cx/term.py                  # C / tag / hr / pad
cx/render.py                # render()
cx/doctor/__init__.py       # 导入四个 checks_* 模块以触发注册
cx/doctor/registry.py       # Finding / Probe / @check 注册 / run_checks
cx/doctor/checks_refs.py
cx/doctor/checks_conflicts.py
cx/doctor/checks_schema.py
cx/doctor/checks_security.py
cx/doctor/render.py         # doctor 的人读输出 + JSON 输出
```

### 2.2 迁移方式

**纯搬运，不改任何现有函数的签名或行为。** `cx/__init__.py` 把现有全部顶层名字再导出，因此 `tests/test_cx.py` 的 `import cx` + `cx.merge_with_provenance(...)` 一行不用改。

拆包作为**独立的第一个提交**，不夹带任何 doctor 代码，出问题可单独回滚。

### 2.3 pyproject 改动

- `[tool.setuptools] py-modules = ["cx"]` → `[tool.setuptools.packages.find]`
- `[project.scripts] cx = "cx:main"` → `cx = "cx.cli:main"`

### 2.4 CLAUDE.md 改动

- 删除「单模块：全部实现在 cx.py，不要拆包」
- 「零依赖」保留，但删除「用户是 `curl` 单文件直接跑的」，改为记录 `uvx cx` 分发方式
- 新增包结构说明
- 「纯只读」本轮**不动**

## 3. 数据模型与注册表

### 3.1 Finding

```python
@dataclass(frozen=True)
class Finding:
    id: str        # 稳定标识，形如 "refs.hook-command-missing"
    severity: str  # "error" | "warn" | "info"
    title: str     # 一行摘要
    detail: str    # 具体是什么、为什么有问题
    where: str     # 出问题的文件路径，或点分配置键路径
    fix: str       # 怎么修，一句话或一条可执行命令
```

`id` 是**公开契约**：`--ignore` 靠它抑制、JSON 消费方靠它匹配，一旦发布不再改名。命名固定为 `<类别>.<kebab-名>`，类别限于 `refs` / `conflicts` / `schema` / `security`，外加运行时保留的 `internal`。

### 3.2 Probe

```python
@dataclass(frozen=True)
class Probe:
    ctx: Ctx
    merged: dict
    prov: dict
    assets: dict
```

这四样 `main()` 里已全部算好，doctor 直接复用，不重复扫盘。检查函数签名统一为 `(probe) -> list[Finding]`。

### 3.3 注册表

```python
_CHECKS: list[Callable[[Probe], list[Finding]]] = []

def check(fn):            # 装饰器，导入即注册
    _CHECKS.append(fn)
    return fn

def run_checks(probe: Probe) -> list[Finding]:
    ...
```

`cx/doctor/__init__.py` 导入四个 `checks_*` 模块来触发注册。新增检查只需写函数 + 挂装饰器，不碰调度代码。

### 3.4 三条契约

1. **只读**：检查函数可以读文件系统（`refs` 类必须 stat 路径），但不得写入，也不得改动 Probe。
2. **崩溃隔离**：`run_checks` 捕获单个检查抛出的异常，转成一条 `internal.check-crashed` 的 warn finding，其余检查照跑。否则一个边界 case 会让 doctor 在最需要它的坏配置上直接崩掉。
3. **确定性排序**：输出按 `(严重度序, id, where)` 排序。CI diff 与测试断言都依赖这条。

### 3.5 严重度与退出码

| 退出码 | 含义 |
|---|---|
| 0 | 未触及阈值 |
| 1 | 触及阈值（存在 ≥ `--fail-on` 级别的 finding） |
| 2 | 用法 / IO 错误（目录不存在等，沿用 `cx.py:701` 现有语义） |

`--fail-on {error,warn,info,never}`，默认 `error`。

### 3.6 抑制机制

`--ignore ID`（可重复，也接受逗号分隔）。这不是投机性设计：`schema.likely-typo` 必然随 Claude Code 版本演进产生误报，没有抑制手段用户只能整个关掉 doctor。

被抑制的 finding 不进人读输出，但**仍在 `--json` 输出里**且标记 `ignored: true`，不静默丢弃。

## 4. 检查清单（19 项）

### 4.1 `refs` — 失效引用

| id | 级别 | 检查什么 |
|---|---|---|
| `refs.hook-command-missing` | error | hook 的 command 取第一个 token：是路径则 stat，是裸命令则 `shutil.which`，找不到即报 |
| `refs.mcp-command-missing` | error | stdio 型 MCP server 的 command 不在 PATH |
| `refs.mcp-url-invalid` | warn | http/sse 型 url 格式非法。**不发网络请求**，纯格式校验 |
| `refs.memory-import-missing` | error | CLAUDE.md 的 `@路径` 导入指向不存在的文件（`imports` 已在 `cx.py:375` 采集，需按 CLAUDE.md 所在目录解析相对路径并展开 `~`） |

**shell 片段的解析约定**：hook command 常是 shell 片段（如 `jq -r . | foo`）。用 `shlex.split` 取首 token；一旦发现管道、重定向、变量替换等 shell 元字符，**降级为 info** 并在 detail 里说明无法可靠判定，绝不硬猜。

### 4.2 `conflicts` — 静默覆盖与冲突

| id | 级别 | 检查什么 |
|---|---|---|
| `conflicts.managed-override` | warn | managed 策略静默覆盖了用户设置（`prov` 中末位是 managed 且存在前序贡献者） |
| `conflicts.shadowed-key` | info | 同一键在多个 scope 定义，只有一个生效 |
| `conflicts.deny-shadows-allow` | warn | **完全相同**的规则字符串同时出现在 allow 和 deny |
| `conflicts.legacy-local-settings` | warn | 新旧两个位置的 `settings.local.json` 同时存在（`cx.py:230` 已在兼容读取，但用户多半不知道两份都在生效） |
| `conflicts.mcp-name-collision` | warn | 同名 MCP server 在多个 scope 定义 |

**`deny-shadows-allow` 刻意做窄**：判断 `Bash(npm run test:*)` 与 `Bash(npm run *)` 谁遮蔽谁需要完整的规则匹配语义，做不准就是误报源。只报字符串完全相同的高置信度情况。

### 4.3 `schema` — 拼写与结构

| id | 级别 | 检查什么 |
|---|---|---|
| `schema.json-syntax` | error | 现有 `ctx.problems` 提升为 finding。整个文件被静默忽略，价值最高的一条 |
| `schema.misplaced-key` | error | 键放错层级，硬编码一张小映射表 |
| `schema.likely-typo` | warn | 见 4.3.1 |
| `schema.asset-missing-frontmatter` | warn | agents / commands / skills 的 `.md` 缺 `name` 或 `description` |
| `schema.hook-malformed` | error | hooks 结构不符：matcher 项不是 dict、hooks 数组缺 command |

`schema.misplaced-key` 的映射表初始内容：顶层 `defaultMode` 应在 `permissions.defaultMode`（`cx.py:594` 正在兼容读两处，说明这是真实错法）；顶层 `allow` / `deny` / `ask` 应在 `permissions` 下。

#### 4.3.1 未知键漂移的应对

「未知配置键」检查需要一份 Claude Code 已知键清单，而该清单会随 Claude Code 版本漂移；在零依赖、无网络的前提下无法自动同步。硬编码全量清单会在新版本发布后误报用户的合法配置。

**应对**：`schema.likely-typo` 只报**与已知键编辑距离为 1 且处于同一层级**的未知键。距离 ≥2 或完全陌生的键一律沉默——那更可能是新版本的新键，而非拼写错误。`env` 下的任意键名、`hooks` 下的事件名整体豁免。

这把误报面从「所有新键」压缩到「几乎必然是手滑」。

### 4.4 `security` — 安全与上下文预算

| id | 级别 | 检查什么 |
|---|---|---|
| `security.local-settings-tracked` | error | `settings.local.json` 被 git 跟踪（现有 `gitignore_status` 提升） |
| `security.local-settings-unignored` | warn | 未被 gitignore 覆盖 |
| `security.plaintext-secret` | warn | `env` 中键名命中 `SECRET_PAT`（`cx.py:51`）、值非空、且不是 `${VAR}` 引用形式 |
| `security.broad-allow` | warn | 过宽的 allow 规则。硬编码一小组高危模式（`Bash(*)` / `Bash` / `Read(//**)`），不做通用宽度分析 |
| `security.context-budget` | info | CLAUDE.md 常驻 token 合计超阈值（`tokens` 已在 `cx.py:382` 算好），默认 20000，`--budget N` 可覆盖 |

### 4.5 明确不做

**agent frontmatter 的 `tools` 值校验**：同样需要一份会漂移的清单，且 `tools` 值格式自由，误报风险高于收益。YAGNI，不做。

### 4.6 交付节奏

19 项**一次做完**。已知代价：启发式检查（`schema.likely-typo`、`conflicts.deny-shadows-allow`、`security.context-budget` 的阈值）只能先拍值，上线后可能需要回头校准。

## 5. CLI 与输出

### 5.1 命令表

`add_subparsers(dest="cmd", required=False)`，不带子命令时走默认报告：

```
cx                          # 完整报告，行为与今天完全一致
cx show [modules]           # 模块查询：cx show skills / cx show mcp,perms
cx doctor                   # 诊断
cx env set|unset|list       # 预留（子系统 B）
cx import / cx export       # 预留（子系统 D）
```

`env` / `import` / `export` 仅表示命名空间已为其留位，**本轮不注册这些子命令**——注册了就等于承诺了写入能力，而「纯只读」约束本轮不动。

共享 flag（`--path` / `--json` / `--show-secrets`）挂在一个 **parent parser** 上，由各子命令继承，避免 `cx --json` 与 `cx show skills --json` 两套写法打架。

### 5.2 向后兼容

现有 `cx --section perms` 保留为 `cx show perms` 的等价别名，帮助文本标注 deprecated，但不移除——已有脚本可能在用。

`cx show` 的模块参数支持位置参数与逗号分隔（`cx show mcp,perms,hooks`），非法模块名报错并返回退出码 2，错误信息列出全部合法名称。

### 5.3 doctor 专属 flag

| flag | 默认 | 作用 |
|---|---|---|
| `--fail-on {error,warn,info,never}` | `error` | 退出码阈值 |
| `--ignore ID` | 空 | 抑制指定 finding，可重复、可逗号分隔 |
| `--budget N` | `20000` | `security.context-budget` 的 token 阈值 |

### 5.4 人读输出

```
── doctor ──────────────────────────────────────

  ✗ error   refs.hook-command-missing
            PostToolUse hook 的命令找不到: /Users/x/bin/fmt
            来源  ~/.claude/settings.json
            修复  确认该路径存在且可执行，或移除这个 hook

  ⚠ warn    conflicts.managed-override
            managed 策略覆盖了你的 model 设置
            键    model
            修复  这是企业策略，本地无法覆盖；如需变更请联系管理员

  合计  1 error · 1 warn · 0 info      (2 项被 --ignore 抑制)
```

排序遵循 3.4 的确定性排序契约。

### 5.5 JSON 输出

```json
{
  "cx_version": "0.2.0",
  "cwd": "/path",
  "findings": [
    {
      "id": "refs.hook-command-missing",
      "severity": "error",
      "title": "...",
      "detail": "...",
      "where": "...",
      "fix": "...",
      "ignored": false
    }
  ],
  "summary": { "error": 1, "warn": 1, "info": 0, "ignored": 2 },
  "fail_on": "error",
  "exit_code": 1
}
```

### 5.6 默认报告的告警收口

默认报告底部现有的「需要注意」区块（`cx.py:671`）改为**调用同一套 doctor 引擎**，只渲染 error 级 finding，末尾附一行 `运行 cx doctor 查看全部 N 项`。

理由：否则告警逻辑会有两份实现，长期必然漂移。

**已知行为变更**：这会让默认报告比今天报出更多内容。这是改进，但属于可见的行为变更，需在 CHANGELOG 与 README 中说明。

## 6. 测试策略

### 6.1 第一层 · 拆包的验收

拆包提交的验收标准：`tests/test_cx.py` 现有 299 行**一行不改、全绿**。这是 `cx/__init__.py` 做全量再导出的唯一理由。

**这条不过，不写 doctor 的任何代码。**

### 6.2 第二层 · 每个检查的单测

每个检查是 `(Probe) -> list[Finding]` 的纯函数，测试直接手搓 Probe，不启动 CLI：

```python
def test_hook_command_missing_flags_absent_path(tmp_path):
    # Arrange
    probe = make_probe(merged={"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [{"command": str(tmp_path / "nope")}]}]}})
    # Act
    findings = check_hook_commands(probe)
    # Assert
    assert [f.id for f in findings] == ["refs.hook-command-missing"]
```

19 项每项至少两个用例（**报**与**不报**），加边界用例约 45–55 个。「不报」的用例同等重要——它防的是误报。

需新增 `make_probe(**overrides)` helper，默认给出全空的 ctx / merged / prov / assets。

- `refs` 类用 `tmp_path` 造真实的存在 / 不存在文件；`shutil.which` 用 `monkeypatch.setattr` 打桩，不依赖跑测机器的 PATH。
- 其余三类纯字典进出，不碰磁盘。

启发式的两项专门钉误报边界：

- `schema.likely-typo`：`"modle"`（距离 1）必报；`"modelXyz"`（距离 ≥2）必**不**报；`env` 下的任意怪键必**不**报。
- `conflicts.deny-shadows-allow`：字符串完全相同必报；`Bash(npm *)` vs `Bash(npm run *)` 必**不**报。

### 6.3 第三层 · 注册表与 CLI 的行为测试

| 测什么 | 断言 |
|---|---|
| 崩溃隔离 | 注册一个必抛异常的假检查，断言其余检查照常返回，且多出一条 `internal.check-crashed` 的 warn |
| 确定性排序 | 打乱注册顺序，两次运行输出的 finding 序列完全一致 |
| `--ignore` 语义 | 被抑制的 finding 不进人读输出，但仍在 JSON 里且 `ignored: true` |
| 退出码 | `main(["doctor"])` 在有 / 无 error 时返回 1 / 0；`--fail-on never` 恒返回 0；坏目录仍返回 2 |
| 默认报告收口 | 「需要注意」区块只渲染 error 级，且与 `cx doctor` 的 error 集合一致 |
| `cx show` 模块解析 | 单个名、逗号分隔、混合空白、重复去重、空输入 → 全量、非法名 → 退出码 2 |

### 6.4 覆盖率

下限 80%。doctor 部分因为是纯函数集合，预期可达 90%+；拆包搬运的部分沿用现有覆盖。

## 7. 实现顺序

1. **拆包**（独立提交）：搬运 `cx.py` → `cx/` 包，改 pyproject，现有测试全绿。
2. **注册表骨架**：`Finding` / `Probe` / `@check` / `run_checks` / 崩溃隔离 / 排序，配套第三层测试。
3. **CLI 骨架**：subparsers、parent parser、`cx show`、`cx doctor` 空跑、退出码语义。
4. **19 项检查**：按 `refs` → `schema` → `security` → `conflicts` 顺序，每项先写测试。
5. **输出渲染**：人读输出 + JSON 输出。
6. **默认报告收口**：「需要注意」区块改接 doctor 引擎。
7. **文档**：CLAUDE.md、README、CHANGELOG。

## 8. 未决事项

- `security.context-budget` 的默认阈值定为 20000 token，属首次拍值，上线后按真实使用校准。
- `schema.misplaced-key` 与 `security.broad-allow` 的硬编码模式表初始很小，随实际踩坑扩充。
- 子系统 A / B / D 各自另起 spec，本文档不涉及。

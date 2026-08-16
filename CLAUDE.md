# cx

一屏看清当前目录下 Claude Code 的生效配置，每一项都标注来源 scope。

## 项目约束（改代码前先读）

- **零依赖**：`dependencies = []`，只用标准库。开发期工具（pytest、pytest-cov）
  放 `[project.optional-dependencies]` 的 dev 组，不进运行时依赖。
- **doctor 只读**：`cx/doctor/` 下的检查函数可以读文件、调用 git，但不得写入
  任何文件，也不得修改传入的 `Probe`。写入能力将由独立的子系统提供。
- **Python 3.10+**：可以用 `X | Y` 联合类型语法，不要用更高版本才有的特性。
- **分发方式**：`uvx cx` / `pipx install cx`。不再支持 curl 单文件直接跑
  （包已拆分，见下方代码结构）。

## 常用命令

```bash
python3 -m pytest          # 跑测试（testpaths = ["tests"]）
python3 -m cx              # 在当前目录运行 cx
pip install -e ".[dev]"    # 本地安装，暴露 cx 命令，并装上 pytest / pytest-cov
```

未配置 lint / format / 类型检查工具。

## 代码结构

`cx/` 是一个包，不再是单文件：

| 文件 | 职责 |
|---|---|
| `cx/__init__.py` | 仅做再导出，保持 `import cx` 的公开 API |
| `cx/model.py` | 数据类与常量（`Ctx` / `SourceFile` / `SCOPES` / `SCOPE_RANK`） |
| `cx/util.py` | 无状态工具函数 |
| `cx/term.py` | 终端上色与排版 |
| `cx/discovery.py` | 按 scope 收集配置文件 |
| `cx/merge.py` | 带 provenance 的合并 |
| `cx/scan.py` | md 资产 / 记忆 / MCP / 插件扫描 |
| `cx/render.py` | 默认报告渲染 |
| `cx/cli.py` | 子命令分发与入口 |
| `cx/doctor/` | 诊断引擎，见下 |

`cx/doctor/` 的每项检查是一个纯函数 `(Probe) -> list[Finding]`，用
`@check` 装饰器注册。新增检查只需写函数并挂装饰器，不要碰 `registry.py`
的调度逻辑。三条契约：只读、崩溃隔离、确定性排序。

关键常量：`SCOPES` / `SCOPE_RANK` 定义优先级顺序
（user < project < local < managed，managed 最高）。

## 测试

`tests/test_cx.py` 的重点是**合并语义**——最容易写错也最难肉眼发现。改动 `merge_with_provenance()`、
`effective_scope()` 或 `SCOPE_RANK` 时必须先跑测试，并补上对应的优先级用例。

测试通过 `monkeypatch` + `tmp_path` 构造假的 HOME 和项目目录，不依赖真实环境。

`tests/doctor/` 覆盖诊断引擎。每项检查至少两个用例：一个「该报」，
一个「不该报」——后者防误报，同等重要。启发式检查
（`schema.likely-typo`、`conflicts.deny-shadows-allow`）必须钉住误报边界。

`tests/doctor/conftest.py` 的 `make_probe(**overrides)` 构造默认全空的
`Probe`，测试只覆盖自己关心的字段。

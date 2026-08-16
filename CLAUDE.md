# cx

一屏看清当前目录下 Claude Code 的生效配置，每一项都标注来源 scope。

## 项目约束（改代码前先读）

- **零依赖**：`dependencies = []`，只用标准库。不要引入第三方包——用户是 `curl` 单文件直接跑的。
- **纯只读**：cx 只读取和展示配置，不写入、不修改任何文件。任何写操作都是 bug。
- **单模块**：全部实现在 `cx.py`（约 750 行），`py-modules = ["cx"]`。不要拆包。
- **Python 3.10+**：可以用 `X | Y` 联合类型语法，不要用更高版本才有的特性。

## 常用命令

```bash
python3 -m pytest          # 跑测试（testpaths = ["tests"]）
python3 cx.py              # 在当前目录运行 cx
pip install -e .           # 本地安装，暴露 cx 命令
```

未配置 lint / format / 类型检查工具，`pytest` 也需要自行安装（`pip install pytest`）。

## 代码结构

`cx.py` 的执行流程：

1. `discover_sources()` — 按 scope 收集配置文件（managed / local / project / user）
2. `merge_with_provenance()` — 合并并记录每个键的来源
3. `effective_scope()` — 判定最终生效的 scope
4. `scan_*()` — 分别扫描 md assets、memory、MCP server、plugins
5. 渲染带 scope 标注的报告

关键常量：`SCOPES` / `SCOPE_RANK` 定义优先级顺序（user < project < local < managed，managed 最高）。

## 测试

`tests/test_cx.py` 的重点是**合并语义**——最容易写错也最难肉眼发现。改动 `merge_with_provenance()`、
`effective_scope()` 或 `SCOPE_RANK` 时必须先跑测试，并补上对应的优先级用例。

测试通过 `monkeypatch` + `tmp_path` 构造假的 HOME 和项目目录，不依赖真实环境。

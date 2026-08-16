# 更新日志

## 0.2.0

### 新增

- `cx doctor`：19 项只读配置诊断，分 `refs` / `conflicts` / `schema` /
  `security` 四类。支持 `--fail-on`、`--ignore`、`--budget`、`--json`。
- `cx show <模块>`：模块查询支持位置参数与逗号分隔，如 `cx show mcp,perms`。

### 变更

- **破坏性**：不再支持 `curl` 单文件直接运行。`cx.py` 已拆分为 `cx/` 包，
  请改用 `uvx cx` 或 `pipx install cx-claude`。
- **行为变更**：默认报告底部的「需要注意」区块改由 doctor 引擎驱动，
  会比 0.1.0 报出更多内容（此前只报 JSON 语法错误）。
- `--section` 标记为 deprecated，等价于 `cx show <模块>`，暂不移除。

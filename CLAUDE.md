# CLAUDE.md

## 项目概述

多语言 LLM 工具集（monorepo），每个工具一个目录，通过统一的 `worker.yaml` 声明元数据。Python 调度器根据 cron 表达式自动运行定期任务。

## 约定

- 每个工具目录必须包含 `worker.yaml` 和 `README.md`
- **功能变更必须同步更新 README.md**：任何工具的功能、配置项、CLI 参数、行为逻辑发生变更时，必须同步更新该工具的 `README.md`，确保文档与实际行为一致
- 工具目录命名用 kebab-case
- 调度器在 `scheduler/` 目录，提供 curses 仪表盘（类似 `top`），`make run` 启动
- `make test` 运行全部测试
- `_template/` 是新工具模板，不会被调度器扫描（下划线开头的目录跳过）
- worker.yaml 中 `schedule.enabled: false` 表示手动运行的工具，调度器忽略
- **禁止直接运行项目中的工具**（如 `python xxx/main.py`），工具由调度器统一调度执行

## 开发要求

- 每个工具应包含 `test_*.py`（推荐 `test_<工具名>.py` 避免多工具同名冲突），使用 pytest 编写单元测试
- 测试覆盖：纯函数 + API mock + 集成测试（临时文件目录）
- 测试中需 mock `load_dotenv`（`mock.patch("main.load_dotenv")`），禁止在测试中触碰真实 `.env` 文件
- 每个工具目录包含 `.env`（存放保密/隐私项：API 密钥 + 文件路径等个人信息）和 `.env.example`（模板），`.env` 已被 `.gitignore` 忽略
- 配置文件分离原则：`.env` 放隐私项（密钥、路径），`worker.yaml` 放非敏感项（阈值、开关、计数器）。参考 `agent-session-journal` 的实现

### 日志规范

所有 Python 工具统一使用以下日志配置，参考 `md-frontmatter` 和 `filesync` 的实现。

**`setup_logging` 函数：**

- 签名：`setup_logging(backup_count: int = 7) -> logging.Logger`
- 在 `main()` 内部调用，不在模块级别调用（避免 import 时产生副作用）
- `backup_count` 参数从 `worker.yaml` 的 `log_retention_days` 字段读取，默认 7
- Logger 名称使用工具名，如 `logging.getLogger("filesync")`

**三个 handler 结构：**

| Handler | 目标 | 级别 | 格式 |
| --- | --- | --- | --- |
| `StreamHandler(sys.stdout)` | 控制台 | INFO | `%(message)s`（纯文本，无时间戳） |
| `TimedRotatingFileHandler` | `logs/run.log` | INFO | `%(asctime)s [%(levelname)s] %(message)s` |
| `TimedRotatingFileHandler` | `logs/debug.log` | DEBUG | 同上 |

**`TimedRotatingFileHandler` 配置：**

- `when="midnight"`, `interval=1`, `backupCount=backup_count`, `encoding="utf-8"`
- 日志目录 `<tool_dir>/logs/`，已在 `.gitignore` 忽略
- `run.log`：INFO 级别，记录关键事件（同步/处理/错误/汇总）
- `debug.log`：DEBUG 级别，含 SKIP 等细节，用于排查

**模块级 logger：**

- 如果工具函数（如 `sync_group`、`load_config`）需要模块级引用 logger，初始化为 `NullHandler` 占位
- `main()` 中通过 `global logger` + `setup_logging()` 替换为完整配置
- 测试中按需 `mock.patch.object(module, "logger")` 验证日志输出

**`worker.yaml` 配置：**

- 必须包含 `log_retention_days` 字段，指定日志保留天数，默认 7
- 示例：

```yaml
name: my-tool
language: python
log_retention_days: 7
schedule:
  enabled: true
  cron: "*/5 * * * *"
run: "python main.py"
```

## 依赖管理

- 所有依赖统一在根目录管理，工具目录下不放依赖配置文件
- Python: 根目录 `pyproject.toml`，用 uv 管理，最低版本 3.13
- TypeScript: 根目录 `package.json`，用 pnpm 管理
- Rust: 根目录 `Cargo.toml`（有 Rust 工具时再加）

## LLM 调用

- 所有 LLM API 调用统一使用 **LiteLLM**（`import litellm`），通过 `litellm.completion()` 发起
- LLM 配置（`API_BASE`、`API_KEY`、`MODEL`）属于保密项，放在工具目录的 `.env` 中。其他非保密配置放在 `worker.yaml`
- 每次调用在 INFO 日志输出 model、prompt 长度、token 用量和 cost；DEBUG 日志输出完整计费明细
- 参考实现：`agent-session-journal/main.py` 的 `call_llm` 函数

## 语言约定

- Python: 入口文件 `main.py`
- Rust: 标准 Cargo 项目
- TypeScript: 入口 `src/index.ts`

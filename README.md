# my-llm-workers

LLM 相关工具集。每个工具独立一个目录，支持 Python、Rust、TypeScript 多语言。

## 目录结构

```text
.
├── pyproject.toml      # Python 依赖统一管理
├── package.json        # TypeScript 依赖统一管理
├── scheduler/          # 调度器，自动运行所有定期任务
├── _template/          # 新建工具的模板
├── worker-a/           # 具体工具（示例）
│   ├── worker.yaml     # 工具元数据与调度配置
│   ├── README.md       # 工具说明
│   └── ...             # 源码
└── ...
```

## worker.yaml 规范

每个工具目录下必须有 `worker.yaml`：

```yaml
name: worker-name
description: 一句话描述工具用途
language: python | rust | typescript
schedule:
  enabled: true          # false 表示手动运行
  cron: "0 9 * * *"     # cron 表达式（enabled=true 时必填）
run: "python main.py"   # 启动命令（相对于工具目录）
```

## 使用方式

### 启动调度器

```bash
uv run python scheduler/main.py
```

调度器会扫描所有同级目录的 `worker.yaml`，按 cron 表达式定期执行对应工具。

### 手动运行某个工具

进入对应工具目录，按其 README 说明执行即可。

### 新建工具

复制 `_template/` 目录，重命名后修改 `worker.yaml` 和代码。

# CLAUDE.md

## 项目概述

多语言 LLM 工具集（monorepo），每个工具一个目录，通过统一的 `worker.yaml` 声明元数据。Python 调度器根据 cron 表达式自动运行定期任务。

## 约定

- 每个工具目录必须包含 `worker.yaml` 和 `README.md`
- 工具目录命名用 kebab-case
- 调度器在 `scheduler/` 目录
- `_template/` 是新工具模板，不会被调度器扫描（下划线开头的目录跳过）
- worker.yaml 中 `schedule.enabled: false` 表示手动运行的工具，调度器忽略

## 依赖管理

- 所有依赖统一在根目录管理，工具目录下不放依赖配置文件
- Python: 根目录 `pyproject.toml`，用 uv 管理，最低版本 3.13
- TypeScript: 根目录 `package.json`，用 pnpm 管理
- Rust: 根目录 `Cargo.toml`（有 Rust 工具时再加）

## 语言约定

- Python: 入口文件 `main.py`
- Rust: 标准 Cargo 项目
- TypeScript: 入口 `src/index.ts`

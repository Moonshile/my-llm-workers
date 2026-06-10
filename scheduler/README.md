# scheduler

自动调度器，扫描所有工具目录的 `worker.yaml`，按 cron 表达式定期执行。

启动后显示 curses 仪表盘（类似 `top` 命令），实时展示 worker 状态和事件日志。

## 运行

```bash
make run                         # 从项目根目录启动
uv run python scheduler/main.py # 或直接运行
```

按 `q` 退出仪表盘。

## 仪表盘界面

```
┌──────────────────────────────────────────────────────────────┐
│  My LLM Workers Scheduler                2026-06-03 15:47:22  │
│  Workers: 2 | Uptime: 01:23:45                               │
├──────────────────────────────────────────────────────────────┤
│  Name              Cron        Next Run  Last Run  Status    │
│  ────────────────  ──────────  ────────  ────────  ─────     │
│  example-worker    0 9 * * *   09:00     09:00     ✓        │
│  another           */5 * * * * 15:50     15:45     ✗        │
├──────────────────────────────────────────────────────────────┤
│  Event Log                                                   │
│  15:45:01  example-worker        completed successfully      │
│  15:45:00  example-worker        → running                   │
│  15:30:00  -                     scheduler started with 2    │
├──────────────────────────────────────────────────────────────┤
│  q: quit  r: refresh                                         │
└──────────────────────────────────────────────────────────────┘
```

## 行为

- 跳过 `_` 或 `.` 开头的目录
- 跳过 `scheduler/` 自身
- 只调度 `schedule.enabled: true` 的工具
- 每个工具执行超时 1 小时
- 工具执行失败不影响其他工具
- 执行日志写入各工具 `logs/` 目录
- **调度器自身日志**写入 `scheduler/logs/` 目录：
  - `run.log`：INFO 级别，记录 worker 启停、成功/失败事件
  - `debug.log`：DEBUG 级别，含完整 stdout/stderr
  - 任务异常退出（非零退出码、超时、异常）时，完整错误栈和 stdout/stderr
    会写入调度器日志，无需再去各工具的日志目录排查
- 仪表盘事件日志保留最近 200 条

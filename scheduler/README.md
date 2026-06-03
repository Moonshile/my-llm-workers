# scheduler

自动调度器，扫描所有工具目录的 `worker.yaml`，按 cron 表达式定期执行。

## 运行

在项目根目录执行：

```bash
uv run python scheduler/main.py
```

## 行为

- 跳过 `_` 或 `.` 开头的目录
- 跳过 `scheduler/` 自身
- 只调度 `schedule.enabled: true` 的工具
- 每个工具执行超时 1 小时
- 工具执行失败不影响其他工具

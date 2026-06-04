"""
调度器：扫描 tools 目录的 worker.yaml，按 cron 表达式定期执行。

启动后显示 curses 仪表盘（类似 top），实时展示 worker 状态和事件日志。
按 q 退出。
"""

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path 中（支持直接 python scheduler/main.py 运行）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from scheduler.dashboard import Dashboard, SharedState, WorkerState, EventLog

ROOT_DIR = Path(__file__).resolve().parent.parent


def discover_workers(shared: SharedState):
    """扫描 root 目录下的 worker.yaml，返回已调度的 job id 列表。"""
    scheduled = []
    for entry in sorted(ROOT_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(("_", ".")):
            continue
        if entry.name == "scheduler":
            continue

        worker_yaml = entry / "worker.yaml"
        if not worker_yaml.exists():
            continue

        with open(worker_yaml) as f:
            config = yaml.safe_load(f)

        name = config.get("name", entry.name)
        schedule = config.get("schedule", {})
        enabled = schedule.get("enabled", False)
        cron_expr = schedule.get("cron", "-")
        command = config.get("run", "-")

        # 添加到共享状态（所有发现的 worker）
        shared.workers[name] = WorkerState(
            name=name,
            cron=cron_expr if enabled else "disabled",
            command=command,
        )

        if enabled:
            job_opts = schedule.get("job_options", {})
            scheduled.append((entry, name, cron_expr, command, job_opts))

    return scheduled


def make_worker_runner(worker_dir: Path, command: str, name: str, shared: SharedState):
    """返回一个可被 scheduler 调用的函数，执行后会更新 shared state。"""

    def run():
        ts = datetime.now().strftime("%H:%M:%S")
        shared.events.add(ts, name, "→ running")
        shared.update_worker(name, run_count=shared.workers[name].run_count + 1)

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=worker_dir,
                capture_output=True,
                text=True,
                timeout=3600,
            )
            ts_end = datetime.now().strftime("%H:%M:%S")
            if result.returncode == 0:
                shared.update_worker(
                    name,
                    last_run=ts_end,
                    last_status="✓",
                    success_count=shared.workers[name].success_count + 1,
                )
                shared.events.add(ts_end, name, "completed successfully")
            else:
                stderr_tail = result.stderr.strip().split("\n")[-1] if result.stderr.strip() else "no output"
                shared.update_worker(name, last_run=ts_end, last_status="✗", fail_count=shared.workers[name].fail_count + 1)
                shared.events.add(ts_end, name, f"failed (exit {result.returncode}): {stderr_tail[:60]}")
        except subprocess.TimeoutExpired:
            ts_end = datetime.now().strftime("%H:%M:%S")
            shared.update_worker(name, last_run=ts_end, last_status="⏱", fail_count=shared.workers[name].fail_count + 1)
            shared.events.add(ts_end, name, "timed out (1h)")
        except Exception as e:
            ts_end = datetime.now().strftime("%H:%M:%S")
            shared.update_worker(name, last_run=ts_end, last_status="✗", fail_count=shared.workers[name].fail_count + 1)
            shared.events.add(ts_end, name, f"error: {e}")

    return run


def main():
    shared = SharedState()

    # 发现 worker
    scheduled = discover_workers(shared)
    disabled_count = len(shared.workers) - len(scheduled)

    now = datetime.now().strftime("%H:%M:%S")
    shared.events.add(now, "-", f"scheduler starting: {len(scheduled)} enabled, {disabled_count} disabled")

    if not scheduled:
        shared.events.add(now, "-", "WARNING: no enabled workers found")
    else:
        # 启动后台调度器
        bg_scheduler = BackgroundScheduler(daemon=True)
        for worker_dir, name, cron_expr, command, job_opts in scheduled:
            trigger = CronTrigger.from_crontab(cron_expr)
            runner = make_worker_runner(worker_dir, command, name, shared)
            next_time = trigger.get_next_fire_time(None, datetime.now())
            next_str = next_time.strftime("%H:%M") if next_time else "-"

            misfire_grace_time = job_opts.get("misfire_grace_time", 60)
            coalesce = job_opts.get("coalesce", True)
            max_instances = job_opts.get("max_instances", 1)

            bg_scheduler.add_job(
                runner,
                trigger=trigger,
                id=name,
                name=name,
                misfire_grace_time=misfire_grace_time,
                coalesce=coalesce,
                max_instances=max_instances,
            )
            shared.update_worker(name, next_run=next_str)
            shared.events.add(now, name, f"scheduled [{cron_expr}] next={next_str}")

        bg_scheduler.start()
        shared.events.add(now, "-", f"scheduler started with {len(scheduled)} workers")

    # 启动仪表盘（阻塞，直到按 q）
    try:
        dashboard = Dashboard(shared)
        dashboard.run()
    except KeyboardInterrupt:
        pass
    finally:
        shared.running = False
        if scheduled:
            bg_scheduler.shutdown(wait=False)
        print("\nscheduler stopped.")


if __name__ == "__main__":
    main()

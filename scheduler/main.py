"""
调度器：扫描 tools 目录的 worker.yaml，按 cron 表达式定期执行。

启动后显示 curses 仪表盘（类似 top），实时展示 worker 状态和事件日志。
按 q 退出。
"""

import logging
import subprocess
import sys
import time
import traceback
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# 确保项目根目录在 sys.path 中（支持直接 python scheduler/main.py 运行）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from scheduler.dashboard import Dashboard, SharedState, WorkerState, EventLog

ROOT_DIR = Path(__file__).resolve().parent.parent
SCHEDULER_DIR = Path(__file__).resolve().parent

# 模块级 logger 占位，main() 中通过 setup_logging() 替换
logger = logging.getLogger("scheduler")
logger.addHandler(logging.NullHandler())


def setup_logging(backup_count: int = 7) -> logging.Logger:
    """配置调度器日志：控制台(INFO) + run.log(INFO) + debug.log(DEBUG)，均每日轮转。"""
    log_dir = SCHEDULER_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    global logger
    logger = logging.getLogger("scheduler")
    logger.setLevel(logging.DEBUG)
    # 清除已有的 handler（包括 NullHandler）
    logger.handlers.clear()

    # 控制台：INFO
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    # run.log：INFO（关键事件）
    fh_run = TimedRotatingFileHandler(
        filename=str(log_dir / "run.log"),
        when="midnight", interval=1, backupCount=backup_count, encoding="utf-8",
    )
    fh_run.setLevel(logging.INFO)
    fh_run.setFormatter(fmt)
    logger.addHandler(fh_run)

    # debug.log：DEBUG（完整细节）
    fh_debug = TimedRotatingFileHandler(
        filename=str(log_dir / "debug.log"),
        when="midnight", interval=1, backupCount=backup_count, encoding="utf-8",
    )
    fh_debug.setLevel(logging.DEBUG)
    fh_debug.setFormatter(fmt)
    logger.addHandler(fh_debug)

    return logger


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
            timeout = config.get("timeout", 3600)
            scheduled.append((entry, name, cron_expr, command, job_opts, timeout))

    return scheduled


def make_worker_runner(worker_dir: Path, command: str, name: str,
                       shared: SharedState, timeout: int = 3600):
    """返回一个可被 scheduler 调用的函数，执行后会更新 shared state。

    任务异常退出时（非零退出码、超时、异常），会将完整的 stdout/stderr
    及错误栈写入调度器自身的日志，便于排查。
    """

    def run():
        ts = datetime.now().strftime("%H:%M:%S")
        shared.events.add(ts, name, "→ running")
        shared.update_worker(name, run_count=shared.workers[name].run_count + 1)
        logger.debug("[%s] 开始执行: %s", name, command)

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=worker_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
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
                logger.info("[%s] 执行成功", name)
                logger.debug("[%s] stdout:\n%s", name, result.stdout)
            else:
                stderr_tail = result.stderr.strip().split("\n")[-1] if result.stderr.strip() else "no output"
                shared.update_worker(name, last_run=ts_end, last_status="✗", fail_count=shared.workers[name].fail_count + 1)
                shared.events.add(ts_end, name, f"failed (exit {result.returncode}): {stderr_tail[:60]}")
                logger.error(
                    "[%s] 异常退出 (exit=%d)\n--- STDOUT ---\n%s\n--- STDERR ---\n%s\n--- END ---",
                    name, result.returncode, result.stdout or "(empty)", result.stderr or "(empty)",
                )
        except subprocess.TimeoutExpired as e:
            ts_end = datetime.now().strftime("%H:%M:%S")
            shared.update_worker(name, last_run=ts_end, last_status="⏱", fail_count=shared.workers[name].fail_count + 1)
            shared.events.add(ts_end, name, f"timed out ({timeout}s)")
            # TimeoutExpired 可能携带部分已捕获的输出（bytes）
            timeout_stdout = e.stdout.decode("utf-8", errors="replace") if e.stdout else "(无输出)"
            timeout_stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else "(无输出)"
            logger.error(
                "[%s] 执行超时 (%ds)\n--- STDOUT (超时前) ---\n%s\n--- STDERR (超时前) ---\n%s\n--- END ---",
                name, timeout, timeout_stdout, timeout_stderr,
            )
        except Exception as e:
            ts_end = datetime.now().strftime("%H:%M:%S")
            shared.update_worker(name, last_run=ts_end, last_status="✗", fail_count=shared.workers[name].fail_count + 1)
            shared.events.add(ts_end, name, f"error: {e}")
            logger.error(
                "[%s] 调度器执行异常: %s\n%s",
                name, e, traceback.format_exc(),
            )

    return run


def main():
    # 初始化调度器自身日志（默认保留 7 天）
    setup_logging(backup_count=7)
    logger.info("调度器启动中...")

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
        for worker_dir, name, cron_expr, command, job_opts, worker_timeout in scheduled:
            trigger = CronTrigger.from_crontab(cron_expr)
            runner = make_worker_runner(worker_dir, command, name, shared, timeout=worker_timeout)
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
        logger.info("调度器已启动，%d 个 worker 已调度", len(scheduled))

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
        logger.info("调度器已停止")
        print("\nscheduler stopped.")


if __name__ == "__main__":
    main()

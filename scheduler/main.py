import os
import subprocess
import logging
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("scheduler")

ROOT_DIR = Path(__file__).resolve().parent.parent


def discover_workers():
    workers = []
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
        if config.get("schedule", {}).get("enabled"):
            workers.append((entry, config))
    return workers


def run_worker(worker_dir: Path, command: str, name: str):
    logger.info("Running worker: %s", name)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=worker_dir,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if result.returncode == 0:
            logger.info("Worker %s completed successfully", name)
        else:
            logger.error(
                "Worker %s failed (exit %d): %s",
                name,
                result.returncode,
                result.stderr[:500],
            )
    except subprocess.TimeoutExpired:
        logger.error("Worker %s timed out", name)
    except Exception as e:
        logger.error("Worker %s error: %s", name, e)


def main():
    workers = discover_workers()
    if not workers:
        logger.warning("No scheduled workers found")
        return

    scheduler = BlockingScheduler()

    for worker_dir, config in workers:
        name = config["name"]
        cron_expr = config["schedule"]["cron"]
        command = config["run"]
        trigger = CronTrigger.from_crontab(cron_expr)
        scheduler.add_job(
            run_worker,
            trigger=trigger,
            args=[worker_dir, command, name],
            id=name,
            name=name,
        )
        logger.info("Scheduled worker: %s [%s]", name, cron_expr)

    logger.info("Scheduler started with %d workers", len(workers))
    scheduler.start()


if __name__ == "__main__":
    main()

"""
统计指定目录下所有 git 仓库中指定作者的提交记录，按天/周汇总。

扫描目标目录下所有包含 .git 的子目录（最多两层），通过 git log --all 遍历所有
分支，按作者和日期范围过滤提交记录，按天聚合统计。支持增量运行：缓存每个仓库
最后处理的 commit SHA，下次运行时仅拉取新提交。

用法：
    uv run python _oneshot/git-stats/main.py                         # 全量/增量扫描
    uv run python _oneshot/git-stats/main.py --root ~/proj           # 指定目录
    uv run python _oneshot/git-stats/main.py --no-fetch              # 跳过 fetch
    uv run python _oneshot/git-stats/main.py --reset-cache           # 强制全量
    uv run python _oneshot/git-stats/main.py --json                  # JSON 输出
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import yaml
from collections import defaultdict
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# ---------- 路径 ----------

TOOL_DIR = Path(__file__).resolve().parent
WORKER_CONFIG = TOOL_DIR / "worker.yaml"

# ---------- 模块级 logger ----------

logger: logging.Logger = logging.getLogger("git-stats")
logger.addHandler(logging.NullHandler())


# ---------- 日志 ----------

def setup_logging(backup_count: int = 7) -> logging.Logger:
    """配置日志：控制台(INFO) + run.log(INFO) + debug.log(DEBUG)，均每日轮转。"""
    log_dir = TOOL_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    _logger = logging.getLogger("git-stats")
    _logger.setLevel(logging.DEBUG)

    # 控制台：INFO（简洁输出）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(ch)

    # run.log：INFO（关键事件：扫描进度 / 统计 / 错误）
    fh = TimedRotatingFileHandler(
        filename=log_dir / "run.log",
        when="midnight", interval=1, backupCount=backup_count, encoding="utf-8",
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    _logger.addHandler(fh)

    # debug.log：DEBUG（含 SKIP 等细节，用于排查）
    dh = TimedRotatingFileHandler(
        filename=log_dir / "debug.log",
        when="midnight", interval=1, backupCount=backup_count, encoding="utf-8",
    )
    dh.setLevel(logging.DEBUG)
    dh.setFormatter(fmt)
    _logger.addHandler(dh)

    return _logger


# ---------- 配置 ----------

CACHE_FILE = os.path.expanduser("~/.cache/git-stats-cache.json")
DATE_FORMAT = "%Y-%m-%d"


def load_log_retention() -> int:
    """从 worker.yaml 读取 log_retention_days 配置。"""
    try:
        if WORKER_CONFIG.exists():
            with open(WORKER_CONFIG) as f:
                cfg = yaml.safe_load(f) or {}
            return int(cfg.get("log_retention_days", 7))
    except Exception:
        pass
    return 7


# ---------- 仓库发现 ----------

def find_git_repos(root: str) -> list[Path]:
    """扫描 root 下所有包含 .git 的目录（最多两层），返回仓库根路径列表。"""
    repos = []
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        logger.error(f"目录不存在: {root_path}")
        sys.exit(1)

    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        git_dir = entry / ".git"
        if git_dir.exists():
            repos.append(entry)
        else:
            for sub in sorted(entry.iterdir()):
                if sub.is_dir() and not sub.name.startswith(".") and (sub / ".git").exists():
                    repos.append(sub)
    return repos


# ---------- 缓存 ----------

def load_cache() -> dict:
    """加载缓存文件，返回 {repo_path: last_commit_sha}。"""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"缓存文件损坏，将重新扫描: {e}")
        return {}


def save_cache(cache: dict) -> None:
    """保存缓存文件。"""
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


# ---------- Git 操作 ----------

def get_commits(
    repo_path: Path,
    author: str,
    since: str,
    until: str,
    after_sha: str | None = None,
) -> list[dict]:
    """
    获取仓库中指定作者的提交。

    返回:
        [{"sha": ..., "date": "YYYY-MM-DD", "time": "HH:MM:SS", "datetime": ..., "subject": ...}, ...]
    """
    cmd = [
        "git", "-C", str(repo_path),
        "log", "--all",
        f"--author={author}",
        f"--since={since}",
        f"--until={until}",
        "--format=%H|%ai|%s",
        "--date-order",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        logger.warning(f"仓库 {repo_path.name} 超时，跳过")
        return []
    except Exception as e:
        logger.warning(f"仓库 {repo_path.name} 错误: {e}")
        return []

    if result.returncode != 0:
        if result.stderr.strip():
            logger.debug(f"{repo_path.name}: {result.stderr.strip()}")
        return []

    commits = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, date_str, subject = parts[0], parts[1].strip(), parts[2].strip()

        try:
            dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        commits.append({
            "sha": sha,
            "date": dt.strftime(DATE_FORMAT),
            "time": dt.strftime("%H:%M:%S"),
            "datetime": dt,
            "subject": subject,
        })

    # 增量模式：截断到上次缓存的 SHA 之后
    if after_sha:
        filtered = []
        for c in commits:
            if c["sha"] == after_sha:
                break
            filtered.append(c)
        commits = filtered

    return commits


def get_latest_commit_sha(repo_path: Path, author: str) -> str | None:
    """获取仓库中该作者最新的一个 commit SHA（不限时间），用于初始化缓存。"""
    cmd = [
        "git", "-C", str(repo_path),
        "log", "--all",
        f"--author={author}",
        "--format=%H",
        "-1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def collect_all_commits(
    repos: list[Path],
    author: str,
    since: str,
    until: str,
    cache: dict,
    do_fetch: bool = True,
) -> tuple[list[dict], dict]:
    """
    遍历所有仓库，收集提交记录。
    如果 do_fetch=True，先 git fetch --all 拉取远端分支。

    始终拉取时间范围内的全部提交用于统计报告；缓存仅用于跳过已在缓存中的
    仓库的 git log 查询（如果该仓库最新 commit 未变则跳过），以及显示增量信息。

    返回 (时间范围内所有提交列表, 更新后的缓存)。
    """
    all_commits = []
    new_cache = {}
    total_repos = len(repos)
    total_new = 0
    total_all = 0

    for i, repo_path in enumerate(repos):
        name = repo_path.name
        cache_key = str(repo_path.resolve())
        after_sha = cache.get(cache_key)

        # Fetch 远端更新（失败不阻塞）
        if do_fetch:
            try:
                subprocess.run(
                    ["git", "-C", str(repo_path), "fetch", "--all", "--quiet"],
                    timeout=60,
                    capture_output=True,
                )
            except Exception:
                logger.debug(f"{name}: fetch 失败，使用本地数据")

        # 始终拉取时间范围内全部提交（不截断），保证报告统计完整
        commits = get_commits(repo_path, author, since, until)
        total_all += len(commits)

        # 计算新增数量（缓存 SHA 之前的提交即为新增）
        new_cnt = 0
        for c in commits:
            if c["sha"] == after_sha:
                break
            new_cnt += 1
        total_new += new_cnt

        if new_cnt:
            logger.info(
                f"[{i+1:3d}/{total_repos}] {name:<40} "
                f"{new_cnt} new / {len(commits)} total"
            )
        elif commits:
            logger.info(
                f"[{i+1:3d}/{total_repos}] {name:<40} "
                f"(no new, {len(commits)} cached)"
            )
        else:
            logger.info(f"[{i+1:3d}/{total_repos}] {name:<40} (no new)")

        all_commits.extend(commits)

        # 更新缓存
        if commits:
            new_cache[cache_key] = commits[0]["sha"]
        elif after_sha:
            new_cache[cache_key] = after_sha
        else:
            latest = get_latest_commit_sha(repo_path, author)
            if latest:
                new_cache[cache_key] = latest

    logger.info(f"总计: {total_new} new / {total_all} total commits")
    return all_commits, new_cache


# ---------- 统计 ----------

def compute_daily_stats(commits: list[dict]) -> dict[str, int]:
    """按日期聚合提交次数。"""
    stats: dict[str, int] = defaultdict(int)
    for c in commits:
        stats[c["date"]] += 1
    return dict(sorted(stats.items()))


def print_report(
    daily_stats: dict[str, int],
    total_commits: int,
    author: str,
    since: str,
    until: str,
) -> None:
    """打印统计报告到控制台和日志。"""
    report_lines = []
    report_lines.append("=" * 50)
    report_lines.append(f"  Git Commit 统计报告")
    report_lines.append(f"  作者: {author}")
    report_lines.append(f"  范围: {since} ~ {until} (exclusive)")
    report_lines.append(f"  总提交: {total_commits}")
    report_lines.append("=" * 50)

    if not daily_stats:
        report_lines.append("  该时间段内没有新的提交记录。")
        logger.info("\n".join(report_lines))
        return

    max_count = max(daily_stats.values())
    bar_max = 40

    report_lines.append("")
    report_lines.append(f"  {'日期':<12} {'次数':>5}  图表")
    report_lines.append(f"  {'-'*12} {'-'*5}  {'-'*bar_max}")

    for d, count in daily_stats.items():
        bar_len = int(count / max_count * bar_max) if max_count > 0 else 0
        bar = "█" * bar_len
        report_lines.append(f"  {d:<12} {count:>5}  {bar}")

    report_lines.append("")

    # 汇总统计
    dates = sorted(daily_stats.keys())
    active_days = len(daily_stats)
    total_days = (
        datetime.strptime(until, DATE_FORMAT).date()
        - datetime.strptime(since, DATE_FORMAT).date()
    ).days
    avg_per_active = total_commits / active_days if active_days > 0 else 0
    avg_per_day = total_commits / total_days if total_days > 0 else 0
    max_day = max(daily_stats, key=daily_stats.get)
    min_day = min(daily_stats, key=daily_stats.get)

    report_lines.append(f"  活跃天数: {active_days} / {total_days}")
    report_lines.append(f"  日平均 (按活跃日): {avg_per_active:.1f}")
    report_lines.append(f"  日平均 (按全部):   {avg_per_day:.1f}")
    report_lines.append(f"  最高单日: {max_day} → {daily_stats[max_day]} 次")
    report_lines.append(f"  最低单日: {min_day} → {daily_stats[min_day]} 次")
    report_lines.append("")

    # 周汇总
    week_stats = defaultdict(int)
    for d, count in daily_stats.items():
        dt = datetime.strptime(d, DATE_FORMAT)
        iso_week = dt.isocalendar()
        week_key = f"{iso_week[0]}-W{iso_week[1]:02d}"
        week_stats[week_key] += count

    max_w = max(week_stats.values()) if week_stats else 1
    report_lines.append(f"  周汇总:")
    report_lines.append(f"  {'周':<10} {'次数':>5}  图表")
    report_lines.append(f"  {'-'*10} {'-'*5}  {'-'*bar_max}")
    for wk in sorted(week_stats):
        count = week_stats[wk]
        bar_len = int(count / max_w * bar_max)
        report_lines.append(f"  {wk:<10} {count:>5}  {'█' * bar_len}")
    report_lines.append("")

    logger.info("\n".join(report_lines))


# ---------- 主入口 ----------

def main():
    global logger
    log_retention = load_log_retention()
    logger = setup_logging(backup_count=log_retention)

    parser = argparse.ArgumentParser(
        description="统计指定目录下所有 git 仓库中的提交频率"
    )
    parser.add_argument(
        "--root", default="~/code",
        help="要扫描的根目录 (默认: ~/code)",
    )
    parser.add_argument(
        "--since", default="2024-10-01",
        help="起始日期 (默认: 2024-10-01)",
    )
    parser.add_argument(
        "--until", default=datetime.now().strftime(DATE_FORMAT),
        help="结束日期，exclusive (默认: 当天)",
    )
    parser.add_argument("--author", default="kaiqiangduan", help="作者匹配字符串 (默认: kaiqiangduan)")
    parser.add_argument("--no-fetch", action="store_true", help="跳过 git fetch，仅使用本地数据")
    parser.add_argument("--reset-cache", action="store_true", help="清除缓存，强制全量重新扫描")
    parser.add_argument("--repos", nargs="+", help="指定要扫描的仓库名（而非全部）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    parser.add_argument("--list-repos", action="store_true", help="仅列出找到的仓库，不执行统计")

    args = parser.parse_args()
    root_dir = os.path.expanduser(args.root)
    author = args.author
    since = args.since
    until = args.until

    # 查找仓库
    all_repos = find_git_repos(root_dir)
    if args.repos:
        all_repos = [r for r in all_repos if r.name in args.repos]
        if not all_repos:
            logger.error(f"未找到指定仓库: {args.repos}")
            sys.exit(1)

    if args.list_repos:
        logger.info(f"找到 {len(all_repos)} 个仓库:")
        for r in all_repos:
            logger.info(f"  {r.name}")
        return

    logger.info(f"找到 {len(all_repos)} 个仓库")

    # 加载缓存
    cache = {} if args.reset_cache else load_cache()
    if args.reset_cache and os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
        logger.info("缓存已清除，将全量扫描")

    if args.no_fetch:
        logger.info("跳过 git fetch，仅使用本地数据")

    logger.info("正在扫描仓库...")

    all_commits, new_cache = collect_all_commits(
        all_repos, author, since, until, cache, do_fetch=not args.no_fetch
    )

    # 按时间排序（最新在前）
    all_commits.sort(key=lambda c: c["datetime"], reverse=True)

    # 按 author date 二次过滤（git --since/--until 用 committer date，
    # rebase/cherry-pick 会导致 author date 早于 committer date）
    since_dt = datetime.strptime(since, DATE_FORMAT)
    until_dt = datetime.strptime(until, DATE_FORMAT)
    filtered = [c for c in all_commits if since_dt <= c["datetime"] < until_dt]
    skipped = len(all_commits) - len(filtered)
    if skipped:
        logger.info(f"按 author date 过滤掉 {skipped} 条范围外提交")
    all_commits = filtered

    # 保存缓存
    save_cache(new_cache)
    logger.info(f"缓存已更新: {CACHE_FILE}")
    logger.info(f"共 {len(all_commits)} 条提交")

    # 输出
    daily_stats = compute_daily_stats(all_commits)

    if args.json:
        print(json.dumps({
            "author": author,
            "since": since,
            "until": until,
            "total_commits": len(all_commits),
            "daily_stats": daily_stats,
            "commits": [
                {"sha": c["sha"], "date": c["date"], "time": c["time"], "subject": c["subject"]}
                for c in all_commits
            ],
        }, indent=2, ensure_ascii=False))
    else:
        print_report(daily_stats, len(all_commits), author, since, until)

        if all_commits:
            logger.info("最近 10 条提交:")
            for c in all_commits[:10]:
                logger.info(f"  {c['date']} {c['time']}  {c['subject'][:70]}")


if __name__ == "__main__":
    main()

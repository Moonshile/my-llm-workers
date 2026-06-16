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
import webbrowser
import yaml
from collections import defaultdict
from datetime import date, datetime, timedelta
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

        # 标记每条提交所属仓库
        for c in commits:
            c["repo"] = name

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

    # 汇总统计（分母用首次到最后一次提交的天数，而非 --since/--until）
    dates = sorted(daily_stats.keys())
    active_days = len(daily_stats)
    if dates:
        first_dt = datetime.strptime(dates[0], DATE_FORMAT).date()
        last_dt = datetime.strptime(dates[-1], DATE_FORMAT).date()
        total_days = (last_dt - first_dt).days + 1
    else:
        total_days = 0
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


# ---------- 工作时间分析 ----------

try:
    from chinese_calendar import is_workday as _cn_is_workday
except ImportError:
    _cn_is_workday = None


def is_workday(d: date) -> bool:
    """判断是否为工作日（使用 chinesecalendar 库，含法定节假日和调休）。"""
    if _cn_is_workday is not None:
        return _cn_is_workday(d)
    # fallback: 周一至周五为工作日
    return d.weekday() < 5


def is_working_hour(dt: datetime) -> bool:
    """判断是否在工作时间（工作日 9:00-20:00）。"""
    if not is_workday(dt.date()):
        return False
    t = dt.time()
    return t.hour >= 9 and t.hour < 20


def classify_commits(commits: list[dict]) -> tuple[list[dict], list[dict]]:
    """将提交分为工作时间和非工作时间两组。"""
    working = []
    non_working = []
    for c in commits:
        if is_working_hour(c["datetime"]):
            working.append(c)
        else:
            non_working.append(c)
    return working, non_working


def compute_hourly_stats(commits: list[dict]) -> dict[int, int]:
    """按小时（0-23）聚合提交次数。"""
    stats: dict[int, int] = defaultdict(int)
    for c in commits:
        stats[c["datetime"].hour] += 1
    return dict(sorted(stats.items()))


# ---------- 仓库 & 连续天数统计 ----------

def compute_repo_stats(commits: list[dict]) -> dict[str, int]:
    """按仓库聚合提交次数，按数量降序排列。"""
    stats: dict[str, int] = defaultdict(int)
    for c in commits:
        stats[c.get("repo", "unknown")] += 1
    return dict(sorted(stats.items(), key=lambda x: x[1], reverse=True))


def compute_streaks(daily_stats: dict[str, int]) -> dict:
    """计算最长连续提交天数和当前连续天数。"""
    if not daily_stats:
        return {"longest": 0, "current": 0, "longest_range": ""}

    dates = sorted(datetime.strptime(d, DATE_FORMAT).date() for d in daily_stats)
    date_set = set(dates)

    longest = 0
    current = 0
    longest_start = dates[0]
    longest_end = dates[0]

    cur_start = dates[0]
    streak = 0
    d = dates[0]
    while d <= dates[-1]:
        if d in date_set:
            streak += 1
            if streak > longest:
                longest = streak
                longest_start = cur_start
                longest_end = d
        else:
            streak = 0
            cur_start = d + timedelta(days=1)
        d += timedelta(days=1)

    # 当前连续（从数据中最后一天往前数）
    data_end = dates[-1]
    current = 0
    d = data_end
    while d >= dates[0]:
        if d in date_set:
            current += 1
        else:
            break
        d -= timedelta(days=1)

    return {
        "longest": longest,
        "longest_range": f"{longest_start} ~ {longest_end}" if longest > 0 else "",
        "current": current,
    }


# ---------- HTML 报告 ----------

HTML_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
     background:#0d1117;color:#c9d1d9;padding:24px 32px;max-width:1200px;margin:0 auto}
h1{font-size:28px;margin-bottom:4px}
h1 span.author{color:#58a6ff}
h2{font-size:18px;margin:32px 0 12px;padding-bottom:6px;border-bottom:1px solid #21262d}
.subtitle{color:#8b949e;font-size:14px;margin-bottom:24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:32px}
.card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:16px}
.card .label{font-size:12px;color:#8b949e;margin-bottom:4px}
.card .value{font-size:24px;font-weight:600}
.card .value.green{color:#3fb950}
.card .value.blue{color:#58a6ff}
.card .value.orange{color:#d2991d}
.card .value.purple{color:#a371f7}
.card .sub{font-size:12px;color:#8b949e;margin-top:2px}
/* Heatmap */
.heatmap-wrapper{overflow-x:auto;margin-bottom:32px}
.heatmap{display:flex;gap:3px}
.heatmap-months{margin-bottom:4px}
.heatmap-months span{font-size:10px;color:#8b949e;overflow:visible;white-space:nowrap}
.heatmap-body{display:flex;gap:3px}
.heatmap-week{display:flex;flex-direction:column;gap:3px}
.heatmap-day-labels{display:flex;flex-direction:column;gap:3px;margin-right:6px}
.heatmap-day-labels span{font-size:10px;color:#8b949e;height:13px;line-height:13px}
.heatmap-cell{width:13px;height:13px;border-radius:2px;cursor:pointer;position:relative}
.heatmap-cell:hover{outline:1px solid #8b949e;z-index:1}
.cell-0{background:#161b22}
.cell-1{background:#0e4429}
.cell-2{background:#006d32}
.cell-3{background:#26a641}
.cell-4{background:#39d353}
.heatmap-legend{display:flex;align-items:center;gap:4px;margin-top:8px;font-size:11px;color:#8b949e}
.heatmap-legend .heatmap-cell{cursor:default}
/* Tooltip */
.tooltip{position:fixed;background:#1c2128;border:1px solid #30363d;border-radius:6px;
         padding:8px 12px;font-size:12px;pointer-events:none;z-index:100;display:none;
         white-space:nowrap;box-shadow:0 8px 24px rgba(0,0,0,.5)}
.tooltip strong{color:#e6edf3}
/* Bar chart */
.bar-row{display:flex;align-items:center;gap:8px;margin-bottom:4px}
.bar-row .bar-label{font-size:12px;color:#8b949e;width:80px;text-align:right;flex-shrink:0}
.bar-row .bar-count{font-size:12px;color:#e6edf3;width:50px;text-align:right;flex-shrink:0}
.bar-row .bar-track{flex:1;height:16px;background:#161b22;border-radius:3px;overflow:hidden}
.bar-row .bar-fill{height:100%;background:linear-gradient(90deg,#0e4429,#39d353);border-radius:3px;
                    transition:width .3s}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:500;font-size:12px;position:sticky;top:0;background:#0d1117}
tr:hover td{background:#161b22}
.mono{font-family:ui-monospace,SFMono-Regular,monospace;font-size:11px;color:#8b949e}
a,a:visited{color:#58a6ff;text-decoration:none}
a:hover{text-decoration:underline}
.scroll-table{max-height:500px;overflow-y:auto;border:1px solid #21262d;border-radius:6px}
.repo-bar .bar-label{{width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:left}}
.repo-badge{display:inline-block;background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb44;
            border-radius:12px;padding:2px 8px;font-size:11px;margin-right:4px}
/* Tabs */
.tabs{display:flex;gap:0;border-bottom:1px solid #21262d;margin-bottom:24px}
.tab-btn{padding:10px 20px;cursor:pointer;color:#8b949e;border-bottom:2px solid transparent;
         font-size:14px;font-weight:500;transition:color .2s,border-color .2s}
.tab-btn:hover{color:#e6edf3}
.tab-btn.active{color:#f78166;border-bottom-color:#f78166}
.tab-panel{display:none}
.tab-panel.active{display:block}
.tag{display:inline-block;background:#1f6feb22;color:#58a6ff;border-radius:10px;
     padding:2px 8px;font-size:11px;margin-left:6px;vertical-align:middle}
"""


def _level(count: int) -> int:
    if count <= 0:
        return 0
    if count <= 2:
        return 1
    if count <= 5:
        return 2
    if count <= 10:
        return 3
    return 4


def generate_html_report(
    commits: list[dict],
    daily_stats: dict[str, int],
    repo_stats: dict[str, int],
    author: str,
    since: str,
    until: str,
    output_path: str,
    non_working: list[dict] | None = None,
    working: list[dict] | None = None,
) -> str:
    """生成 GitHub 风格的 HTML 统计报告（含工作时间分析 Tab），返回输出路径。"""

    # 准备数据（分母用首次到最后一次提交的天数）
    total_commits = len(commits)
    dates_list = sorted(daily_stats.keys())
    active_days = len(daily_stats)
    if dates_list:
        data_start = dates_list[0]
        data_end = dates_list[-1]
        first_dt = datetime.strptime(data_start, DATE_FORMAT).date()
        last_dt = datetime.strptime(data_end, DATE_FORMAT).date()
        total_days = (last_dt - first_dt).days + 1
    else:
        data_start = since
        data_end = until
        total_days = 0
    avg_per_active = total_commits / active_days if active_days > 0 else 0
    avg_per_day = total_commits / total_days if total_days > 0 else 0
    max_day = max(daily_stats, key=daily_stats.get) if daily_stats else "-"
    max_day_count = daily_stats.get(max_day, 0) if daily_stats else 0
    streaks = compute_streaks(daily_stats)

    # 周汇总
    week_stats = defaultdict(int)
    for d, count in daily_stats.items():
        dt = datetime.strptime(d, DATE_FORMAT)
        iso = dt.isocalendar()
        week_stats[f"{iso[0]}-W{iso[1]:02d}"] += count
    week_stats = dict(sorted(week_stats.items()))
    week_max = max(week_stats.values()) if week_stats else 1

    # 月份汇总
    month_stats = defaultdict(int)
    for d, count in daily_stats.items():
        month_stats[d[:7]] += count
    month_stats = dict(sorted(month_stats.items()))

    # 数据嵌入 JSON（热力图从首次提交开始，而非 --since）
    data_json = json.dumps({
        "daily_stats": daily_stats,
        "since": data_start,
        "until": data_end,
    }, ensure_ascii=False)

    # 周柱状图 HTML
    week_bars = ""
    for wk, cnt in week_stats.items():
        pct = int(cnt / week_max * 100) if week_max > 0 else 0
        week_bars += (
            f'<div class="bar-row">'
            f'<span class="bar-label">{wk}</span>'
            f'<span class="bar-count">{cnt}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>'
            f'</div>\n'
        )

    # 月份柱状图
    month_max = max(month_stats.values()) if month_stats else 1
    month_bars = ""
    for m, cnt in month_stats.items():
        pct = int(cnt / month_max * 100) if month_max > 0 else 0
        month_bars += (
            f'<div class="bar-row">'
            f'<span class="bar-label">{m}</span>'
            f'<span class="bar-count">{cnt}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>'
            f'</div>\n'
        )

    # 仓库排行柱状图
    repo_max = max(repo_stats.values()) if repo_stats else 1
    repo_bars = ""
    for repo, cnt in repo_stats.items():
        pct = int(cnt / repo_max * 100) if repo_max > 0 else 0
        repo_bars += (
            f'<div class="bar-row repo-bar">'
            f'<span class="bar-label" title="{repo}">{repo}</span>'
            f'<span class="bar-count">{cnt}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>'
            f'</div>\n'
        )

    # 最近提交表
    commit_rows = ""
    for c in commits[:100]:
        sha_short = c["sha"][:7]
        subject = c["subject"][:100]
        repo = c.get("repo", "-")
        commit_rows += (
            f'<tr>'
            f'<td><span class="mono">{sha_short}</span></td>'
            f'<td>{c["date"]} {c["time"]}</td>'
            f'<td><span class="repo-badge">{repo}</span></td>'
            f'<td>{subject}</td>'
            f'</tr>\n'
        )

    # ---------- 非工作时间分析 ----------
    nw_data_json = "null"
    nw_cards = ""
    nw_heatmap = ""
    nw_hourly_html = ""
    nw_repo_bars = ""
    nw_tab_btn = ""

    if non_working is not None and working is not None:
        nw_daily = compute_daily_stats(non_working)
        nw_total = len(non_working)
        w_total = len(working)
        nw_pct = nw_total / total_commits * 100 if total_commits > 0 else 0
        nw_active = len(nw_daily)
        nw_max_day = max(nw_daily, key=nw_daily.get) if nw_daily else "-"
        nw_max_count = nw_daily.get(nw_max_day, 0) if nw_daily else 0
        nw_hourly = compute_hourly_stats(non_working)
        nw_hourly_max = max(nw_hourly.values()) if nw_hourly else 1
        nw_peak_hour = max(nw_hourly, key=nw_hourly.get) if nw_hourly else 0
        nw_repo = compute_repo_stats(non_working)

        # 非工作时间热力图数据（范围跟主热力图一致）
        nw_data_json = json.dumps({
            "daily_stats": nw_daily,
            "since": data_start,
            "until": data_end,
        }, ensure_ascii=False)

        # 统计卡片
        nw_cards = f"""
<div class="cards">
  <div class="card">
    <div class="label">非工作时间提交</div>
    <div class="value orange">{nw_total}</div>
    <div class="sub">占总提交的 {nw_pct:.1f}%</div>
  </div>
  <div class="card">
    <div class="label">工作时间提交</div>
    <div class="value green">{w_total}</div>
    <div class="sub">占总提交的 {100 - nw_pct:.1f}%</div>
  </div>
  <div class="card">
    <div class="label">非工作活跃天数</div>
    <div class="value blue">{nw_active}</div>
    <div class="sub">日均 {nw_total / nw_active:.1f} 次" if nw_active > 0 else "" + "</div>
  </div>
  <div class="card">
    <div class="label">非工作最高单日</div>
    <div class="value purple">{nw_max_count}</div>
    <div class="sub">{nw_max_day}</div>
  </div>
  <div class="card">
    <div class="label">非工作高峰时段</div>
    <div class="value">{nw_peak_hour}:00-{nw_peak_hour + 1}:00</div>
    <div class="sub">{nw_hourly.get(nw_peak_hour, 0)} 次提交</div>
  </div>
</div>"""

        # 非工作时间热力图
        nw_heatmap = f"""
<h2>📊 非工作时间提交热力图</h2>
<div class="heatmap-wrapper">
  <div style="display:flex">
    <div class="heatmap-day-labels">
      <span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span><span></span>
    </div>
    <div>
      <div class="heatmap-months" id="heatmap-months-nw"></div>
      <div class="heatmap-body" id="heatmap-body-nw"></div>
    </div>
  </div>
  <div class="heatmap-legend">
    Less <span class="heatmap-cell cell-0"></span>
    <span class="heatmap-cell cell-1"></span>
    <span class="heatmap-cell cell-2"></span>
    <span class="heatmap-cell cell-3"></span>
    <span class="heatmap-cell cell-4"></span> More
  </div>
</div>"""

        # 按小时分布
        nw_hourly_html = '<h2>🕐 非工作时间按小时分布</h2>\n'
        for h, cnt in nw_hourly.items():
            pct = int(cnt / nw_hourly_max * 100) if nw_hourly_max > 0 else 0
            label = "🌙" if h < 6 else ("🌅" if h < 9 else ("🌙" if h >= 20 else "☀️"))
            nw_hourly_html += (
                f'<div class="bar-row">'
                f'<span class="bar-label">{label} {h:02d}:00</span>'
                f'<span class="bar-count">{cnt}</span>'
                f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>'
                f'</div>\n'
            )

        # 仓库排行柱状图
        nw_repo_max = max(nw_repo.values()) if nw_repo else 1
        nw_repo_bars = ""
        for repo, cnt in nw_repo.items():
            pct = int(cnt / nw_repo_max * 100) if nw_repo_max > 0 else 0
            nw_repo_bars += (
                f'<div class="bar-row repo-bar">'
                f'<span class="bar-label" title="{repo}">{repo}</span>'
                f'<span class="bar-count">{cnt}</span>'
                f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>'
                f'</div>\n'
            )

        nw_tab_btn = f'<button class="tab-btn" onclick="switchTab(\'tab-nonworking\', this)">⏰ 非工作时间<span class="tag">{nw_pct:.0f}%</span></button>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Git Commit Stats — {author}</title>
<style>{HTML_CSS}</style>
</head>
<body>

<!-- Tab Buttons -->
<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('tab-all', this)">📊 全部提交</button>
  {nw_tab_btn}
</div>

<!-- ====== TAB: 全部提交 ====== -->
<div class="tab-panel active" id="tab-all">

<h1><span class="author">{author}</span> 的提交统计</h1>
<p class="subtitle">{data_start} ~ {data_end} &nbsp;·&nbsp; {len(set(c.get('repo','') for c in commits))} 个仓库 &nbsp;·&nbsp; 共 {total_commits} 条提交 &nbsp;·&nbsp; {total_days} 天</p>

<!-- Summary Cards -->
<div class="cards">
  <div class="card">
    <div class="label">总提交</div>
    <div class="value green">{total_commits}</div>
    <div class="sub">{total_days} 天内的提交</div>
  </div>
  <div class="card">
    <div class="label">活跃天数</div>
    <div class="value blue">{active_days}</div>
    <div class="sub">占全部天数的 {active_days / total_days * 100:.0f}%</div>
  </div>
  <div class="card">
    <div class="label">日均 (活跃日)</div>
    <div class="value">{avg_per_active:.1f}</div>
    <div class="sub">全部天数日均 {avg_per_day:.1f}</div>
  </div>
  <div class="card">
    <div class="label">最高单日</div>
    <div class="value orange">{max_day_count}</div>
    <div class="sub">{max_day}</div>
  </div>
  <div class="card">
    <div class="label">最长连续</div>
    <div class="value purple">{streaks["longest"]}</div>
    <div class="sub">{streaks["longest_range"]}</div>
  </div>
  <div class="card">
    <div class="label">当前连续</div>
    <div class="value">{streaks["current"]}</div>
    <div class="sub">截至数据最新日</div>
  </div>
</div>

<!-- Heatmap -->
<h2>📊 提交热力图</h2>
<div class="heatmap-wrapper">
  <div style="display:flex">
    <div class="heatmap-day-labels">
      <span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span><span></span>
    </div>
    <div>
      <div class="heatmap-months" id="heatmap-months"></div>
      <div class="heatmap-body" id="heatmap-body"></div>
    </div>
  </div>
  <div class="heatmap-legend">
    Less <span class="heatmap-cell cell-0"></span>
    <span class="heatmap-cell cell-1"></span>
    <span class="heatmap-cell cell-2"></span>
    <span class="heatmap-cell cell-3"></span>
    <span class="heatmap-cell cell-4"></span> More
  </div>
</div>

<!-- Repo Breakdown -->
<h2>📦 仓库排行</h2>
{repo_bars}

<!-- Monthly Bar Chart -->
<h2>📆 月提交分布</h2>
{month_bars}

<!-- Weekly Bar Chart -->
<h2>📅 周提交分布</h2>
{week_bars}

<!-- Recent Commits -->
<h2>📝 最近提交</h2>
<div class="scroll-table">
<table>
<thead><tr><th>SHA</th><th>时间</th><th>仓库</th><th>提交信息</th></tr></thead>
<tbody>{commit_rows}</tbody>
</table>
</div>

</div><!-- /tab-all -->

<!-- ====== TAB: 非工作时间 ====== -->
<div class="tab-panel" id="tab-nonworking">

<h1>⏰ <span class="author">{author}</span> 的非工作时间提交</h1>
<p class="subtitle">工作日 9:00-20:00 以外（含周末、法定节假日，已考虑调休）&nbsp;·&nbsp; {data_start} ~ {data_end}</p>

{nw_cards}

{nw_heatmap}

{nw_hourly_html}

<h2>📦 非工作时间仓库排行</h2>
{nw_repo_bars}

</div><!-- /tab-nonworking -->

<div class="tooltip" id="tooltip"></div>

<script>
// Tab switching
function switchTab(id, btn) {{
  document.querySelectorAll('.tab-panel').forEach(function(p) {{ p.classList.remove('active'); }});
  document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
  // 切换后滚动热力图到最右侧
  var panel = document.getElementById(id);
  panel.querySelectorAll('.heatmap-wrapper').forEach(function(w) {{ w.scrollLeft = w.scrollWidth; }});
}}

function parseYMD(str) {{
  var p = str.split('-');
  return new Date(+p[0], +p[1] - 1, +p[2]);
}}

function fmtDate(d) {{
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}}

// Heatmap rendering helper
function renderHeatmap(monthsDivId, bodyDivId, data) {{
  var daily = data.daily_stats;
  var since = parseYMD(data.since);
  var until = parseYMD(data.until);

  // Start from the Monday on or before the first commit
  var start = new Date(since);
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7));

  // End on the Sunday on or after the last commit
  var end = new Date(until);
  end.setDate(end.getDate() + (7 - end.getDay()) % 7);

  var monthsDiv = document.getElementById(monthsDivId);
  var bodyDiv = document.getElementById(bodyDivId);

  var weeks = [];
  var cursor = new Date(start);
  var lastMonth = -1;
  var monthPositions = [];

  var weekIdx = 0;
  while (cursor <= end) {{
    var week = [];
    for (var d = 0; d < 7; d++) {{
      var ds = fmtDate(cursor);
      var count = daily[ds] || 0;
      var month = cursor.getMonth();
      if (month !== lastMonth && d <= 3) {{
        monthPositions.push({{label: cursor.toLocaleString('zh-CN', {{month:'short'}}), idx: weekIdx}});
        lastMonth = month;
      }}
      week.push({{date: ds, count: count}});
      cursor.setDate(cursor.getDate() + 1);
    }}
    weeks.push(week);
    weekIdx++;
  }}

  var COL_WIDTH = 16;
  monthsDiv.style.position = 'relative';
  monthsDiv.style.height = '18px';
  monthsDiv.style.width = (weeks.length * COL_WIDTH) + 'px';
  var monthHTML = '';
  for (var i = 0; i < monthPositions.length; i++) {{
    var mp = monthPositions[i];
    monthHTML += '<span style="position:absolute;left:' + (mp.idx * COL_WIDTH) + 'px;font-size:10px;color:#8b949e">' + mp.label + '</span>';
  }}
  monthsDiv.innerHTML = monthHTML;

  var bodyHTML = '';
  for (var w = 0; w < weeks.length; w++) {{
    bodyHTML += '<div class="heatmap-week">';
    for (var d = 0; d < 7; d++) {{
      var cell = weeks[w][d];
      var level = cell.count <= 0 ? 0 : cell.count <= 2 ? 1 : cell.count <= 5 ? 2 : cell.count <= 10 ? 3 : 4;
      bodyHTML += '<div class="heatmap-cell cell-' + level + '" data-date="' + cell.date + '" data-count="' + cell.count + '"';
      bodyHTML += ' onmouseenter="showTooltip(event,\\'' + cell.date + '\\',' + cell.count + ')"';
      bodyHTML += ' onmouseleave="hideTooltip()"></div>';
    }}
    bodyHTML += '</div>';
  }}
  bodyDiv.innerHTML = bodyHTML;
}}

// Render heatmap for tab-all
renderHeatmap('heatmap-months', 'heatmap-body', {data_json});

// Render heatmap for tab-nonworking (if data exists)
var nwData = {nw_data_json};
if (nwData) {{
  renderHeatmap('heatmap-months-nw', 'heatmap-body-nw', nwData);
}}

// Auto-scroll heatmaps to latest (rightmost)
document.querySelectorAll('.heatmap-wrapper').forEach(function(w) {{
  w.scrollLeft = w.scrollWidth;
}});

function showTooltip(e, date, count) {{
  var t = document.getElementById('tooltip');
  t.innerHTML = '<strong>' + count + ' commits</strong> on ' + date;
  t.style.display = 'block';
  t.style.left = (e.clientX + 12) + 'px';
  t.style.top = (e.clientY - 36) + 'px';
}}
function hideTooltip() {{
  document.getElementById('tooltip').style.display = 'none';
}}
</script>

</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path


# ---------- 主入口 ----------

def main():
    global logger
    log_retention = load_log_retention()
    logger = setup_logging(backup_count=log_retention)

    # 每年年底提示升级 chinesecalendar 以获取下一年节假日数据
    if _cn_is_workday is not None and date.today().month == 12:
        logger.warning(
            "⚠️  已到年底，国务院可能已公布下一年节假日安排。"
            "请运行 uv sync --upgrade-package chinesecalendar 更新节假日数据。"
        )

    parser = argparse.ArgumentParser(
        description="统计指定目录下所有 git 仓库中的提交频率"
    )
    parser.add_argument(
        "--root", default="~/code",
        help="要扫描的根目录 (默认: ~/code)",
    )
    parser.add_argument(
        "--since", default="2024-11-01",
        help="起始日期 (默认: 2024-11-01)",
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
    parser.add_argument(
        "--output", default=str(TOOL_DIR / "report.html"),
        help=f"HTML 报告输出路径 (默认: {TOOL_DIR / 'report.html'})",
    )
    parser.add_argument("--no-open", action="store_true", help="不自动打开 HTML 报告")

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

    # 统计
    daily_stats = compute_daily_stats(all_commits)
    repo_stats = compute_repo_stats(all_commits)

    if args.json:
        print(json.dumps({
            "author": author,
            "since": since,
            "until": until,
            "total_commits": len(all_commits),
            "daily_stats": daily_stats,
            "repo_stats": repo_stats,
            "streaks": compute_streaks(daily_stats),
            "commits": [
                {"sha": c["sha"], "date": c["date"], "time": c["time"],
                 "subject": c["subject"], "repo": c.get("repo", "")}
                for c in all_commits
            ],
        }, indent=2, ensure_ascii=False))
    else:
        print_report(daily_stats, len(all_commits), author, since, until)

        if all_commits:
            logger.info("最近 10 条提交:")
            for c in all_commits[:10]:
                logger.info(f"  {c['date']} {c['time']}  {c['subject'][:70]}")

    # 工作时间分类
    working, non_working = classify_commits(all_commits)
    logger.info(
        f"工作时间: {len(working)} 条, "
        f"非工作时间: {len(non_working)} 条 "
        f"({len(non_working) / len(all_commits) * 100:.1f}%)"
        if all_commits else "无提交"
    )

    # 生成 HTML 报告
    output_path = os.path.expanduser(args.output)
    generate_html_report(
        all_commits, daily_stats, repo_stats,
        author, since, until, output_path,
        non_working=non_working, working=working,
    )
    logger.info(f"HTML 报告已生成: {output_path}")

    if not args.no_open and not args.json:
        webbrowser.open(f"file://{os.path.abspath(output_path)}")


if __name__ == "__main__":
    main()

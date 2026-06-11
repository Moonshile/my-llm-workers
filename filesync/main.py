"""
文件同步工具 — 按最后修改时间将最新文件同步到同组其他位置。

根据 .filesync.yaml 中定义的文件组，找出每组中 mtime 最新的文件，
将其内容覆盖复制到组内其他文件。冲突时（多个文件具有相同最新 mtime
但内容不同）记录 WARNING 并跳过该组。

覆盖前自动备份被覆盖文件到 backups/ 目录，备份文件名包含日期和原始路径，
超过 30 天的备份自动滚动删除。

用法：
    uv run python filesync/main.py                 # 一次性同步
    uv run python filesync/main.py --dry-run       # 预览模式
    uv run python filesync/main.py --check         # 检查模式（有差异则 exit 1）
    uv run python filesync/main.py --backup-days 60  # 自定义备份保留天数
"""

import os
import sys
import argparse
import difflib
import hashlib
import logging
import re
import shutil
import time
import yaml
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_FILE = TOOL_DIR / ".filesync.yaml"
BACKUP_DIR = TOOL_DIR / "backups"
WORKER_CONFIG = TOOL_DIR / "worker.yaml"

# 模块级 logger：默认 NullHandler（静默），main() 中替换为完整配置
logger: logging.Logger = logging.getLogger("filesync")
logger.addHandler(logging.NullHandler())


# ---------- 日志 ----------

def setup_logging(backup_count: int = 7) -> logging.Logger:
    """配置日志：控制台(INFO) + run.log(INFO) + debug.log(DEBUG)，均每日轮转。"""
    log_dir = TOOL_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    _logger = logging.getLogger("filesync")
    _logger.setLevel(logging.DEBUG)

    # 控制台：INFO（简洁输出）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(ch)

    # run.log：INFO（关键事件：同步操作 / 错误 / 汇总）
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


# ---------- 路径处理 ----------

def expand_path(path_str: str) -> Path:
    """展开 ~ 和环境变量，解析为绝对路径。"""
    expanded = os.path.expanduser(os.path.expandvars(path_str))
    return Path(expanded).resolve()


# ---------- 文件信息 ----------

def file_info(path: Path) -> dict:
    """读取文件元信息：mtime、size、sha256、原始内容。"""
    stat = path.stat()
    content = path.read_bytes()
    return {
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "hash": hashlib.sha256(content).hexdigest(),
        "content": content,
    }


# ---------- 配置 ----------

def load_worker_config() -> dict:
    """加载 worker.yaml，获取调度元数据（log_retention_days 等）。"""
    if WORKER_CONFIG.exists():
        with open(WORKER_CONFIG, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def load_config() -> dict:
    """加载 .filesync.yaml 配置。"""
    if not CONFIG_FILE.exists():
        logger.error(f"配置文件不存在: {CONFIG_FILE}")
        logger.info(
            f"请在 {TOOL_DIR} 下创建 .filesync.yaml，"
            f"参考 .filesync.example.yaml 的格式"
        )
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------- 备份 ----------

def _safe_path(path: Path) -> str:
    """将绝对路径转为可用作文件名的字符串（/ → _）。"""
    # 去掉开头的 /，其余 / 替换为 _
    return str(path).lstrip("/").replace("/", "_")


def _parse_date_from_filename(filename: str) -> Optional[datetime]:
    """从备份文件名中提取日期，格式 YYYYMMDD-。失败返回 None。"""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-", filename)
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def backup_file(src_path: Path, group_name: str) -> Optional[Path]:
    """将被覆盖文件备份到 backups/ 目录。返回备份路径。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    ts = now.strftime("%Y%m%d-%H%M%S")
    safe = _safe_path(src_path)
    safe_group = group_name.replace("/", "_")
    backup_name = f"{ts}_{safe_group}__{safe}.bak"
    backup_path = BACKUP_DIR / backup_name

    shutil.copy2(src_path, backup_path)
    logger.debug(f"[{group_name}] 已备份: {src_path} → {backup_path}")
    return backup_path


def cleanup_backups(backup_days: int = 30) -> int:
    """滚动删除超过 backup_days 天的旧备份。返回删除数量。"""
    if not BACKUP_DIR.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=backup_days)
    deleted = 0

    for entry in BACKUP_DIR.iterdir():
        if not entry.is_file() or not entry.name.endswith(".bak"):
            continue
        d = _parse_date_from_filename(entry.name)
        if d is not None and d < cutoff:
            try:
                entry.unlink()
                logger.debug(f"清理旧备份: {entry.name}")
                deleted += 1
            except OSError:
                logger.warning(f"无法删除旧备份: {entry}")

    if deleted > 0:
        logger.info(f"清理了 {deleted} 个超过 {backup_days} 天的旧备份")

    return deleted


# ---------- 同步 ----------

def sync_group(group: dict, dry_run: bool = False) -> bool:
    """
    同步一个文件组。返回 True 表示执行了同步操作。

    冲突判定：当组内有多个文件具有相同的最新 mtime（1ms 容差内）但内容不同时，
    无法确定哪个是真正的"最新"，记录 WARNING 并跳过该组同步。
    """
    name = group.get("name", "unnamed")
    path_strs = group.get("paths", [])

    if len(path_strs) < 2:
        logger.warning(f"[{name}] 文件组少于 2 个路径，跳过")
        return False

    # 展开路径，分为已存在文件和缺失文件
    paths = [expand_path(p) for p in path_strs]
    infos: dict[Path, dict] = {}
    missing: list[Path] = []
    for p in paths:
        if p.exists() and p.is_file():
            infos[p] = file_info(p)
        elif p.is_dir():
            logger.warning(f"[{name}] 路径是目录而非文件: {p}")
        else:
            missing.append(p)

    if len(infos) == 0:
        logger.warning(f"[{name}] 组内没有可用文件，跳过同步")
        return False

    # 找到 mtime 最新的文件（作为同步源）
    latest_path = max(infos, key=lambda p: infos[p]["mtime"])
    latest = infos[latest_path]

    # 冲突检测：已有文件中，是否有与 latest 同 mtime 但内容不同
    MTIME_TOLERANCE = 0.001  # 1ms
    conflicts: list[Path] = []
    for p, info in infos.items():
        if p == latest_path:
            continue
        if (abs(info["mtime"] - latest["mtime"]) < MTIME_TOLERANCE
                and info["hash"] != latest["hash"]):
            conflicts.append(p)

    if conflicts:
        conflict_lines = "\n    ".join(str(c) for c in conflicts)
        logger.warning(
            f"[{name}] ⚠ 冲突：多个文件具有相同的最新修改时间 "
            f"({latest['mtime']}) 但内容不同，无法确定最新版本，跳过同步。\n"
            f"  最新候选: {latest_path}\n"
            f"  冲突文件:\n    {conflict_lines}"
        )
        return False

    synced = False

    # 同步已存在的文件（覆盖为最新内容）
    for p in infos:
        if p == latest_path:
            continue
        if infos[p]["hash"] == latest["hash"]:
            logger.debug(f"[{name}] 内容已相同，跳过: {p}")
            continue

        # 生成 diff 用于日志
        old_text = infos[p]["content"].decode("utf-8", errors="replace")
        new_text = latest["content"].decode("utf-8", errors="replace")
        diff_lines = list(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(p),
            tofile=str(latest_path),
        ))
        diff_text = "".join(diff_lines) if diff_lines else "(二进制文件或无可读差异)"

        prefix = "[DRY-RUN] " if dry_run else ""
        logger.info(
            f"[{name}] {prefix}同步: {latest_path} → {p}\n"
            f"  旧大小: {infos[p]['size']} bytes, 新大小: {latest['size']} bytes\n"
            f"  差异:\n{diff_text}"
        )

        if not dry_run:
            saved = backup_file(p, name)
            if saved:
                logger.info(f"[{name}] 已备份到: {saved}")
            shutil.copy2(latest_path, p)
            logger.info(f"[{name}] ✓ 已同步: {p}")
        synced = True

    # 创建缺失的文件（无需备份）
    for p in missing:
        prefix = "[DRY-RUN] " if dry_run else ""
        logger.info(
            f"[{name}] {prefix}创建: {latest_path} → {p}\n"
            f"  新大小: {latest['size']} bytes"
        )
        if not dry_run:
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(latest_path, p)
            logger.info(f"[{name}] ✓ 已创建: {p}")
        synced = True

    if not synced:
        logger.debug(f"[{name}] 无需同步")

    return synced


# ---------- 目录展开 ----------

def expand_group(group: dict) -> list[dict]:
    """
    展开目录同步组为文件级子组列表。

    如果 group 包含 `pattern` 字段，将所有 paths 视为目录，
    用 glob 扫描匹配文件，按文件名跨目录匹配后生成子组。
    无 pattern 时原样返回。
    """
    pattern = group.get("pattern")
    if not pattern:
        return [group]

    name = group.get("name", "unnamed")
    path_strs = group.get("paths", [])

    if len(path_strs) < 2:
        logger.warning(f"[{name}] 路径少于 2 个，跳过")
        return []

    # 展开路径，筛选有效目录（缺失的自动创建）
    dirs: list[Path] = []
    for p_str in path_strs:
        p = expand_path(p_str)
        if p.is_dir():
            dirs.append(p)
        elif not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            logger.info(f"[{name}] 目录不存在，已自动创建: {p}")
            dirs.append(p)
        else:
            logger.warning(f"[{name}] 路径存在但不是目录: {p}")

    if len(dirs) < 2:
        logger.warning(f"[{name}] 有效目录少于 2 个，跳过目录同步")
        return []

    # 扫描每个目录中匹配 pattern 的文件
    dir_files: dict[Path, dict[str, Path]] = {}
    all_filenames: set[str] = set()
    for d in dirs:
        files: dict[str, Path] = {}
        for f in sorted(d.glob(pattern)):
            if f.is_file():
                files[f.name] = f
                all_filenames.add(f.name)
        dir_files[d] = files

    if not all_filenames:
        logger.warning(f"[{name}] pattern '{pattern}' 未匹配到任何文件")
        return []

    # 按文件名构建子组
    sub_groups: list[dict] = []
    for fname in sorted(all_filenames):
        sub_paths: list[str] = []
        for d in dirs:
            if fname in dir_files[d]:
                sub_paths.append(str(dir_files[d][fname]))
            else:
                # 文件缺失，使用占位路径（后续 sync_group 会自动创建）
                sub_paths.append(str(d / fname))
        sub_groups.append({
            "name": f"{name}/{fname}",
            "paths": sub_paths,
        })

    logger.info(f"[{name}] 目录同步: 匹配到 {len(all_filenames)} 个文件, "
                f"分布在 {len(dirs)} 个目录")
    return sub_groups


# ---------- CLI ----------

def main():
    parser = argparse.ArgumentParser(
        description="文件同步工具 — 按 mtime 将最新文件同步到同组其他位置",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="预览模式：显示将要执行的操作，不实际修改文件",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="检查模式：仅检查是否有待同步的差异，有则 exit code=1",
    )
    parser.add_argument(
        "--backup-days", type=int, default=30,
        help="备份保留天数（默认 30，自动滚动删除旧备份）",
    )
    args = parser.parse_args()

    # 读取 worker.yaml 获取日志保留天数（与 md-frontmatter 一致）
    worker_cfg = load_worker_config()
    log_retention = worker_cfg.get("log_retention_days", 7)

    global logger
    logger = setup_logging(backup_count=log_retention)

    config = load_config()
    groups = config.get("groups", [])

    if not groups:
        logger.warning("配置中未定义任何文件组 (groups)")
        return

    synced_count = 0
    skip_count = 0
    error_count = 0

    for group in groups:
        try:
            sub_groups = expand_group(group)
            for sub in sub_groups:
                if sync_group(sub, dry_run=args.dry_run):
                    synced_count += 1
                else:
                    skip_count += 1
        except Exception:
            logger.error(
                f"[{group.get('name', 'unnamed')}] 同步出错",
                exc_info=True,
            )
            error_count += 1

    # 同步完成后清理旧备份
    if not args.dry_run:
        try:
            cleanup_backups(backup_days=args.backup_days)
        except Exception:
            logger.error("清理旧备份时出错", exc_info=True)

    logger.info(
        f"汇总: {synced_count} 组已同步, {skip_count} 组跳过/已一致, "
        f"{error_count} 组出错"
    )

    if args.check and (synced_count > 0 or error_count > 0):
        sys.exit(1)


if __name__ == "__main__":
    main()

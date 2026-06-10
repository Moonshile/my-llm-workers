"""
为指定目录下的所有 .md 文件递归添加 YAML frontmatter 元数据。

使用 LLM（OpenAI 兼容 API）根据内容智能生成 title、date、tags。
配置通过 .env 文件或环境变量注入，不放在 worker.yaml 中。

用法：
    python main.py                               # 从 .env 的 WATCH_PATHS 读取目录
    python main.py /path/to/dir --dry-run        # 手动指定目录预览
    python main.py /path/to/dir --update         # 更新已有 frontmatter
"""

import os
import sys
import json
import argparse
import logging
import re
import yaml
import litellm
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

TOOL_DIR = Path(__file__).resolve().parent


# ---------- 日志 ----------

def setup_logging(backup_count: int = 7) -> logging.Logger:
    """配置日志：控制台(INFO) + run.log(INFO) + debug.log(DEBUG)，均每日轮转。"""
    log_dir = TOOL_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    logger = logging.getLogger("md-frontmatter")
    logger.setLevel(logging.DEBUG)

    # 控制台：INFO
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    # run.log：INFO（重要事件）
    fh = TimedRotatingFileHandler(
        filename=log_dir / "run.log",
        when="midnight", interval=1, backupCount=backup_count, encoding="utf-8",
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # debug.log：DEBUG（含 SKIP 等细节）
    dh = TimedRotatingFileHandler(
        filename=log_dir / "debug.log",
        when="midnight", interval=1, backupCount=backup_count, encoding="utf-8",
    )
    dh.setLevel(logging.DEBUG)
    dh.setFormatter(fmt)
    logger.addHandler(dh)

    return logger


# ---------- 配置（全部来自环境变量） ----------

def get_config() -> dict:
    """加载 .env 并从环境变量读取 LLM 配置，缺失则报错退出。"""
    load_dotenv(TOOL_DIR / ".env")

    watch_paths_raw = os.environ.get("WATCH_PATHS", "")
    api_base = os.environ.get("API_BASE")
    api_key = os.environ.get("API_KEY")
    model = os.environ.get("MODEL")

    missing = []
    if not api_base:
        missing.append("API_BASE")
    if not api_key:
        missing.append("API_KEY")
    if not model:
        missing.append("MODEL")

    if missing:
        print(f"错误：缺少以下环境变量: {', '.join(missing)}")
        print("请在 md-frontmatter/.env 文件中配置，参考 .env.example")
        sys.exit(1)

    # 解析 WATCH_PATHS（展开 ~ 和环境变量）
    watch_paths = []
    if watch_paths_raw:
        watch_paths = [os.path.expanduser(p.strip()) for p in watch_paths_raw.split(",") if p.strip()]

    # 最短内容长度（低于此值跳过，防止不完整的文件过早打标）
    min_content_length = int(os.environ.get("MIN_CONTENT_LENGTH", "200"))

    # 草稿标记（逗号分隔，文件头部包含任一标记则跳过）
    draft_markers_raw = os.environ.get("DRAFT_MARKERS", "<!-- draft -->,<!-- wip -->")
    draft_markers = [m.strip() for m in draft_markers_raw.split(",") if m.strip()]

    return {
        "api_base": api_base,
        "api_key": api_key,
        "model": model,
        "watch_paths": watch_paths,
        "min_content_length": min_content_length,
        "draft_markers": draft_markers,
    }


# ---------- frontmatter 检测 ----------

def has_frontmatter(content: str) -> bool:
    """检查内容是否已有 YAML frontmatter（以 --- 开头且有闭合 ---）。"""
    stripped = content.strip()
    if not stripped.startswith("---"):
        return False
    lines = stripped.split("\n")
    if len(lines) < 2:
        return False
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return True
    return False


# ---------- 草稿检测 ----------

def has_draft_marker(content: str, markers: list[str]) -> bool:
    """检查内容开头是否包含草稿标记（如 <!-- draft -->、<!-- wip -->）。"""
    head = content[:500]
    head_lower = head.lower()
    for marker in markers:
        if marker.lower() in head_lower:
            return True
    return False


# ---------- frontmatter 标签检测 ----------

def frontmatter_missing_tags(content: str) -> bool:
    """检查已有 frontmatter 中 tags 字段是否缺失或为空。"""
    if not has_frontmatter(content):
        return False
    m = re.match(r"^---\r?\n(.*?)\r?\n---", content.strip(), re.DOTALL)
    if not m:
        return False
    fm_block = m.group(1)
    tags_match = re.search(r"^tags:\s*(.+)$", fm_block, re.MULTILINE)
    if not tags_match:
        return True  # 完全没有 tags 行
    tags_val = tags_match.group(1).strip()
    # 匹配 []、[ ] 等空列表
    if re.match(r"^\[\s*\]$", tags_val):
        return True
    return False


# ---------- 日期提取 ----------

def extract_date_from_filename(filepath: Path) -> str | None:
    """从文件名中提取 YYYY-MM-DD 格式日期。"""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:-|\.)", filepath.name)
    return match.group(1) if match else None


def extract_date_from_mtime(filepath: Path) -> str:
    """从文件修改时间提取日期。"""
    mtime = os.path.getmtime(str(filepath))
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


# ---------- title 提取 ----------

def extract_title_from_content(content: str) -> str | None:
    """从内容中提取第一个 # 标题。"""
    match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else None


# ---------- LLM 元数据生成 ----------

def build_metadata_prompt(content: str, filepath: Path) -> str:
    """构建 LLM prompt。"""
    max_len = 4000
    truncated = content[:max_len]
    if len(content) > max_len:
        truncated += "\n\n... (内容已截断)"

    return f"""Analyze this markdown file and generate YAML frontmatter metadata.

File info:
- Filename: {filepath.name}
- Parent directory: {filepath.parent.name}

Requirements:
- title: Concise, descriptive title. If the first heading is already a good title, use it (in the original language). Otherwise write one.
- date: Publication date in YYYY-MM-DD format. Extract from filename if it contains a date, otherwise estimate.
- tags: 3-5 relevant tags as a flat list. Use short, search-friendly terms (Chinese or English as appropriate). Include the parent directory name as one tag if relevant.

Return ONLY valid JSON (no markdown code fences, no extra text):
{{"title": "...", "date": "YYYY-MM-DD", "tags": ["tag1", "tag2", "tag3"]}}

Content:
{truncated}"""


def call_llm(prompt: str, config: dict, logger: logging.Logger) -> dict:
    """通过 LiteLLM 调用 LLM 生成元数据。"""
    model = config["model"]
    api_base = config["api_base"]
    api_key = config["api_key"]

    messages = [
        {"role": "system", "content": "You are a precise metadata generator. Return ONLY valid JSON."},
        {"role": "user", "content": prompt},
    ]

    prompt_chars = len(prompt)
    logger.info("  LLM 调用: model=%s, prompt=%d chars", model, prompt_chars)
    logger.debug("  LLM 请求详情: model=%s, prompt_chars=%d", model, prompt_chars)

    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            api_base=api_base,
            api_key=api_key,
            temperature=0.3,
            max_tokens=300,
            timeout=60,
        )
    except Exception as e:
        logger.error("  LLM 调用失败: %s", e)
        raise

    # 提取 usage 信息
    usage = getattr(response, "usage", None)
    if usage:
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", 0) or input_tokens + output_tokens
        # 尝试获取 cost（LiteLLM 可能跟踪了成本）
        cost = None
        if hasattr(response, "_hidden_params"):
            cost = response._hidden_params.get("response_cost", None)
        cost_str = f", cost=${cost:.6f}" if cost is not None else ""
        logger.debug(
            "  LLM 计费: input=%d, output=%d, total=%d tokens%s",
            input_tokens, output_tokens, total_tokens, cost_str,
        )
        logger.info(
            "  LLM 完成: %d input + %d output = %d tokens%s",
            input_tokens, output_tokens, total_tokens, cost_str,
        )
    else:
        logger.debug("  LLM 完成: 无 usage 信息")

    text = response.choices[0].message.content.strip()

    json_match = re.search(r"\{[\s\S]*\}", text)
    if json_match:
        return json.loads(json_match.group())
    return json.loads(text)


def generate_metadata(content: str, filepath: Path, config: dict, logger: logging.Logger) -> dict:
    """调用 LLM 生成元数据，失败时回退到启发式。"""
    try:
        prompt = build_metadata_prompt(content, filepath)
        result = call_llm(prompt, config, logger)
        return {
            "title": result.get("title") or extract_title_from_content(content) or filepath.stem,
            "date": result.get("date") or extract_date_from_filename(filepath) or extract_date_from_mtime(filepath),
            "tags": result.get("tags") or [filepath.parent.name],
        }
    except Exception as e:
        logger.warning("    LLM 调用失败 (%s)，回退到启发式模式", e)
        title = extract_title_from_content(content) or filepath.stem
        date = extract_date_from_filename(filepath) or extract_date_from_mtime(filepath)
        tags = [filepath.parent.name]
        return {"title": title, "date": date, "tags": tags}


# ---------- frontmatter 格式化 ----------

def format_frontmatter(metadata: dict) -> str:
    """将元数据格式化为 YAML frontmatter 字符串。"""
    lines = ["---"]
    lines.append(f"title: {metadata['title']}")
    lines.append(f"date: {metadata['date']}")

    tags = metadata.get("tags", [])
    if not tags:
        lines.append("tags: []")
    elif len(tags) == 1:
        lines.append(f"tags: [{tags[0]}]")
    else:
        lines.append("tags: [" + ", ".join(tags) + "]")

    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------- 目录处理 ----------

def process_directory(dir_path: Path, config: dict, log: logging.Logger, dry_run: bool, update: bool) -> dict:
    """处理单个目录下的所有 .md 文件，返回统计。"""
    if not dir_path.is_dir():
        log.warning("  跳过不存在的目录: %s", dir_path)
        return {"processed": 0, "skipped": 0, "updated": 0, "errors": 0}

    min_content_length = config.get("min_content_length", 200)
    draft_markers = config.get("draft_markers", ["<!-- draft -->", "<!-- wip -->"])

    md_files = sorted(dir_path.rglob("*.md"))
    log.info("扫描目录: %s", dir_path)
    log.info("找到 %d 个 .md 文件", len(md_files))

    counts = {"processed": 0, "skipped": 0, "updated": 0, "errors": 0}

    for md_file in md_files:
        rel_path = md_file.relative_to(dir_path)

        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as e:
            log.error("  ERR   %s (读取失败: %s)", rel_path, e)
            counts["errors"] += 1
            continue

        if not content.strip():
            log.debug("  SKIP  %s (空文件)", rel_path)
            counts["skipped"] += 1
            continue

        # 内容过短：防止不完整的文件过早打标
        content_len = len(content.strip())
        if content_len < min_content_length:
            log.debug("  SKIP  %s (内容过短: %d chars < %d)", rel_path, content_len, min_content_length)
            counts["skipped"] += 1
            continue

        # 草稿标记：<!-- draft --> 或 <!-- wip -->
        if draft_markers and has_draft_marker(content, draft_markers):
            log.debug("  SKIP  %s (草稿标记)", rel_path)
            counts["skipped"] += 1
            continue

        already_has = has_frontmatter(content)

        if already_has and not update:
            # 已有 frontmatter 但缺少 tags → 强制触发更新
            if not frontmatter_missing_tags(content):
                log.debug("  SKIP  %s (已有 frontmatter)", rel_path)
                counts["skipped"] += 1
                continue
            log.info("  TAGS  %s (frontmatter 缺少 tags)", rel_path)

        action = "UPDT" if already_has else "PROC"
        log.info("  %s  %s", action, rel_path)

        try:
            metadata = generate_metadata(content, md_file, config, log)
            frontmatter = format_frontmatter(metadata)

            if already_has:
                body = re.sub(r"^---\n.*?---\n", "", content, count=1, flags=re.DOTALL)
                new_content = frontmatter + "\n" + body.lstrip("\n")
            else:
                new_content = frontmatter + "\n" + content

            if dry_run:
                log.info("    → title: \"%s\"", metadata["title"])
                log.info("    → date:  %s", metadata["date"])
                log.info("    → tags:  %s", metadata["tags"])
            else:
                md_file.write_text(new_content, encoding="utf-8")
                log.info("    ✓ title: \"%s\"", metadata["title"])
                log.info("    ✓ date:  %s", metadata["date"])
                log.info("    ✓ tags:  %s", metadata["tags"])

            if already_has:
                counts["updated"] += 1
            else:
                counts["processed"] += 1

        except Exception as e:
            log.error("    ✗ 错误: %s", e)
            counts["errors"] += 1

    log.info("  → 目录完成: 新增 %d, 更新 %d, 跳过 %d, 错误 %d\n",
             counts["processed"], counts["updated"], counts["skipped"], counts["errors"])
    return counts


# ---------- worker.yaml 配置 ----------

def load_worker_config() -> dict:
    """从 worker.yaml 读取运行参数（如日志保留天数）。"""
    worker_yaml = TOOL_DIR / "worker.yaml"
    if worker_yaml.exists():
        with open(worker_yaml) as f:
            return yaml.safe_load(f) or {}
    return {}


# ---------- 主逻辑 ----------

def main():
    parser = argparse.ArgumentParser(
        description="为指定目录下的 .md 文件递归添加 YAML frontmatter 元数据（LLM 模式）"
    )
    parser.add_argument(
        "directory", nargs="?",
        help="目标目录（可选，不指定则使用 .env 中的 WATCH_PATHS）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="预览模式：仅显示将要添加的元数据，不实际修改文件",
    )
    parser.add_argument(
        "--update", action="store_true",
        help="更新已有 frontmatter 的文件（默认跳过）",
    )

    args = parser.parse_args()

    worker_cfg = load_worker_config()
    log_retention = worker_cfg.get("log_retention_days", 7)
    log = setup_logging(backup_count=log_retention)

    # 加载配置
    config = get_config()

    # 确定要处理的目录列表
    if args.directory:
        dirs = [Path(os.path.expanduser(args.directory))]
    elif config["watch_paths"]:
        dirs = [Path(p) for p in config["watch_paths"]]
    else:
        log.error("错误：未指定目录。请通过命令行参数传入或设置 .env 中的 WATCH_PATHS。")
        sys.exit(1)

    log.info("LLM: %s @ %s", config["model"], config["api_base"])
    log.info("处理 %d 个目录\n", len(dirs))

    total = {"processed": 0, "skipped": 0, "updated": 0, "errors": 0}

    for dir_path in dirs:
        counts = process_directory(dir_path, config, log, args.dry_run, args.update)
        for k in total:
            total[k] += counts[k]

    log.info("=== 总计 ===")
    log.info("新增 frontmatter: %d", total["processed"])
    log.info("更新 frontmatter: %d", total["updated"])
    log.info("跳过:            %d", total["skipped"])
    log.info("错误:            %d", total["errors"])
    if args.dry_run:
        log.info("[预览模式] 未修改任何文件。")


if __name__ == "__main__":
    main()

"""
扫描 Claude Code session 并生成结构化工作日志。

每天处理最后修改时间为昨天及之前的所有 session，通过 LLM 生成包含
工作概览、高复杂度工作、多轮交互分析、最佳实践等维度的日志文档。
支持增量更新：已处理过的 session 只分析新增部分并重生成文档。

用法：
    python main.py                          # 正常模式
    python main.py --dry-run                # 预览模式，不实际调用 LLM
    python main.py --session-id <uuid>      # 只处理指定 session
"""

import os
import sys
import json
import argparse
import logging
import re
import yaml
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import litellm
from dotenv import load_dotenv

TOOL_DIR = Path(__file__).resolve().parent

# 模块级 logger：默认 NullHandler（静默），main() 中替换
logger: logging.Logger = logging.getLogger("agent-session-journal")
logger.addHandler(logging.NullHandler())


class _SessionStats:
    """追踪单个 session 处理过程中的 LLM 调用统计。"""

    def __init__(self):
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.total_cost = 0.0
        self.cost_estimated = False

    def record(self, input_t: int, output_t: int, cost: Optional[float], estimated: bool,
               cache_read_t: int = 0):
        self.requests += 1
        self.input_tokens += input_t
        self.output_tokens += output_t
        self.cache_read_tokens += cache_read_t
        if cost is not None:
            self.total_cost += cost
            if estimated:
                self.cost_estimated = True

    def summary(self) -> str:
        cost_label = "预估" if self.cost_estimated else ""
        parts = [
            f"requests={self.requests}",
            f"input_tokens={self.input_tokens}",
            f"output_tokens={self.output_tokens}",
            f"total_tokens={self.input_tokens + self.output_tokens}",
        ]
        if self.cache_read_tokens > 0:
            parts.append(f"cache_hit={self.cache_read_tokens}")
        parts.append(f"{cost_label}cost=¥{self.total_cost:.6f}")
        return ", ".join(parts)


# 当前 session 的统计，process_session 开始时重置
_current_stats: Optional[_SessionStats] = None


# ============================================================================
# 日志
# ============================================================================

def setup_logging(backup_count: int = 7) -> logging.Logger:
    """配置日志：控制台(INFO) + run.log(INFO) + debug.log(DEBUG)，均每日轮转。"""
    log_dir = TOOL_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    _logger = logging.getLogger("agent-session-journal")
    _logger.setLevel(logging.DEBUG)

    # 控制台：INFO（简洁输出）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(ch)

    # run.log：INFO（关键事件）
    fh = TimedRotatingFileHandler(
        filename=log_dir / "run.log",
        when="midnight", interval=1, backupCount=backup_count, encoding="utf-8",
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    _logger.addHandler(fh)

    # debug.log：DEBUG（含 SKIP 等细节）
    dh = TimedRotatingFileHandler(
        filename=log_dir / "debug.log",
        when="midnight", interval=1, backupCount=backup_count, encoding="utf-8",
    )
    dh.setLevel(logging.DEBUG)
    dh.setFormatter(fmt)
    _logger.addHandler(dh)

    return _logger


# ============================================================================
# 配置
# ============================================================================

def load_worker_config() -> dict:
    """加载 worker.yaml，获取调度元数据（log_retention_days 等）。"""
    worker_yaml = TOOL_DIR / "worker.yaml"
    if worker_yaml.exists():
        with open(worker_yaml, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def get_config() -> dict:
    """加载配置：worker.yaml（非保密） + .env（保密），env 可覆盖 yaml。"""
    load_dotenv(TOOL_DIR / ".env")
    worker_cfg = load_worker_config()

    # 保密配置仅从 .env 读取
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
        print("请在 agent-session-journal/.env 文件中配置，参考 .env.example")
        sys.exit(1)

    # 隐私配置（路径）：仅从 .env 读取
    output_dir_raw = os.environ.get(
        "OUTPUT_DIR", "~/Documents/claude-session-journals"
    )
    output_dir = Path(os.path.expanduser(output_dir_raw))

    session_dirs_raw = os.environ.get("SESSION_DIRS", "")
    if session_dirs_raw:
        session_dirs = [
            Path(os.path.expanduser(p.strip()))
            for p in session_dirs_raw.split(",") if p.strip()
        ]
    else:
        session_dirs = [
            Path(os.path.expanduser("~/.claude/sessions")),
            Path(os.path.expanduser("~/.claude/projects")),
        ]

    serious_paths_raw = os.environ.get("SERIOUS_WORK_PATHS", "")
    if serious_paths_raw:
        serious_work_paths = [
            os.path.expanduser(p.strip())
            for p in serious_paths_raw.split(",") if p.strip()
        ]
    else:
        serious_work_paths = []

    # 非保密配置：优先 .env 覆盖，其次 worker.yaml，最后默认值
    max_chunk_chars = int(os.environ.get(
        "MAX_CHUNK_CHARS",
        worker_cfg.get("max_chunk_chars", 8000),
    ))
    chunk_overlap = int(os.environ.get(
        "CHUNK_OVERLAP",
        worker_cfg.get("chunk_overlap", 1000),
    ))

    # 模型计费表：优先 worker.yaml 配置，用于覆盖/补充硬编码计费表
    # 格式：{prefix: {input, output, cache_hit_input?}}
    model_pricing_cfg = worker_cfg.get("model_pricing", {})
    model_pricing: dict[str, dict] = {}
    if isinstance(model_pricing_cfg, dict):
        for prefix, prices in model_pricing_cfg.items():
            if isinstance(prices, dict) and "input" in prices and "output" in prices:
                entry: dict = {
                    "input": float(prices["input"]),
                    "output": float(prices["output"]),
                }
                if "cache_hit_input" in prices:
                    entry["cache_hit_input"] = float(prices["cache_hit_input"])
                model_pricing[prefix] = entry

    batch_size = int(worker_cfg.get("batch_size", 5))

    return {
        "api_base": api_base,
        "api_key": api_key,
        "model": model,
        "output_dir": output_dir,
        "session_dirs": session_dirs,
        "max_chunk_chars": max_chunk_chars,
        "chunk_overlap": chunk_overlap,
        "serious_work_paths": serious_work_paths,
        "model_pricing": model_pricing,
        "batch_size": batch_size,
    }


# ============================================================================
# Session 发现
# ============================================================================

def _get_session_creation_ts(jsonl_path: Path) -> Optional[float]:
    """从 JSONL 第一个事件中获取 session 创建时间戳（毫秒级）。"""
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            first_line = f.readline()
            if not first_line.strip():
                return None
            event = json.loads(first_line)
            ts_str = event.get("timestamp", "")
            if ts_str:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                return dt.timestamp()
    except Exception:
        pass
    return None


def discover_sessions(dirs: list[Path]) -> list[dict]:
    """
    扫描所有 session 目录，返回 session 信息列表。

    返回 [{session_id, project_path, jsonl_path, mtime, mtime_date, created_date, ...}]
    """
    sessions: dict[str, dict] = {}  # key: (session_id, project_path) 用于去重
    today_str = datetime.now().strftime("%Y-%m-%d")

    for d in dirs:
        d_expanded = Path(os.path.expanduser(str(d)))
        if not d_expanded.exists():
            logger.debug("跳过不存在的目录: %s", d_expanded)
            continue

        # 扫描项目目录下的 .jsonl 文件
        if "projects" in str(d_expanded) or d_expanded.suffix == ".jsonl":
            jsonl_files = list(d_expanded.rglob("*.jsonl")) if d_expanded.is_dir() else [d_expanded]
            for f in jsonl_files:
                # 排除 subagents 和 memory 子目录
                if "subagents" in f.parts or "memory" in f.parts:
                    continue
                sid = f.stem  # 文件名去掉 .jsonl 即为 session_id
                mtime = os.path.getmtime(str(f))
                mtime_dt = datetime.fromtimestamp(mtime)
                mtime_date = mtime_dt.strftime("%Y-%m-%d")

                # 跳过当天修改的 session
                if mtime_date == today_str:
                    logger.debug("跳过当天修改的 session: %s (%s)", sid, mtime_date)
                    continue

                # 推导项目路径
                # f 形如 ~/.claude/projects/-Users-xxx-proj-foo/uuid.jsonl
                proj_parts = []
                for part in f.parts:
                    if part == "projects":
                        proj_parts = []
                    elif part.endswith(".jsonl"):
                        break
                    else:
                        proj_parts.append(part)
                project_path = "/" + "/".join(proj_parts) if proj_parts else "unknown"

                # 获取创建日期
                created_ts = _get_session_creation_ts(f)
                created_date = (
                    datetime.fromtimestamp(created_ts).strftime("%Y-%m-%d")
                    if created_ts else mtime_date  # 回退到 mtime
                )

                key = f"{sid}::{project_path}"
                if key not in sessions or mtime > sessions[key]["mtime"]:
                    sessions[key] = {
                        "session_id": sid,
                        "project_path": project_path,
                        "jsonl_path": str(f),
                        "mtime": mtime,
                        "mtime_date": mtime_date,
                        "created_date": created_date,
                    }

        # 扫描全局 sessions 目录获取元数据
        elif "sessions" in str(d_expanded) and d_expanded.is_dir():
            for f in d_expanded.glob("*.json"):
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                except (json.JSONDecodeError, OSError):
                    continue

                sid = meta.get("sessionId", "")
                if not sid:
                    continue

                # 检查是否已有 JSONL 对应的条目
                matched = False
                for key, sess in list(sessions.items()):
                    if sess["session_id"] == sid:
                        # 补充元数据中的创建时间
                        if meta.get("startedAt") and not sess.get("_meta_started_at"):
                            started_ts = meta["startedAt"] / 1000 if meta["startedAt"] > 1e12 else meta["startedAt"]
                            sess["created_date"] = datetime.fromtimestamp(started_ts).strftime("%Y-%m-%d")
                        matched = True
                        break

                if not matched:
                    # 没有 JSONL 文件但有元数据记录（可能全局 session）
                    updated_at = meta.get("updatedAt", 0)
                    mtime = updated_at / 1000 if updated_at > 1e12 else updated_at
                    mtime_dt = datetime.fromtimestamp(mtime) if mtime > 0 else datetime.now()
                    mtime_date = mtime_dt.strftime("%Y-%m-%d")

                    if mtime_date == today_str:
                        continue

                    started_ts = meta.get("startedAt", updated_at)
                    if started_ts:
                        created_ts = started_ts / 1000 if started_ts > 1e12 else started_ts
                        created_date = datetime.fromtimestamp(created_ts).strftime("%Y-%m-%d")
                    else:
                        created_date = mtime_date

                    key = f"{sid}::global"
                    sessions[key] = {
                        "session_id": sid,
                        "project_path": meta.get("cwd", "unknown"),
                        "jsonl_path": None,  # 无 JSONL 文件
                        "mtime": mtime,
                        "mtime_date": mtime_date,
                        "created_date": created_date,
                    }

    return list(sessions.values())


# ============================================================================
# 对话压缩
# ============================================================================

def extract_condensed_transcript(
    jsonl_path: str | Path | None,
    since_timestamp: Optional[float] = None,
    max_chars: Optional[int] = None,
) -> str:
    """
    从 JSONL 文件中提取压缩后的对话内容。

    压缩规则：
    - user: 提取 message.content 中的文本
    - assistant: 只提取 text 块，跳过 thinking，标注 tool_use
    - 跳过 mode/permission-mode/file-history-snapshot/ai-title/last-prompt/attachment
    """
    if jsonl_path is None or not Path(jsonl_path).exists():
        return ""

    lines = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")
                event_ts_str = event.get("timestamp", "")
                event_ts = None
                if event_ts_str:
                    try:
                        dt = datetime.fromisoformat(event_ts_str.replace("Z", "+00:00"))
                        event_ts = dt.timestamp()
                    except (ValueError, TypeError):
                        pass

                # 增量模式：跳过已处理过的事件
                if since_timestamp is not None and event_ts is not None and event_ts <= since_timestamp:
                    continue

                # user 消息
                if event_type == "user":
                    msg = event.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        text_parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                            elif isinstance(block, str):
                                text_parts.append(block)
                        content = " ".join(text_parts)
                    if content and content.strip():
                        # 过滤本地命令标记
                        if "<local-command-caveat>" in content:
                            continue
                        # 过滤 slash 命令
                        if content.strip().startswith("<command-name>/"):
                            continue
                        lines.append(f"[用户] {content.strip()}")

                # assistant 消息
                elif event_type == "assistant":
                    msg = event.get("message", {})
                    content_blocks = msg.get("content", [])
                    text_parts = []
                    tool_names = []
                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type", "")
                        if bt == "text":
                            text_parts.append(block.get("text", ""))
                        elif bt == "tool_use":
                            name = block.get("name", "?")
                            tool_names.append(name)

                    tool_note = ""
                    if tool_names:
                        tool_note = f" [使用工具: {', '.join(tool_names)}]"

                    if text_parts:
                        combined = " ".join(text_parts)
                        lines.append(f"[助手]{tool_note} {combined.strip()}")

                # system 消息
                elif event_type == "system":
                    msg = event.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") if isinstance(c, dict) else str(c)
                            for c in content
                        )
                    if content and content.strip():
                        lines.append(f"[系统] {content.strip()}")

    except Exception as e:
        logger.warning("提取对话时出错: %s - %s", jsonl_path, e)
        return ""

    result = "\n".join(lines)

    # 截断（用于控制单次 LLM 调用的输入大小）
    if max_chars and len(result) > max_chars:
        result = result[:max_chars] + "\n\n... (对话内容已截断)"

    return result


# ============================================================================
# Frontmatter 解析与生成
# ============================================================================

def parse_frontmatter(content: str) -> Optional[dict]:
    """解析 markdown 文件的 YAML frontmatter。无 frontmatter 返回 None。"""
    stripped = content.strip()
    if not stripped.startswith("---"):
        return None
    # 找闭合的 ---
    m = re.match(r"^---\r?\n(.*?)\r?\n---", stripped, re.DOTALL)
    if not m:
        return None
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None


def format_frontmatter(meta: dict) -> str:
    """将元数据格式化为 YAML frontmatter 字符串。"""
    lines = ["---"]
    # 先输出固定字段（保持顺序），再输出其余字段
    order = ["title", "date", "tags", "type", "category", "sessions",
             "session_id", "project_path", "last_processed_timestamp"]
    seen = set()
    for key in order:
        if key not in meta:
            continue
        seen.add(key)
        val = meta[key]
        if key == "tags":
            if not val:
                lines.append("tags: []")
            else:
                lines.append("tags: [" + ", ".join(val) + "]")
        elif key == "sessions":
            lines.append("sessions:")
            for s in val:
                lines.append(f"  - session_id: {s['session_id']}")
                lines.append(f"    title: {s['title']}")
                lines.append(f"    project_path: {s.get('project_path', '')}")
                lines.append(f"    category: {s.get('category', '')}")
                lines.append(f"    last_processed_timestamp: {s['last_processed_timestamp']}")
        elif isinstance(val, str):
            lines.append(f"{key}: {val}")
        else:
            lines.append(f"{key}: {val}")

    # 输出不在固定列表中的其余字段
    for key, val in meta.items():
        if key in seen:
            continue
        if isinstance(val, str):
            lines.append(f"{key}: {val}")
        else:
            lines.append(f"{key}: {val}")

    lines.append("---")
    return "\n".join(lines) + "\n"


def slugify(title: str) -> str:
    """将标题转换为文件名友好的 slug。"""
    # 转小写
    slug = title.lower()
    # 中文字符保持不变，但去掉中文标点
    # 将空格、中文标点替换为连字符
    slug = re.sub(r'[\s,，。！？、：；""（）【】《》/\\]+', '-', slug)
    # 去掉非字母数字中文连字符的字符
    slug = re.sub(r'[^a-z0-9一-鿿-]', '', slug)
    # 合并多个连字符
    slug = re.sub(r'-+', '-', slug)
    # 去掉首尾连字符
    slug = slug.strip('-')
    # 限制长度
    if len(slug) > 80:
        slug = slug[:80].rstrip('-')
    return slug or "untitled"


# ============================================================================
# 已有文档查找
# ============================================================================

def find_existing_document(output_dir: Path, session_id: str) -> Optional[Path]:
    """在输出目录中按 session_id 查找已有独立文档（不含 daily brief）。"""
    if not output_dir.exists():
        return None
    for md_file in output_dir.rglob("*.md"):
        # 跳过 daily brief 文件
        if md_file.name.endswith("-daily.md"):
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
            fm = parse_frontmatter(content)
            if fm and fm.get("session_id") == session_id:
                return md_file
        except Exception:
            continue
    return None


def _find_daily_brief(output_dir: Path, date_str: str) -> Optional[Path]:
    """查找指定日期的 daily brief 文件（每天一个，不分分类）。"""
    daily_dir = output_dir / "daily"
    if not daily_dir.exists():
        return None
    target = daily_dir / f"{date_str}-daily.md"
    return target if target.exists() else None


def _read_daily_brief_sessions(daily_path: Path) -> tuple[dict, dict, str]:
    """
    读取 daily brief 文件，返回 (sessions_dict, frontmatter, body)。

    sessions_dict: {session_id: {last_processed_timestamp, title, project_path}}
    """
    try:
        content = daily_path.read_text(encoding="utf-8")
    except Exception:
        return {}, {}, ""

    fm = parse_frontmatter(content) or {}
    sessions_list = fm.get("sessions", [])
    if not isinstance(sessions_list, list):
        sessions_list = []

    sessions_dict = {}
    for s in sessions_list:
        if isinstance(s, dict) and s.get("session_id"):
            sessions_dict[s["session_id"]] = {
                "last_processed_timestamp": s.get("last_processed_timestamp", 0),
                "title": s.get("title", ""),
                "project_path": s.get("project_path", ""),
                "category": s.get("category", ""),
            }

    # 提取 body（frontmatter 之后的内容）
    m = re.match(r"^---\r?\n.*?\r?\n---\r?\n?", content, re.DOTALL)
    body = content[m.end():] if m else ""

    return sessions_dict, fm, body


def _find_session_in_daily_briefs(output_dir: Path, session_id: str) -> Optional[dict]:
    """
    在所有 daily brief 文件中查找指定 session。

    返回 {daily_path, last_processed_timestamp, ...} 或 None。
    """
    if not output_dir.exists():
        return None
    daily_dir = output_dir / "daily"
    if not daily_dir.exists():
        return None
    for md_file in daily_dir.glob("*daily.md"):
        sessions_dict, fm, _ = _read_daily_brief_sessions(md_file)
        if session_id in sessions_dict:
            return {
                "daily_path": md_file,
                **sessions_dict[session_id],
            }
    return None


def get_existing_categories(output_dir: Path) -> list[str]:
    """获取输出目录中已存在的分类子目录列表。"""
    if not output_dir.exists():
        return []
    return [
        d.name for d in output_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]


# ============================================================================
# 预扫描
# ============================================================================

def pre_scan_sessions(sessions: list[dict], config: dict) -> list[dict]:
    """
    预扫描所有 session，过滤出确实需要处理的。

    对每个 session：
    1. 检查是否有新内容（mtime > last_processed_ts）
    2. 提取对话，过滤空内容
    3. 返回需要处理的 session 列表（含预提取的 transcript）

    返回 [{session, transcript, is_update, ref_content, last_processed_ts, prev_daily_info}]
    """
    output_dir = config["output_dir"]
    to_process = []

    for session in sessions:
        sid = session["session_id"]
        jsonl_path = session.get("jsonl_path")
        mtime = session["mtime"]

        # 查找已有文档
        existing_doc_path = find_existing_document(output_dir, sid)
        existing_doc_content = None
        last_processed_ts = None
        prev_daily_info = None

        if existing_doc_path:
            try:
                content = existing_doc_path.read_text(encoding="utf-8")
                fm = parse_frontmatter(content)
                if fm:
                    last_processed_ts = fm.get("last_processed_timestamp")
                existing_doc_content = content
            except Exception:
                pass
        else:
            prev_daily_info = _find_session_in_daily_briefs(output_dir, sid)
            if prev_daily_info:
                last_processed_ts = prev_daily_info.get("last_processed_timestamp")

        # 跳过无新内容的 session
        if last_processed_ts is not None and mtime <= last_processed_ts:
            logger.debug("  SKIP  %s (无新内容)", sid[:20])
            continue

        # 提取对话
        transcript = extract_condensed_transcript(
            jsonl_path,
            since_timestamp=last_processed_ts,
        )

        if not transcript.strip():
            logger.debug("  SKIP  %s (对话为空)", sid[:20])
            continue

        # 构造参考内容（用于增量更新）
        is_update = existing_doc_content is not None or prev_daily_info is not None
        ref_content = existing_doc_content
        if prev_daily_info and not ref_content:
            ref_content = f"此前已记录：{prev_daily_info.get('title', '')}"

        to_process.append({
            "session": session,
            "transcript": transcript,
            "is_update": is_update,
            "ref_content": ref_content,
            "last_processed_ts": last_processed_ts,
            "prev_daily_info": prev_daily_info,
            "existing_doc_path": str(existing_doc_path) if existing_doc_path else None,
        })

    return to_process


# ============================================================================
# LLM 调用
# ============================================================================

_PROMPT_TEMPLATE: Optional[str] = None


def _load_prompt_template() -> str:
    """加载 prompt.md 模板（首次调用后缓存）。"""
    global _PROMPT_TEMPLATE
    if _PROMPT_TEMPLATE is None:
        _PROMPT_TEMPLATE = (TOOL_DIR / "prompt.md").read_text(encoding="utf-8")
    return _PROMPT_TEMPLATE


def build_summary_prompt(
    transcript: str,
    session_meta: dict,
    existing_doc_content: str | None,
    existing_categories: list[str],
    is_update: bool,
) -> str:
    """构造发送给 LLM 的 prompt（从 prompt.md 模板注入变量）。"""

    # 构造 context_section
    if is_update and existing_doc_content:
        ref = existing_doc_content[-2000:] if len(existing_doc_content) > 2000 else existing_doc_content
        context_section = (
            "## 说明：这是增量更新\n"
            "以下是**自上次处理后新增的对话内容**。请基于已有文档和新增内容，"
            "生成一份完整的更新版文档。保持结构与已有文档一致，融合新旧内容。\n\n"
            f"### 已有文档（参考）\n{ref}\n\n"
            f"### 新增对话内容\n{transcript}"
        )
    else:
        context_section = f"## 对话内容\n{transcript}"

    # 构造 categories_section
    if existing_categories:
        cats_str = ", ".join(existing_categories)
        categories_section = (
            "## 备注：已有分类目录\n"
            f"输出目录中已有以下分类: {cats_str}\n"
            "请优先从已有分类中选择 category，若无匹配则创建新分类。\n"
            "分类应宽泛通用（如 前端开发、后端开发、工具脚本、AI应用、文档写作），\n"
            "不要太具体（不要用项目名或具体功能名）。分类名只能是一级（不含 /）。"
        )
    else:
        categories_section = ""

    template = _load_prompt_template()
    # 使用简单字符串替换（避免 .format() 被 JSON 示例中的花括号干扰）
    result = template
    replacements = {
        "{session_id}": session_meta["session_id"],
        "{project_path}": session_meta["project_path"],
        "{created_date}": session_meta.get("created_date", "unknown"),
        "{update_date}": session_meta.get("mtime_date", "unknown"),
        "{context_section}": context_section,
        "{categories_section}": categories_section,
    }
    for key, val in replacements.items():
        result = result.replace(key, val)
    return result


def call_llm(prompt: str, config: dict, label: str = "") -> dict:
    """通过 LiteLLM 调用 LLM，返回解析后的 JSON。"""
    model = config["model"]
    # 如果模型名不含 provider 前缀，默认用 openai/（兼容 OpenAI 兼容 API）
    if "/" not in model:
        model = f"openai/{model}"
    api_base = config["api_base"]
    api_key = config["api_key"]

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个精准的工作日志生成器。"
                "请严格只返回 JSON，不要 markdown 代码块，不要其他任何文字。"
            ),
        },
        {"role": "user", "content": prompt},
    ]

    prompt_chars = len(prompt)
    label_suffix = f" [{label}]" if label else ""
    logger.info("  LLM 调用%s: model=%s, prompt=%d chars", label_suffix, model, prompt_chars)
    logger.debug("  LLM 请求详情: model=%s, prompt_chars=%d", model, prompt_chars)

    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            api_base=api_base,
            api_key=api_key,
            temperature=0.3,
            max_tokens=4000,
            timeout=120,
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
        # 缓存命中 token（Anthropic: cache_read_input_tokens, DeepSeek 兼容）
        cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
        if cache_read_tokens == 0:
            # DeepSeek 可能用 prompt_cache_hit_tokens 或其它字段
            cache_read_tokens = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        # 优先取 LiteLLM 跟踪的 cost，否则本地估算
        cost = None
        if hasattr(response, "_hidden_params"):
            cost = response._hidden_params.get("response_cost", None)
        estimated = cost is None
        if estimated:
            cost = _estimate_cost(model, input_tokens, output_tokens,
                                  model_pricing=config.get("model_pricing"),
                                  cache_read_tokens=cache_read_tokens)
        # 构造日志中的缓存命中信息
        cache_str = ""
        if cache_read_tokens > 0:
            cache_str = f", cache_hit={cache_read_tokens}"
        cost_str = f", 预估费用 ¥{cost:.6f}" if estimated and cost is not None else (
            f", 费用 ¥{cost:.6f}" if cost is not None else ""
        )
        logger.debug(
            "  LLM 计费: input=%d, output=%d, total=%d tokens%s%s",
            input_tokens, output_tokens, total_tokens, cache_str, cost_str,
        )
        logger.info(
            "  LLM 完成: %d input + %d output = %d tokens%s%s",
            input_tokens, output_tokens, total_tokens, cache_str, cost_str,
        )
        # 累计到 session 统计（统计失败不影响主流程）
        if _current_stats is not None:
            try:
                _current_stats.record(input_tokens, output_tokens, cost, estimated,
                                      cache_read_t=cache_read_tokens)
            except Exception as e:
                logger.debug("  统计记录异常: %s", e)
    else:
        logger.debug("  LLM 完成: 无 usage 信息")

    text = response.choices[0].message.content.strip()

    # 尝试提取 JSON（优先数组，其次对象，可能被 markdown 代码块包裹）
    # 1) JSON 数组（批量响应）
    arr_match = re.search(r"\[[\s\S]*\]", text)
    if arr_match:
        try:
            return json.loads(arr_match.group())
        except json.JSONDecodeError:
            pass
    # 2) JSON 对象（单个响应）
    obj_match = re.search(r"\{[\s\S]*\}", text)
    if obj_match:
        try:
            return json.loads(obj_match.group())
        except json.JSONDecodeError:
            pass
    # 3) 最后尝试直接解析全文
    return json.loads(text)


# 模型预估单价（¥/1M tokens），用于 LiteLLM 无法提供 cost 时的本地估算
# 注意：前缀匹配，更具体的条目放在前面
# DeepSeek 系列模型的计费统一在 worker.yaml 的 model_pricing 中配置，
# 此处的 deepseek 条目仅作为未配置时的默认回退
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # (input_price, output_price) per 1M tokens, unit: ¥
    "claude-opus-4": (108.0, 540.0),
    "claude-sonnet-4": (22.0, 108.0),
    "claude-haiku-4": (5.8, 29.0),
    "claude-fable-5": (22.0, 108.0),
    "gpt-4o": (18.0, 72.0),
    "gpt-4o-mini": (1.1, 4.3),
    "gpt-4.1": (14.4, 57.6),
    "gemini-2.5-flash": (1.1, 4.3),
    "gemini-2.5-pro": (9.0, 72.0),
    # DeepSeek 默认回退（优先使用 worker.yaml 中的配置）
    "deepseek-v4-pro": (3.0, 6.0),
    "deepseek-v4-flash": (1.0, 2.0),
    "deepseek-v4": (3.0, 6.0),
    "deepseek-v3": (2.0, 8.0),
    "deepseek-r1": (4.0, 16.0),
    "deepseek-chat": (2.0, 8.0),
    "deepseek-reasoner": (4.0, 16.0),
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int,
                   model_pricing: Optional[dict[str, dict]] = None,
                   cache_read_tokens: int = 0) -> Optional[float]:
    """根据模型名和 token 用量估算费用。匹配不到返回 None。

    model_pricing 来自 worker.yaml 配置，优先级高于硬编码 _MODEL_PRICING。
    cache_read_tokens: 缓存命中的输入 token 数（适用缓存命中价）。
    """
    model_lower = model.lower()
    # 先查 worker.yaml 配置的计费表（支持 cache_hit_input）
    if model_pricing:
        for prefix, prices in model_pricing.items():
            if prefix in model_lower:
                in_price = prices["input"]
                out_price = prices["output"]
                cache_hit_price = prices.get("cache_hit_input")
                if cache_read_tokens > 0 and cache_hit_price is not None:
                    miss_tokens = max(0, input_tokens - cache_read_tokens)
                    return (
                        (miss_tokens / 1_000_000) * in_price
                        + (cache_read_tokens / 1_000_000) * cache_hit_price
                        + (output_tokens / 1_000_000) * out_price
                    )
                return (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price
    # 再查硬编码计费表（无缓存命中区分）
    for prefix, (in_price, out_price) in _MODEL_PRICING.items():
        if prefix in model_lower:
            return (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price
    return None


def _encode_real_path(real_path: str) -> str:
    """将真实文件系统路径编码为 session project_path 格式（每个 `/` 替换为 `-`）。"""
    real_path = os.path.expanduser(real_path)
    # /Users/x/proj → -Users-x-proj → /-Users-x-proj
    return "/" + real_path.replace("/", "-")


def _ensure_tags_include_serious(tags: list[str]) -> list[str]:
    """确保 tags 中包含 '严肃工作'（去重）。"""
    if "严肃工作" not in tags:
        tags.append("严肃工作")
    return tags


def _is_serious_work(project_path: str, serious_paths: list[str]) -> bool:
    """
    判断 session 的 project_path 是否匹配任一严肃工作路径。

    都使用 Claude 的编码规则（`/` → `-`）后做前缀比对，避免编解码横线歧义。
    """
    for sp in serious_paths:
        encoded_sp = _encode_real_path(sp)
        if project_path == encoded_sp:
            return True
        # 检查是否为子路径：prefix 后必须紧跟 -（即原路径的 / 边界）
        if project_path.startswith(encoded_sp + "-"):
            return True
    return False


def _build_document_content(llm_result: dict, session_meta: dict, processing_time: str) -> str:
    """根据 LLM 返回结果构造 markdown 文档正文。支持 simple/complex 两种模式。"""
    title = llm_result.get("title", "Untitled")
    session_id = session_meta["session_id"]
    project_path = session_meta["project_path"]
    update_date = session_meta["mtime_date"]
    complexity = llm_result.get("complexity", "complex")

    parts = [
        f"# {title}",
        "",
        f"**Session ID**: `{session_id}`",
        f"**项目路径**: `{project_path}`",
        f"**最后更新**: {update_date}",
        f"**处理时间**: {processing_time}",
    ]

    # Simple 模式：只输出一句话总结
    if complexity == "simple":
        summary = llm_result.get("summary", "")
        if summary:
            parts.extend(["", summary])
        return "\n".join(parts) + "\n"

    # Complex 模式：自由结构 sections
    sections = llm_result.get("sections", [])
    if isinstance(sections, list) and sections:
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            heading = sec.get("heading", "")
            content = sec.get("content", "")
            if heading:
                parts.extend(["", f"## {heading}", ""])
            if content:
                parts.append(content)
    else:
        # 兼容旧格式（overview/complex_work 等）
        summary = llm_result.get("summary", "")
        if summary:
            parts.extend(["", summary])

    return "\n".join(parts) + "\n"


def _get_chunk_summary_prompt(transcript_chunk: str, chunk_idx: int, total_chunks: int) -> str:
    """构造分块摘要 prompt。"""
    return f"""请分析以下会话记录片段（第 {chunk_idx + 1}/{total_chunks} 块），提取关键信息。

严格基于对话事实，不要猜测。只返回 JSON（不含 markdown 代码块），格式：
{{"summary":"这段对话做了什么（一两句话）","key_points":["关键事件或决策1","关键事件或决策2"],"issues":["遇到的问题或踩坑"],"unresolved":["未解决的问题"]}}

对话内容：
{transcript_chunk}"""


def _synthesize_chunks(chunk_results: list[dict], config: dict, session_meta: dict,
                        existing_categories: list[str]) -> dict:
    """将分块摘要合成为最终文档。"""
    chunks_text = json.dumps(chunk_results, ensure_ascii=False, indent=2)
    max_len = 8000
    if len(chunks_text) > max_len:
        chunks_text = chunks_text[:max_len] + "\n... (截断)"

    existing_cat_str = ", ".join(existing_categories) if existing_categories else "无"
    prompt = f"""请将以下多个会话片段的摘要合成为一份完整的结构化工作日志。

会话元数据：
- Session ID: {session_meta['session_id']}
- 项目路径: {session_meta['project_path']}
- 创建日期: {session_meta.get('created_date', 'unknown')}
- 最后更新: {session_meta.get('mtime_date', 'unknown')}
- 已有分类目录: {existing_cat_str}

各片段摘要：
{chunks_text}

请输出完整 JSON（不含 markdown 代码块），包含：
- `complexity`: "simple" 或 "complex"
- `title`: 描述性标题
- `tags`: 3-5 个标签
- `category`: 宽泛的分类名（不要太具体）
- `summary`: 一两句话总结
- `sections`: 自由组织的章节数组，每项含 heading（章节标题）和 content（markdown 内容）

根据内容特点自行决定章节结构，去掉各片段间的重复，合并同类项。
严格基于对话原文，不猜测。"""

    return call_llm(prompt, config)


# ============================================================================
# 批量处理
# ============================================================================

def build_batch_prompt(batch: list[dict], config: dict, existing_categories: list[str]) -> str:
    """为多个 session 构造批量处理的 prompt。"""
    n = len(batch)
    cats_str = ", ".join(existing_categories) if existing_categories else "无"

    parts = [
        f"你是一个精准的工作日志生成器。请分析以下 {n} 个 Claude Code 会话记录，"
        f"为每个生成结构化工作日志。",
        "",
        f"已有分类目录: {cats_str}（优先复用已有分类，不要用项目名作为分类）",
        "",
    ]

    for i, info in enumerate(batch, 1):
        s = info["session"]
        parts.append(f"## 会话 {i}")
        parts.append(f"- Session ID: {s['session_id']}")
        parts.append(f"- 项目路径: {s['project_path']}")
        parts.append(f"- 日期: {s.get('created_date', 'unknown')}")

        if info["is_update"] and info["ref_content"]:
            ref = info["ref_content"]
            if len(ref) > 1500:
                ref = ref[-1500:]
            parts.append(f"- 说明: 增量更新，已有文档参考: {ref[:500]}...")

        parts.append("")
        parts.append("### 对话内容")
        # 截断过长的对话（单 session 不超过 20000 字符）
        transcript = info["transcript"]
        if len(transcript) > 20000:
            transcript = transcript[:20000] + "\n... (截断)"
        parts.append(transcript)
        parts.append("")

    parts.append("## 输出要求")
    parts.append("")
    parts.append("为每个会话返回一个 JSON 对象，字段：")
    parts.append("- `complexity`: \"simple\"(简单问答/单步操作) 或 \"complex\"(技术决策/多步调试)")
    parts.append("- `title`: 描述性标题")
    parts.append("- `tags`: 3-5个标签的数组")
    parts.append("- `category`: 宽泛分类名（如 AI应用/工具脚本/前端开发/后端开发/文档写作/技术调研/部署与优化）")
    parts.append("- `summary`: 一两句话总结（simple 模式的核心输出）")
    parts.append("- `sections`: complex 时输出章节数组[{heading, content}], simple 时空数组")
    parts.append("")
    parts.append("严格只返回 JSON 数组（不含 markdown 代码块），顺序与输入一致：")
    parts.append("```")
    parts.append("[")
    parts.append(f"  {{\"complexity\":\"...\", \"title\":\"...\", ...}},  // 会话 1")
    parts.append(f"  {{\"complexity\":\"...\", \"title\":\"...\", ...}},  // 会话 2")
    parts.append("  ...")
    parts.append("]")
    parts.append("```")

    return "\n".join(parts)


def _write_session_doc(session_info: dict, llm_result: dict, config: dict) -> Optional[str]:
    """
    将单个 session 的 LLM 分析结果写入文档。
    simple → daily brief，complex → 独立文件。
    返回文件路径或 None。
    """
    session = session_info["session"]
    sid = session["session_id"]
    project_path = session["project_path"]
    mtime = session["mtime"]
    mtime_date = session["mtime_date"]
    created_date = session["created_date"]
    jsonl_path = session.get("jsonl_path")
    transcript = session_info["transcript"]
    output_dir = config["output_dir"]
    serious_paths = config.get("serious_work_paths", [])
    prev_daily_info = session_info.get("prev_daily_info")
    existing_doc_path_str = session_info.get("existing_doc_path")
    existing_doc_path = Path(existing_doc_path_str) if existing_doc_path_str else None

    # 处理标签和分类
    tags = llm_result.get("tags", [])
    if not isinstance(tags, list):
        tags = [tags] if tags else []

    is_serious = _is_serious_work(project_path, serious_paths)
    if is_serious:
        tags = _ensure_tags_include_serious(tags)

    category = llm_result.get("category", "未分类")
    if is_serious:
        category = "严肃工作"
    elif category == "严肃工作":
        category = "未分类"
    if not category or "/" in str(category):
        category = "未分类"

    # 计算 last_processed_timestamp
    last_ts = _compute_last_processed_ts(transcript, jsonl_path)
    if last_ts is None:
        last_ts = mtime

    title = llm_result.get("title", "Untitled")
    processing_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    complexity = llm_result.get("complexity", "complex")

    # 输出：simple → daily brief，complex → 独立文件
    if complexity == "simple":
        # 如果之前是独立文件，删除
        if existing_doc_path:
            try:
                existing_doc_path.unlink()
                logger.debug("  转为 simple，删除旧独立文件: %s", existing_doc_path)
            except OSError:
                pass

        # 读取或创建 daily brief
        daily_path = _find_daily_brief(output_dir, created_date)
        if daily_path is None:
            daily_dir = output_dir / "daily"
            daily_dir.mkdir(parents=True, exist_ok=True)
            daily_path = daily_dir / f"{created_date}-daily.md"

        sessions_dict, _, _ = _read_daily_brief_sessions(daily_path) if daily_path.exists() else ({}, {}, "")

        sessions_dict[sid] = {
            "last_processed_timestamp": last_ts,
            "title": title,
            "project_path": project_path,
            "category": category,
            "summary": llm_result.get("summary", ""),
        }

        entries = [
            {
                "session_id": k,
                "title": v["title"],
                "project_path": v.get("project_path", ""),
                "category": v.get("category", ""),
                "summary": v.get("summary", ""),
                "last_processed_timestamp": v["last_processed_timestamp"],
            }
            for k, v in sessions_dict.items()
        ]
        _write_daily_brief(daily_path, entries, created_date, processing_time)
        if len(entries) == 1:
            logger.info("  ✓ daily/%s-daily.md", created_date)
        logger.info("[RESULT] session=%s complexity=simple category=%s file=daily/%s-daily.md entries=%d | %s",
                    sid, category, created_date, len(entries), _current_stats.summary())
        return str(daily_path)

    else:
        # Complex: 独立文件
        if prev_daily_info:
            old_daily = prev_daily_info["daily_path"]
            sessions_dict, _, _ = _read_daily_brief_sessions(old_daily)
            if sid in sessions_dict:
                del sessions_dict[sid]
                if sessions_dict:
                    entries = [
                        {
                            "session_id": k,
                            "title": v["title"],
                            "project_path": v.get("project_path", ""),
                            "category": v.get("category", ""),
                            "summary": v.get("summary", ""),
                            "last_processed_timestamp": v["last_processed_timestamp"],
                        }
                        for k, v in sessions_dict.items()
                    ]
                    _write_daily_brief(old_daily, entries, created_date, processing_time)
                else:
                    try:
                        old_daily.unlink()
                    except OSError:
                        pass

        frontmatter_meta = {
            "title": title,
            "date": mtime_date,
            "tags": tags,
            "session_id": sid,
            "project_path": project_path,
            "last_processed_timestamp": last_ts,
        }
        fm = format_frontmatter(frontmatter_meta)
        body = _build_document_content(llm_result, session, processing_time)
        full_doc = fm + "\n" + body

        slug = slugify(title)
        filename = f"{created_date}-{slug}.md"
        cat_dir = output_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        new_path = cat_dir / filename

        if existing_doc_path and existing_doc_path != new_path:
            try:
                existing_doc_path.unlink()
                logger.debug("  删除旧文件: %s", existing_doc_path)
            except OSError:
                pass

        new_path.write_text(full_doc, encoding="utf-8")
        logger.info("  ✓ %s/%s", category, filename)
        logger.info("[RESULT] session=%s complexity=complex category=%s file=%s/%s | %s",
                    sid, category, category, filename, _current_stats.summary())
        return str(new_path)


def process_batch(batch: list[dict], config: dict, dry_run: bool = False) -> int:
    """
    批量处理多个 session：一次 LLM 调用 → 多份文档。

    返回成功处理的 session 数。
    """
    global _current_stats
    _current_stats = _SessionStats()

    if not batch:
        return 0

    output_dir = config["output_dir"]
    existing_cats = get_existing_categories(output_dir)
    n = len(batch)

    if dry_run:
        total_chars = sum(len(info["transcript"]) for info in batch)
        logger.info("  DRY-RUN 批量处理 %d 个 session (%d chars)", n, total_chars)
        return 0

    # 构造 prompt 并调用 LLM
    prompt = build_batch_prompt(batch, config, existing_cats)
    logger.info("  批量处理 %d 个 session, prompt=%d chars", n, len(prompt))

    try:
        response = call_llm(prompt, config, label=f"batch-{n}")
    except Exception as e:
        logger.error("  批量 LLM 调用失败: %s", e)
        return 0

    # 解析响应：期望 JSON 数组
    if isinstance(response, list):
        results = response
    elif isinstance(response, dict):
        # 可能被包在 {"sessions": [...]} 或 {"results": [...]} 中
        results = response.get("sessions") or response.get("results") or []
        if not isinstance(results, list):
            logger.error("  批量响应格式错误：期望数组，得到 %s", type(response).__name__)
            return 0
    else:
        logger.error("  批量响应格式错误：期望数组，得到 %s", type(response).__name__)
        return 0

    # 对齐：确保 results 数量与 batch 一致
    if len(results) != n:
        logger.warning("  批量响应数量不匹配: 期望 %d, 得到 %d", n, len(results))
        # 截断或补空
        while len(results) < n:
            results.append({"complexity": "simple", "title": "未知", "tags": [],
                           "category": "未分类", "summary": "LLM 未返回结果", "sections": []})
        results = results[:n]

    # 写入各 session 文档
    processed = 0
    for i, (info, result) in enumerate(zip(batch, results)):
        if not isinstance(result, dict):
            logger.warning("  会话 %d/%d 结果非 dict，跳过", i + 1, n)
            continue
        try:
            path = _write_session_doc(info, result, config)
            if path:
                processed += 1
        except Exception as e:
            logger.error("  会话 %d/%d 写入失败: %s", i + 1, n, e)

    return processed


def _write_daily_brief(daily_path: Path, sessions_entries: list[dict],
                       date_str: str, processing_time: str):
    """写入 daily brief 文件（每天一个）。有实质内容的按分类分组展示，空 session 聚合到末尾。"""
    sessions_entries.sort(key=lambda s: (s.get("category", ""), s.get("title", "")))

    # 分离有意义和无内容的 session（summary 为空或极短视为空）
    meaningful: dict[str, list[dict]] = {}
    empty_sessions = []
    for s in sessions_entries:
        summary = s.get("summary", "").strip()
        if len(summary) < 10:
            empty_sessions.append(s)
        else:
            cat = s.get("category", "未分类")
            if cat not in meaningful:
                meaningful[cat] = []
            meaningful[cat].append(s)

    total = len(sessions_entries)
    meaningful_count = sum(len(v) for v in meaningful.values())

    # 构造 frontmatter
    fm_meta = {
        "title": f"每日简报 — {date_str}",
        "date": date_str,
        "type": "daily-brief",
        "sessions": [
            {
                "session_id": s["session_id"],
                "title": s["title"],
                "project_path": s.get("project_path", ""),
                "category": s.get("category", ""),
                "last_processed_timestamp": s["last_processed_timestamp"],
            }
            for s in sessions_entries
        ],
    }
    fm = format_frontmatter(fm_meta)

    # 构造正文
    parts = [
        f"# 每日简报 — {date_str}",
        "",
        f"**日期**: {date_str}",
        f"**处理时间**: {processing_time}",
        f"**共 {total} 个会话**（有内容 {meaningful_count}，空 {len(empty_sessions)}）",
        "",
    ]

    # 有意义的内容，按分类分组
    for cat in sorted(meaningful.keys()):
        entries = meaningful[cat]
        if len(meaningful) > 1:
            parts.append(f"## {cat}")
            parts.append("")
        for s in entries:
            title = s.get("title", "Untitled")
            sid = s.get("session_id", "")
            proj = s.get("project_path", "")
            summary = s.get("summary", "")
            parts.append(f"### {title}")
            parts.append(f"Session `{sid}` | 项目 `{proj}`")
            parts.append("")
            parts.append(summary)
            parts.append("")

    # 空 session 聚合
    if empty_sessions:
        parts.append("## 空session")
        parts.append("")
        for s in empty_sessions:
            title = s.get("title", "Untitled")
            sid = s.get("session_id", "")
            proj = s.get("project_path", "")
            parts.append(f"- {title} — `{sid}` (`{proj}`)")
        parts.append("")

    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_text(fm + "\n" + "\n".join(parts) + "\n", encoding="utf-8")


# ============================================================================
# 文档生成与输出
# ============================================================================

def _compute_last_processed_ts(transcript: str, jsonl_path: Optional[str]) -> Optional[float]:
    """计算处理到的最新事件时间戳。"""
    if not jsonl_path or not Path(jsonl_path).exists():
        return None
    max_ts = None
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = event.get("timestamp", "")
                if ts_str:
                    try:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        ts = dt.timestamp()
                        if max_ts is None or ts > max_ts:
                            max_ts = ts
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    return max_ts


def process_session(
    session: dict,
    config: dict,
    dry_run: bool = False,
) -> Optional[str]:
    """
    处理单个 session：提取对话、调用 LLM、生成文档。

    Simple 模式写入 daily brief，complex 模式写入独立文件。

    处理完成后输出 session 级别的 LLM 统计日志。
    返回生成的文档路径（dry_run 或 skip 返回 None）。
    """
    global _current_stats
    _current_stats = _SessionStats()

    sid = session["session_id"]
    project_path = session["project_path"]
    jsonl_path = session.get("jsonl_path")
    mtime = session["mtime"]
    mtime_date = session["mtime_date"]
    created_date = session["created_date"]
    output_dir = config["output_dir"]
    serious_paths = config.get("serious_work_paths", [])

    # 1. 查找已有文档（独立文件 + daily brief）
    existing_doc_path = find_existing_document(output_dir, sid)
    existing_doc_content = None
    last_processed_ts = None
    prev_daily_info = None  # 之前是否在 daily brief 中

    if existing_doc_path:
        try:
            content = existing_doc_path.read_text(encoding="utf-8")
            fm = parse_frontmatter(content)
            if fm:
                last_processed_ts = fm.get("last_processed_timestamp")
            existing_doc_content = content
        except Exception:
            pass
    else:
        # 在 daily brief 中查找
        prev_daily_info = _find_session_in_daily_briefs(output_dir, sid)
        if prev_daily_info:
            last_processed_ts = prev_daily_info.get("last_processed_timestamp")

    # 检查是否有增量
    if last_processed_ts is not None and mtime <= last_processed_ts:
        logger.debug("  SKIP  %s (无新内容)", sid[:20])
        return None

    # 2. 提取压缩后的对话
    transcript = extract_condensed_transcript(
        jsonl_path,
        since_timestamp=last_processed_ts,
    )

    if not transcript.strip():
        logger.debug("  SKIP  %s (对话为空)", sid[:20])
        return None

    # 3. 获取已有分类目录
    existing_cats = get_existing_categories(output_dir)

    is_update = existing_doc_content is not None or prev_daily_info is not None
    # 对于 daily brief 中的旧条目，传递简要上下文
    ref_content = existing_doc_content
    if prev_daily_info and not ref_content:
        ref_content = f"此前已记录：{prev_daily_info.get('title', '')}"
    if is_update and existing_doc_content:
        ref_content = existing_doc_content

    if dry_run:
        logger.info("  DRY-RUN %s (%d chars)", sid[:20], len(transcript))
        return None

    # 4. 调用 LLM 生成文档
    try:
        max_chars = config.get("max_chunk_chars", 8000)
        if len(transcript) <= max_chars:
            prompt = build_summary_prompt(
                transcript, session,
                ref_content, existing_cats, is_update,
            )
            llm_result = call_llm(prompt, config)
        else:
            logger.debug("  分块处理 (%d chars > %d)", len(transcript), max_chars)
            chunk_size = max(1000, max_chars - 2000)
            overlap = config.get("chunk_overlap", 1000)
            chunks = []
            start = 0
            while start < len(transcript):
                end = min(start + chunk_size, len(transcript))
                chunks.append(transcript[start:end])
                start = end - overlap if end < len(transcript) else end

            chunk_results = []
            for i, chunk in enumerate(chunks):
                chunk_prompt = _get_chunk_summary_prompt(chunk, i, len(chunks))
                try:
                    chunk_result = call_llm(chunk_prompt, config)
                    chunk_results.append(chunk_result)
                except Exception as e:
                    logger.warning("  分块 %d/%d 处理失败: %s", i + 1, len(chunks), e)

            if not chunk_results:
                logger.error("  所有分块处理失败")
                return None

            llm_result = _synthesize_chunks(
                chunk_results, config, session, existing_cats,
            )
    except Exception as e:
        logger.error("  LLM 调用失败 (%s): %s", sid[:20], e)
        return None

    # 5. 处理标签和分类
    tags = llm_result.get("tags", [])
    if not isinstance(tags, list):
        tags = [tags] if tags else []

    is_serious = _is_serious_work(project_path, serious_paths)

    if is_serious:
        tags = _ensure_tags_include_serious(tags)

    category = llm_result.get("category", "未分类")
    if is_serious:
        category = "严肃工作"
    elif category == "严肃工作":
        category = "未分类"
    if not category or "/" in str(category):
        category = "未分类"

    # 6. 计算 last_processed_timestamp
    last_ts = _compute_last_processed_ts(transcript, jsonl_path)
    if last_ts is None:
        last_ts = mtime

    title = llm_result.get("title", "Untitled")
    processing_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    complexity = llm_result.get("complexity", "complex")

    # 7. 输出：simple → daily brief，complex → 独立文件
    if complexity == "simple":
        # 如果之前是独立文件，删除它
        if existing_doc_path:
            try:
                existing_doc_path.unlink()
                logger.debug("  转为 simple，删除旧独立文件: %s", existing_doc_path)
            except OSError:
                pass

        # 读取或创建 daily brief（每天一个文件）
        daily_path = _find_daily_brief(output_dir, created_date)
        if daily_path is None:
            daily_dir = output_dir / "daily"
            daily_dir.mkdir(parents=True, exist_ok=True)
            daily_path = daily_dir / f"{created_date}-daily.md"

        sessions_dict, _, _ = _read_daily_brief_sessions(daily_path) if daily_path.exists() else ({}, {}, "")

        # 更新或新增此 session 条目
        sessions_dict[sid] = {
            "last_processed_timestamp": last_ts,
            "title": title,
            "project_path": project_path,
            "category": category,
            "summary": llm_result.get("summary", ""),
        }

        # 重写 daily brief
        entries = [
            {
                "session_id": k,
                "title": v["title"],
                "project_path": v.get("project_path", ""),
                "category": v.get("category", ""),
                "summary": v.get("summary", ""),
                "last_processed_timestamp": v["last_processed_timestamp"],
            }
            for k, v in sessions_dict.items()
        ]
        _write_daily_brief(daily_path, entries, created_date, processing_time)
        logger.info("  ✓ daily/%s-daily.md (%d sessions)", created_date, len(entries))
        logger.info("[RESULT] session=%s complexity=simple category=%s file=daily/%s-daily.md entries=%d | %s",
                    sid, category, created_date, len(entries), _current_stats.summary())
        return str(daily_path)

    else:
        # Complex: 独立文件
        # 如果之前在 daily brief 中，移除
        if prev_daily_info:
            old_daily = prev_daily_info["daily_path"]
            sessions_dict, _, _ = _read_daily_brief_sessions(old_daily)
            if sid in sessions_dict:
                del sessions_dict[sid]
                if sessions_dict:
                    entries = [
                        {
                            "session_id": k,
                            "title": v["title"],
                            "project_path": v.get("project_path", ""),
                            "category": v.get("category", ""),
                            "summary": v.get("summary", ""),
                            "last_processed_timestamp": v["last_processed_timestamp"],
                        }
                        for k, v in sessions_dict.items()
                    ]
                    _write_daily_brief(old_daily, entries, created_date, processing_time)
                else:
                    try:
                        old_daily.unlink()
                    except OSError:
                        pass

        frontmatter_meta = {
            "title": title,
            "date": mtime_date,
            "tags": tags,
            "session_id": sid,
            "project_path": project_path,
            "last_processed_timestamp": last_ts,
        }
        fm = format_frontmatter(frontmatter_meta)
        body = _build_document_content(llm_result, session, processing_time)
        full_doc = fm + "\n" + body

        slug = slugify(title)
        filename = f"{created_date}-{slug}.md"
        cat_dir = output_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        new_path = cat_dir / filename

        if existing_doc_path and existing_doc_path != new_path:
            try:
                existing_doc_path.unlink()
                logger.debug("  删除旧文件: %s", existing_doc_path)
            except OSError:
                pass

        new_path.write_text(full_doc, encoding="utf-8")
        logger.info("  ✓ %s/%s", category, filename)
        logger.info("[RESULT] session=%s complexity=complex category=%s file=%s/%s | %s",
                    sid, category, category, filename, _current_stats.summary())
        return str(new_path)


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="扫描 Claude Code session 并生成结构化工作日志",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="预览模式：仅显示将要处理的 session，不实际调用 LLM",
    )
    parser.add_argument(
        "--preview", type=str, metavar="SESSION_ID",
        help="预览指定 session：调用 LLM 生成结果并输出到终端，不写入文件",
    )
    parser.add_argument(
        "--session-id", type=str,
        help="只处理指定 session_id",
    )
    args = parser.parse_args()

    # 读取 worker.yaml 获取日志保留天数
    worker_cfg = load_worker_config()
    log_retention = worker_cfg.get("log_retention_days", 7)

    global logger
    logger = setup_logging(backup_count=log_retention)

    # 加载配置
    config = get_config()

    logger.info("Agent Session Journal")
    logger.info("LLM: %s @ %s", config["model"], config["api_base"])
    logger.info("输出目录: %s", config["output_dir"])
    logger.info("会话目录: %s", ", ".join(str(d) for d in config["session_dirs"]))
    if config.get("serious_work_paths"):
        logger.info("严肃工作路径: %s", ", ".join(config["serious_work_paths"]))
    logger.info("")

    # 发现 session（preview 模式也需要扫全量以匹配 session_id）
    sessions = discover_sessions(config["session_dirs"])

    # --preview 模式：调用 LLM 并直接输出到终端，不写入文件
    if args.preview:
        target = [s for s in sessions if s["session_id"] == args.preview]
        if not target:
            logger.error("未找到 session: %s", args.preview)
            sys.exit(1)
        session = target[0]
        logger.info("预览 session: %s", args.preview)
        transcript = extract_condensed_transcript(session.get("jsonl_path"))
        if not transcript.strip():
            logger.error("对话为空，无法预览")
            sys.exit(1)
        existing_cats = get_existing_categories(config["output_dir"])
        prompt = build_summary_prompt(transcript, session, None, existing_cats, False)
        llm_result = call_llm(prompt, config)
        processing_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 输出 JSON 结果
        print("\n========== LLM 原始结果 ==========")
        print(json.dumps(llm_result, ensure_ascii=False, indent=2))
        # 输出渲染后的 markdown
        complexity = llm_result.get("complexity", "complex")
        title = llm_result.get("title", "Untitled")
        tags = llm_result.get("tags", [])
        category = llm_result.get("category", "未分类")
        frontmatter_meta = {
            "title": title,
            "date": session["mtime_date"],
            "tags": tags,
            "session_id": session["session_id"],
            "project_path": session["project_path"],
            "last_processed_timestamp": 0,
        }
        fm = format_frontmatter(frontmatter_meta)
        body = _build_document_content(llm_result, session, processing_time)
        print("\n========== 渲染结果 ==========")
        print(fm + "\n" + body)
        return

    # --session-id 模式：单个 session，保留完整处理逻辑（含分块）
    if args.session_id:
        target = [s for s in sessions if s["session_id"] == args.session_id]
        if not target:
            logger.error("未找到 session: %s", args.session_id)
            sys.exit(1)
        session = target[0]
        logger.info("处理指定 session: %s", args.session_id)
        try:
            result_path = process_session(session, config, dry_run=args.dry_run)
            if result_path:
                logger.info("完成: %s", result_path)
            else:
                logger.info("跳过（无新内容或 dry-run）")
        except Exception:
            logger.error("处理出错", exc_info=True)
            sys.exit(1)
        return

    # 预扫描：过滤出确实需要处理的 session
    logger.info("发现 %d 个 session（排除当天修改），开始预扫描...", len(sessions))
    to_process = pre_scan_sessions(sessions, config)
    logger.info("预扫描完成: %d 个需要处理，%d 个跳过",
                len(to_process), len(sessions) - len(to_process))
    logger.info("")

    if not to_process:
        logger.info("没有需要处理的 session。")
        return

    # 分组：长对话独立处理，短对话批量
    batch_size = config.get("batch_size", 5)
    max_chars = config.get("max_chunk_chars", 8000)
    individual_queue = []
    batch_queue = []

    for info in to_process:
        transcript_len = len(info["transcript"])
        if transcript_len > max_chars or info["is_update"]:
            # 长对话或增量更新 → 独立处理（保留分块和增量逻辑）
            individual_queue.append(info)
        else:
            batch_queue.append(info)

    logger.info("分组: %d 个独立处理, %d 个批量处理 (%d 个/批)",
                len(individual_queue), len(batch_queue), batch_size)

    # 全局 LLM 统计
    global_requests = 0
    global_input_tokens = 0
    global_output_tokens = 0
    global_cache_read_tokens = 0
    global_cost = 0.0
    global_cost_estimated = False

    processed = 0
    skipped = 0
    errors = 0
    total = len(to_process)

    # 生成批次
    batches = [batch_queue[i:i + batch_size] for i in range(0, len(batch_queue), batch_size)]
    # 独立处理的 session 每个单独一"批"
    for info in individual_queue:
        batches.append([info])

    for batch_idx, batch in enumerate(batches, 1):
        n = len(batch)
        # 进度显示
        if n == 1:
            s = batch[0]["session"]
            sid = s["session_id"]
            short_id = sid[:20] if len(sid) > 20 else sid
            logger.info("[%d/%d] [%s] %s (独立)", batch_idx, len(batches),
                       s["mtime_date"], short_id)
        else:
            logger.info("[%d/%d] 批量 %d 个 session", batch_idx, len(batches), n)

        try:
            if n == 1 and batch[0] in individual_queue:
                # 独立处理：使用原有的完整流程（含分块/增量）
                info = batch[0]
                session = info["session"]
                # 构造 ref_content 供 process_session 使用（需临时设置）
                # process_session 内部会重新查找，但我们的 pre_scan 已经做了
                # 直接调用即可（它内部会再次检查 mtime 和提取 transcript）
                result_path = process_session(session, config, dry_run=args.dry_run)
                if result_path:
                    processed += 1
                else:
                    skipped += 1
            else:
                # 批量处理
                batch_processed = process_batch(batch, config, dry_run=args.dry_run)
                processed += batch_processed
                skipped += (n - batch_processed)

            # 累计全局统计
            if _current_stats is not None:
                global_requests += _current_stats.requests
                global_input_tokens += _current_stats.input_tokens
                global_output_tokens += _current_stats.output_tokens
                global_cache_read_tokens += _current_stats.cache_read_tokens
                global_cost += _current_stats.total_cost
                if _current_stats.cost_estimated:
                    global_cost_estimated = True
        except Exception:
            logger.error("[批次 %d] 处理出错", batch_idx, exc_info=True)
            errors += n

        logger.info("[进度] %d/%d 批次完成（处理 %d，跳过 %d，错误 %d）",
                    batch_idx, len(batches), processed, skipped, errors)

    logger.info("")
    logger.info("=== 汇总 ===")
    logger.info("Session: 预扫描 %d → 处理 %d / 跳过 %d / 错误 %d",
                len(sessions), processed, skipped, errors)
    if global_requests > 0:
        total_tokens = global_input_tokens + global_output_tokens
        cost_label = "预估" if global_cost_estimated else ""
        cache_str = ""
        if global_cache_read_tokens > 0:
            cache_str = f", 缓存命中 {global_cache_read_tokens}"
        logger.info(
            "LLM: %d 次调用, input=%d output=%d total=%d%s, %scost=¥%.6f",
            global_requests, global_input_tokens, global_output_tokens,
            total_tokens, cache_str, cost_label, global_cost,
        )
    if args.dry_run:
        logger.info("[预览模式] 未实际调用 LLM。")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.critical("未捕获的异常导致退出", exc_info=True)
        sys.exit(1)

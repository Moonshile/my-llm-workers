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
    """加载 .env 并从环境变量读取配置，缺失则报错退出。"""
    load_dotenv(TOOL_DIR / ".env")

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

    output_dir_raw = os.environ.get("OUTPUT_DIR", "~/Documents/claude-session-journals")
    output_dir = Path(os.path.expanduser(output_dir_raw))

    session_dirs_raw = os.environ.get("SESSION_DIRS", "~/.claude/sessions,~/.claude/projects")
    session_dirs = [
        Path(os.path.expanduser(p.strip()))
        for p in session_dirs_raw.split(",") if p.strip()
    ]

    max_chunk_chars = int(os.environ.get("MAX_CHUNK_CHARS", "8000"))

    serious_paths_raw = os.environ.get("SERIOUS_WORK_PATHS", "")
    serious_work_paths = []
    if serious_paths_raw:
        serious_work_paths = [
            os.path.expanduser(p.strip())
            for p in serious_paths_raw.split(",") if p.strip()
        ]

    return {
        "api_base": api_base,
        "api_key": api_key,
        "model": model,
        "output_dir": output_dir,
        "session_dirs": session_dirs,
        "max_chunk_chars": max_chunk_chars,
        "serious_work_paths": serious_work_paths,
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
    # 保持字段顺序: title, date, tags, session_id, project_path, last_processed_timestamp
    order = ["title", "date", "tags", "session_id", "project_path", "last_processed_timestamp"]
    for key in order:
        if key not in meta:
            continue
        val = meta[key]
        if key == "tags":
            if not val:
                lines.append("tags: []")
            else:
                lines.append("tags: [" + ", ".join(val) + "]")
        elif isinstance(val, str):
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
    """在输出目录中按 session_id（frontmatter 字段）查找已有文档。"""
    if not output_dir.exists():
        return None
    for md_file in output_dir.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            fm = parse_frontmatter(content)
            if fm and fm.get("session_id") == session_id:
                return md_file
        except Exception:
            continue
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
# LLM 调用
# ============================================================================

def build_summary_prompt(
    transcript: str,
    session_meta: dict,
    existing_doc_content: str | None,
    existing_categories: list[str],
    is_update: bool,
) -> str:
    """构造发送给 LLM 的 prompt。"""
    max_len = 6000
    truncated = transcript[:max_len]
    if len(transcript) > max_len:
        truncated += "\n\n... (对话已截断)"

    session_id = session_meta["session_id"]
    project_path = session_meta["project_path"]
    created_date = session_meta.get("created_date", "unknown")
    update_date = session_meta.get("mtime_date", "unknown")

    sections = [
        "请分析以下 Claude Code 会话记录，生成结构化的工作日志文档。",
        "",
        "## 会话信息",
        f"- Session ID: {session_id}",
        f"- 项目路径: {project_path}",
        f"- 创建日期: {created_date}",
        f"- 最后更新: {update_date}",
        "",
    ]

    if is_update and existing_doc_content:
        sections.extend([
            "## 说明：这是增量更新",
            "以下是**自上次处理后新增的对话内容**。请基于已有文档和新增内容，生成一份完整的更新版文档。",
            "保持结构与已有文档一致，融合新旧内容。",
            "",
            "### 已有文档（参考）",
            existing_doc_content[-2000:] if len(existing_doc_content) > 2000 else existing_doc_content,
            "",
            "### 新增对话内容",
            truncated,
        ])
    else:
        sections.append("## 对话内容")
        sections.append(truncated)

    # 分类建议
    if existing_categories:
        cats_str = ", ".join(existing_categories)
        sections.append("")
        sections.append(f"## 备注：已有分类目录")
        sections.append(f"输出目录中已有以下分类: {cats_str}")
        sections.append("请优先从已有分类中选择 category，若无匹配则创建新分类。分类名只能是一级（不含 /）。")

    sections.extend([
        "",
        "## 输出要求",
        "请以 JSON 格式输出（**仅返回 JSON，不要 markdown 代码块，不要其他文字**），包含以下字段：",
        "",
        "- `title`: 描述性标题（简洁、有意义，保留原始语言）",
        "- `tags`: 3-5 个短标签（中英文均可，便于搜索）",
        "- `category`: 主题分类名（优先从已有分类中选择，无法判断时用 `未分类`）",
        "- `overview`: 工作概览——做了哪几项工作（简洁列表，每个工作项一句话）",
        "- `complex_work`: 高复杂度工作列表，每项含 `topic`（主题）、`problem`（问题）、`solution`（方案）、`key_decisions`（关键决策列表）",
        "- `multi_turn`: 多轮交互分析列表，每项含 `topic`（主题）、`rounds`（大约轮次）、`reason`（多轮才解决的原因）、`suggestions`（可能的解决思路列表）",
        "- `best_practices`: 可沉淀的最佳实践列表（字符串数组）",
        "- `notes`: 其他值得记录的信息（如有）",
        "",
        "对于没有足够信息可填的字段，返回空数组或空字符串。",
    ])

    return "\n".join(sections)


def call_llm(prompt: str, config: dict, label: str = "") -> dict:
    """通过 LiteLLM 调用 LLM，返回解析后的 JSON。"""
    model = config["model"]
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

    # 尝试提取 JSON（可能被 markdown 代码块包裹）
    json_match = re.search(r"\{[\s\S]*\}", text)
    if json_match:
        return json.loads(json_match.group())
    return json.loads(text)


def _ensure_tags_include_serious(tags: list[str]) -> list[str]:
    """确保 tags 中包含 '严肃工作'（去重）。"""
    if "严肃工作" not in tags:
        tags.append("严肃工作")
    return tags


def _determine_category(tags: list[str], project_path: str, serious_paths: list[str]) -> str:
    """根据标签和配置确定分类。"""
    # 检查是否匹配严肃工作路径
    expanded_project = os.path.expanduser(project_path)
    for sp in serious_paths:
        expanded_sp = os.path.expanduser(sp)
        if expanded_project.startswith(expanded_sp):
            return "严肃工作"
    # 从 tags 推断分类（由 LLM 在 metadata 中给出，这里由 LLM 输出决定）
    return ""  # 返回空表示由 LLM 输出中的 category 决定


def _build_document_content(llm_result: dict, session_meta: dict, processing_time: str) -> str:
    """根据 LLM 返回结果构造 markdown 文档正文。"""
    title = llm_result.get("title", "Untitled")
    session_id = session_meta["session_id"]
    project_path = session_meta["project_path"]
    update_date = session_meta["mtime_date"]

    parts = [
        f"# {title}",
        "",
        f"**Session ID**: `{session_id}`",
        f"**项目路径**: `{project_path}`",
        f"**最后更新**: {update_date}",
        f"**处理时间**: {processing_time}",
        "",
        "## 1. 工作概览",
        "",
    ]

    overview = llm_result.get("overview", [])
    if isinstance(overview, str):
        parts.append(overview)
    elif overview:
        for item in overview:
            parts.append(f"- {item}")
    else:
        parts.append("（无记录）")

    parts.extend(["", "## 2. 高复杂度工作", ""])
    complex_work = llm_result.get("complex_work", [])
    if complex_work:
        for cw in complex_work:
            topic = cw.get("topic", "未命名")
            problem = cw.get("problem", "")
            solution = cw.get("solution", "")
            key_decisions = cw.get("key_decisions", [])
            parts.append(f"### {topic}")
            parts.append(f"- **问题**：{problem}")
            parts.append(f"- **方案**：{solution}")
            if key_decisions:
                parts.append("- **关键决策**：")
                for kd in key_decisions:
                    parts.append(f"  - {kd}")
            parts.append("")
    else:
        parts.append("（无记录）")

    parts.extend(["## 3. 多轮交互分析", ""])
    multi_turn = llm_result.get("multi_turn", [])
    if multi_turn:
        for mt in multi_turn:
            topic = mt.get("topic", "未命名")
            rounds = mt.get("rounds", "?")
            reason = mt.get("reason", "")
            suggestions = mt.get("suggestions", [])
            parts.append(f"### {topic}")
            parts.append(f"- **交互轮次**：约 {rounds} 轮")
            parts.append(f"- **原因分析**：{reason}")
            if suggestions:
                parts.append("- **解决思路**：")
                for s in suggestions:
                    parts.append(f"  - {s}")
            parts.append("")
    else:
        parts.append("（无记录）")

    parts.extend(["## 4. 最佳实践", ""])
    best_practices = llm_result.get("best_practices", [])
    if best_practices:
        for bp in best_practices:
            parts.append(f"- {bp}")
    else:
        parts.append("（无记录）")

    notes = llm_result.get("notes", "")
    if notes:
        parts.extend(["", "## 5. 其他备注", "", notes])

    return "\n".join(parts) + "\n"


def _get_chunk_summary_prompt(transcript_chunk: str, chunk_idx: int, total_chunks: int) -> str:
    """构造分块摘要 prompt。"""
    return f"""请分析以下会话记录片段（第 {chunk_idx + 1}/{total_chunks} 块），提取结构化信息。

只返回 JSON（不含 markdown 代码块），格式：
{{"overview": ["工作项1", ...], "complex_work": [{{"topic": "", "problem": "", "solution": "", "key_decisions": []}}], "multi_turn": [{{"topic": "", "rounds": 0, "reason": "", "suggestions": []}}], "best_practices": [], "notes": ""}}

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
- `title`: 描述性标题
- `tags`: 3-5 个标签
- `category`: 主题分类名
- `overview`: 工作概览
- `complex_work`: 高复杂度工作
- `multi_turn`: 多轮交互分析
- `best_practices`: 最佳实践
- `notes`: 其他备注

去掉各片段间的重复内容，合并同类项。"""

    return call_llm(prompt, config)


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

    返回生成的文档路径（dry_run 模式返回 None）。
    """
    sid = session["session_id"]
    project_path = session["project_path"]
    jsonl_path = session.get("jsonl_path")
    mtime = session["mtime"]
    mtime_date = session["mtime_date"]
    created_date = session["created_date"]
    output_dir = config["output_dir"]
    serious_paths = config.get("serious_work_paths", [])

    # 1. 查找已有文档
    existing_doc_path = find_existing_document(output_dir, sid)
    existing_doc_content = None
    last_processed_ts = None
    if existing_doc_path:
        try:
            content = existing_doc_path.read_text(encoding="utf-8")
            fm = parse_frontmatter(content)
            if fm:
                last_processed_ts = fm.get("last_processed_timestamp")
            existing_doc_content = content
        except Exception:
            pass

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

    is_update = existing_doc_content is not None

    if dry_run:
        logger.info("  DRY-RUN %s (%d chars)", sid[:20], len(transcript))
        return None

    # 4. 调用 LLM 生成文档
    try:
        max_chars = config.get("max_chunk_chars", 8000)
        if len(transcript) <= max_chars:
            # 直接调用
            prompt = build_summary_prompt(
                transcript, session,
                existing_doc_content, existing_cats, is_update,
            )
            llm_result = call_llm(prompt, config)
        else:
            # 分块处理
            logger.debug("  分块处理 (%d chars > %d)", len(transcript), max_chars)
            chunk_size = max(1000, max_chars - 2000)
            overlap = 1000
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

    # 检查是否匹配严肃工作路径
    is_serious = False
    expanded_project = os.path.expanduser(project_path)
    for sp in serious_paths:
        expanded_sp = os.path.expanduser(sp)
        if expanded_project.startswith(expanded_sp):
            is_serious = True
            break

    if is_serious:
        tags = _ensure_tags_include_serious(tags)

    category = llm_result.get("category", "未分类")
    if is_serious:
        category = "严肃工作"
    if not category or "/" in str(category):
        category = "未分类"

    # 6. 计算 last_processed_timestamp
    last_ts = _compute_last_processed_ts(transcript, jsonl_path)
    if last_ts is None:
        last_ts = mtime

    # 7. 构造文档
    title = llm_result.get("title", "Untitled")
    processing_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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

    # 8. 写入文件
    # 如果已有文档且路径不同（文件名变了），删除旧文件
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

    # 发现 session
    sessions = discover_sessions(config["session_dirs"])
    logger.info("发现 %d 个待处理 session（排除当天修改）", len(sessions))

    if args.session_id:
        sessions = [s for s in sessions if s["session_id"] == args.session_id]
        if not sessions:
            logger.error("未找到 session: %s", args.session_id)
            sys.exit(1)
        logger.info("筛选指定 session: %s", args.session_id)

    # 处理
    processed = 0
    skipped = 0
    errors = 0

    for session in sessions:
        sid = session["session_id"]
        short_id = sid[:20] if len(sid) > 20 else sid
        logger.info("[%s] %s", session["mtime_date"], short_id)

        try:
            result = process_session(session, config, dry_run=args.dry_run)
            if result:
                processed += 1
            else:
                skipped += 1
        except Exception:
            logger.error("[%s] 处理出错", short_id, exc_info=True)
            errors += 1

    logger.info("")
    logger.info("=== 汇总 ===")
    logger.info("已处理: %d", processed)
    logger.info("已跳过: %d", skipped)
    logger.info("错误:   %d", errors)
    if args.dry_run:
        logger.info("[预览模式] 未实际调用 LLM。")


if __name__ == "__main__":
    main()

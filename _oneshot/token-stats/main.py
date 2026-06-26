#!/usr/bin/env python3
"""token-stats: 静态分析 Claude Code session 文件，按模型统计 token 消耗并生成 HTML 报告。

纯数据解析，不调用 LLM。
"""

import os
import sys
import json
import time
import argparse
import logging
import urllib.request
import urllib.error
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime, date, timedelta
from collections import defaultdict
from typing import Optional

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 路径 & 日志
# ---------------------------------------------------------------------------

TOOL_DIR = Path(__file__).resolve().parent

logger: logging.Logger = logging.getLogger("token-stats")
logger.addHandler(logging.NullHandler())


def setup_logging(backup_count: int = 7) -> logging.Logger:
    log_dir = TOOL_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    _logger = logging.getLogger("token-stats")
    _logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(ch)

    fh = TimedRotatingFileHandler(
        filename=log_dir / "run.log",
        when="midnight", interval=1, backupCount=backup_count, encoding="utf-8",
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    _logger.addHandler(fh)

    dh = TimedRotatingFileHandler(
        filename=log_dir / "debug.log",
        when="midnight", interval=1, backupCount=backup_count, encoding="utf-8",
    )
    dh.setLevel(logging.DEBUG)
    dh.setFormatter(fmt)
    _logger.addHandler(dh)

    return _logger


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def load_worker_config() -> dict:
    worker_yaml = TOOL_DIR / "worker.yaml"
    if worker_yaml.exists():
        with open(worker_yaml, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _load_cached_rate() -> Optional[dict]:
    """读取缓存的汇率。返回 {"rate": float, "ts": int} 或 None。"""
    cache_path = TOOL_DIR / ".rate_cache.json"
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if "rate" in data and "ts" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _save_cached_rate(rate: float):
    """缓存汇率到文件。"""
    cache_path = TOOL_DIR / ".rate_cache.json"
    cache_path.write_text(json.dumps({
        "rate": rate,
        "ts": int(time.time()),
        "updated": datetime.now().isoformat(),
    }), encoding="utf-8")


def fetch_usd_cny_rate(ttl: int = 14400) -> tuple[float, str]:
    """获取实时 USD→CNY 汇率，优先读缓存，过期后调免费 API 刷新。

    Returns:
        (rate, source) — rate 为 1 USD 兑换多少 CNY，source 为来源标识。
    """
    # 1. 尝试读缓存
    cached = _load_cached_rate()
    if cached and (int(time.time()) - cached["ts"]) < ttl:
        logger.debug("使用缓存汇率: %.4f (更新于 %s)",
                     cached["rate"], cached.get("updated", "?"))
        return cached["rate"], "cache"

    # 2. 调免费 API（open.er-api.com，无需 API Key）
    api_url = "https://open.er-api.com/v6/latest/USD"
    try:
        req = urllib.request.Request(api_url)
        req.add_header("User-Agent", "token-stats/1.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rate = float(data["rates"]["CNY"])
        _save_cached_rate(rate)
        logger.info("实时汇率: 1 USD = %.4f CNY (来源: open.er-api.com)", rate)
        return rate, "open.er-api.com"
    except Exception as e:
        logger.warning("获取实时汇率失败: %s", e)
        # 3. 降级：使用过期缓存或硬编码汇率
        if cached:
            logger.warning("降级使用过期缓存汇率: %.4f", cached["rate"])
            return cached["rate"], "stale-cache"
        fallback = 7.30
        logger.warning("降级使用默认汇率: %.4f", fallback)
        return fallback, "fallback"


def get_config() -> dict:
    load_dotenv(TOOL_DIR / ".env")
    worker_cfg = load_worker_config()

    session_dirs_str = os.environ.get(
        "SESSION_DIRS", os.path.expanduser("~/.claude/projects")
    )
    session_dirs = [
        Path(os.path.expanduser(p.strip()))
        for p in session_dirs_str.split(",") if p.strip()
    ]

    session_meta_dir = Path(os.path.expanduser(
        os.environ.get("SESSION_META_DIR", "~/.claude/sessions")
    ))

    # 实时汇率
    ttl = int(worker_cfg.get("rate_cache_ttl", 14400))
    usd_cny_rate, rate_source = fetch_usd_cny_rate(ttl)

    # 加载 model_pricing（按 currency 字段区分币种）
    model_pricing: dict[str, dict] = {}       # 统一 CNY 价（用于费用计算）
    model_pricing_display: dict[str, dict] = {}  # 原生币种价（用于计费表展示）
    pricing_cfg = worker_cfg.get("model_pricing", {})
    if isinstance(pricing_cfg, dict):
        for prefix, prices in pricing_cfg.items():
            if not isinstance(prices, dict):
                continue
            currency = prices.get("currency", "cny")
            if "input" not in prices or "output" not in prices:
                continue

            in_val = float(prices["input"])
            out_val = float(prices["output"])
            hit_val = float(prices["cache_hit_input"]) if "cache_hit_input" in prices else None

            if currency == "usd":
                # USD 计价 → 按实时汇率转 CNY
                entry_cny = {
                    "input": in_val * usd_cny_rate,
                    "output": out_val * usd_cny_rate,
                }
                if hit_val is not None:
                    entry_cny["cache_hit_input"] = hit_val * usd_cny_rate
                if "cache_write_input" in prices:
                    entry_cny["cache_write_input"] = float(prices["cache_write_input"]) * usd_cny_rate
                model_pricing[prefix] = entry_cny

                entry_display = {
                    "currency": "usd", "input": in_val, "output": out_val,
                }
                if hit_val is not None:
                    entry_display["cache_hit_input"] = hit_val
                if "cache_write_input" in prices:
                    entry_display["cache_write_input"] = float(prices["cache_write_input"])
                model_pricing_display[prefix] = entry_display
            else:
                # CNY 原生计价
                entry_cny = {"input": in_val, "output": out_val}
                if hit_val is not None:
                    entry_cny["cache_hit_input"] = hit_val
                if "cache_write_input" in prices:
                    entry_cny["cache_write_input"] = float(prices["cache_write_input"])
                model_pricing[prefix] = entry_cny

                entry_display = {
                    "currency": "cny", "input": in_val, "output": out_val,
                }
                if hit_val is not None:
                    entry_display["cache_hit_input"] = hit_val
                if "cache_write_input" in prices:
                    entry_display["cache_write_input"] = float(prices["cache_write_input"])
                model_pricing_display[prefix] = entry_display

    return {
        "session_dirs": session_dirs,
        "session_meta_dir": session_meta_dir,
        "model_pricing": model_pricing,
        "model_pricing_display": model_pricing_display,
        "usd_cny_rate": usd_cny_rate,
        "rate_source": rate_source,
        "log_retention_days": worker_cfg.get("log_retention_days", 7),
    }


# ---------------------------------------------------------------------------
# Session 文件扫描 & 名称映射
# ---------------------------------------------------------------------------

def find_session_files(dirs: list[Path]) -> list[Path]:
    """扫描目录下所有 *.jsonl 文件。"""
    files: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            logger.debug("跳过不存在的目录: %s", d)
            continue
        for f in sorted(d.rglob("*.jsonl")):
            # 跳过子 agent 的 session（在 subagents/ 子目录中）
            if "subagents" in f.parts:
                continue
            if str(f) not in seen:
                seen.add(str(f))
                files.append(f)
    return files


def load_session_names(meta_dir: Path) -> dict[str, dict]:
    """从 session 元数据 JSON 文件读取 sessionId → {name, cwd, pid} 映射。"""
    names: dict[str, dict] = {}
    if not meta_dir.is_dir():
        return names
    for f in sorted(meta_dir.glob("*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            sid = d.get("sessionId")
            if sid:
                names[sid] = {
                    "name": d.get("name", ""),
                    "cwd": d.get("cwd", ""),
                    "pid": d.get("pid", 0),
                }
        except Exception:
            logger.debug("跳过无法解析的元数据文件: %s", f)
    return names


# ---------------------------------------------------------------------------
# 核心解析
# ---------------------------------------------------------------------------

def parse_sessions(
    files: list[Path],
    date_from: date,
    date_to: date,
    session_names: dict[str, dict],
    model_pricing: dict[str, dict],
) -> dict:
    """解析所有 session JSONL 文件，聚合 token 统计。

    Returns:
        {
            "model_stats": {model: {input_tokens, output_tokens, cache_read, cache_create,
                                    total_tokens, message_count, estimated_cost}},
            "daily_stats": {hour_str: {model: total_tokens}},  # YYYY-MM-DDTHH
            "project_stats": {project: {models: {model: {input, output, total, ...}},
                                         sessions: [{id, name, models: {...}}],
                                         total_tokens, estimated_cost}},
            "session_list": [{id, name, project, model_stats: {...}, total_tokens, message_count, estimated_cost}],
            "unmatched_session_count": int,
            "total_session_count": int,
        }
    """
    model_stats: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    daily_stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    project_stats: dict[str, dict] = defaultdict(
        lambda: {"models": defaultdict(lambda: defaultdict(int)), "sessions": []}
    )
    session_list: list[dict] = []

    total_count = 0
    processed_count = 0
    unmatched_count = 0

    for filepath in files:
        total_count += 1
        sid = filepath.stem  # session ID from filename
        meta = session_names.get(sid, {})
        session_name = meta.get("name", "")
        project = _extract_project_name(filepath)

        session_model_stats: dict[str, dict] = defaultdict(lambda: defaultdict(int))
        session_msg_count = 0

        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if entry.get("type") != "assistant":
                        continue

                    # 日期过滤
                    ts_str = entry.get("timestamp", "")
                    if not ts_str:
                        continue
                    try:
                        msg_date = _parse_timestamp_date(ts_str)
                    except (ValueError, IndexError):
                        continue

                    if msg_date < date_from or msg_date > date_to:
                        continue

                    # 提取 token 数据
                    model = entry.get("message", {}).get("model", "unknown")
                    usage = entry.get("message", {}).get("usage", {})
                    if not usage:
                        continue

                    input_t = usage.get("input_tokens", 0) or 0
                    output_t = usage.get("output_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_create = usage.get("cache_creation_input_tokens", 0) or 0

                    # 跳过无实际 token 消耗的消息（如 <synthetic> 等系统内部消息）
                    if input_t == 0 and output_t == 0:
                        continue

                    # 聚合到全局 model_stats
                    ms = model_stats[model]
                    ms["input_tokens"] += input_t
                    ms["output_tokens"] += output_t
                    ms["cache_read_input_tokens"] += cache_read
                    ms["cache_creation_input_tokens"] += cache_create
                    ms["total_tokens"] += input_t + output_t
                    ms["message_count"] += 1

                    # 聚合到 session 级别
                    sms = session_model_stats[model]
                    sms["input_tokens"] += input_t
                    sms["output_tokens"] += output_t
                    sms["cache_read_input_tokens"] += cache_read
                    sms["cache_creation_input_tokens"] += cache_create
                    sms["total_tokens"] += input_t + output_t
                    sms["message_count"] += 1

                    session_msg_count += 1

                    # 小时级统计 (YYYY-MM-DDTHH)
                    hour_key = ts_str[:13]
                    daily_stats[hour_key][model] += input_t + output_t

            if session_msg_count > 0:
                processed_count += 1
                session_total = sum(s["total_tokens"] for s in session_model_stats.values())
                session_cost = _calc_session_cost(session_model_stats, model_pricing)
                session_list.append({
                    "id": sid,
                    "name": session_name or sid[:8],
                    "project": project,
                    "model_stats": dict(session_model_stats),
                    "total_tokens": session_total,
                    "message_count": session_msg_count,
                    "estimated_cost": session_cost,
                })

                # 聚合到 project_stats
                ps = project_stats[project]
                for m, s in session_model_stats.items():
                    for k, v in s.items():
                        ps["models"][m][k] += v
                ps["sessions"].append({
                    "id": sid,
                    "name": session_name or sid[:8],
                    "total_tokens": session_total,
                    "estimated_cost": session_cost,
                })
            else:
                if processed_count == 0:
                    unmatched_count += 1

        except Exception:
            logger.debug("解析 session 文件失败: %s", filepath, exc_info=True)
            unmatched_count += 1

    # 初始化 project_stats 的 total 字段
    for pname, pdata in project_stats.items():
        pdata["total_tokens"] = sum(
            m["total_tokens"] for m in pdata["models"].values()
        )
        pdata["estimated_cost"] = sum(
            s["estimated_cost"] for s in pdata["sessions"]
        )

    return {
        "model_stats": dict(model_stats),
        "daily_stats": dict(daily_stats),
        "project_stats": dict(project_stats),
        "session_list": session_list,
        "processed_session_count": processed_count,
        "total_session_count": total_count,
    }


def _extract_project_name(filepath: Path) -> str:
    """从文件路径提取项目名（保持编码形式，- 充当路径分隔符）。

    session 文件路径格式:
      ~/.claude/projects/-Users-name-proj-xxx/<session-id>.jsonl

    项目目录名是把原始绝对路径的 / 替换为 - 得到的。
    由于原始路径可能含 -，解码不可逆，故保留编码形式。
    例如 /Users/duankaiqiang/proj/my-llm-workers →
         -Users-duankaiqiang-proj-my-llm-workers
    """
    raw = filepath.parent.name
    if raw.startswith("-"):
        raw = raw[1:]
    return raw


def _strip_common_prefix(names: list[str]) -> dict[str, str]:
    """去掉项目名的公共前缀，返回 {原始名: 显示名} 映射。

    所有项目名如 Users-duankaiqiang-proj-foo → 去掉公共 Users-duankaiqiang- 后得 proj-foo。
    若去前缀后为空，退化为最后 2 段。
    """
    if not names:
        return {}
    parts_list = [n.split("-") for n in names]
    min_len = min(len(p) for p in parts_list)
    common_end = 0
    for i in range(min_len):
        first = parts_list[0][i]
        if all(p[i] == first for p in parts_list):
            common_end = i + 1
        else:
            break
    # 对每个名：去前缀后至少保留 2 段
    result = {}
    for n, parts in zip(names, parts_list):
        keep_start = min(common_end, max(0, len(parts) - 2))
        result[n] = "-".join(parts[keep_start:])
    return result


def _parse_timestamp_date(ts: str) -> date:
    """解析 ISO 8601 时间戳为 date 对象。"""
    # 格式: 2026-06-24T14:05:20.970Z
    return date.fromisoformat(ts[:10])


# ---------------------------------------------------------------------------
# 费用估算
# ---------------------------------------------------------------------------

def estimate_cost(
    model_stats: dict[str, dict],
    model_pricing: dict[str, dict],
) -> float:
    """计算全局总费用。"""
    total = 0.0
    for model, stats in model_stats.items():
        price = _find_price(model, model_pricing)
        if price is None:
            continue
        total += _calc_model_cost(stats, price)
    return total


def _calc_session_cost(
    session_model_stats: dict[str, dict],
    model_pricing: dict[str, dict],
) -> float:
    """计算单个 session 的总费用。"""
    total = 0.0
    for model, stats in session_model_stats.items():
        price = _find_price(model, model_pricing)
        if price is None:
            continue
        total += _calc_model_cost(stats, price)
    return total


def _calc_model_cost(stats: dict, price: dict) -> float:
    """计算单模型费用。

    session 中的 input_tokens 已排除 cache_read（未计费），
    因此 input_tokens 按输入单价、cache_read 按缓存命中单价分别计费。
    cache_creation 按基础输入单价计费（等同 cache miss）。
    """
    input_tokens = stats.get("input_tokens", 0)
    output_tokens = stats.get("output_tokens", 0)
    cache_read = stats.get("cache_read_input_tokens", 0)
    cache_create = stats.get("cache_creation_input_tokens", 0)

    cost = 0.0
    # input token 按基础输入单价
    cost += (input_tokens / 1_000_000) * price["input"]
    # cache write 按独立单价（如有），否则按输入单价
    cw_price = price.get("cache_write_input", price["input"])
    cost += (cache_create / 1_000_000) * cw_price
    cost += (output_tokens / 1_000_000) * price["output"]

    # 缓存命中：按低单价计费（如配置了独立缓存命中价）
    cache_hit_price = price.get("cache_hit_input")
    if cache_read > 0 and cache_hit_price is not None:
        cost += (cache_read / 1_000_000) * cache_hit_price
    else:
        cost += (cache_read / 1_000_000) * price["input"]

    return cost


def _find_price(model: str, model_pricing: dict[str, dict]) -> Optional[dict]:
    """前缀匹配查找模型定价。"""
    for prefix, price in model_pricing.items():
        if model.startswith(prefix):
            return price
    return None


# ---------------------------------------------------------------------------
# HTML 报告生成
# ---------------------------------------------------------------------------

def generate_html(stats: dict, date_from: date, date_to: date,
                  model_pricing: dict[str, dict],
                  model_pricing_display: dict[str, dict] | None = None,
                  usd_cny_rate: float = 0.0,
                  rate_source: str = "") -> str:
    """生成自包含的 HTML 报告。"""
    model_stats = stats["model_stats"]
    daily_stats = stats["daily_stats"]
    project_stats = stats["project_stats"]

    # 计算全局汇总
    total_input = sum(m["input_tokens"] for m in model_stats.values())
    total_output = sum(m["output_tokens"] for m in model_stats.values())
    total_cache_read = sum(m["cache_read_input_tokens"] for m in model_stats.values())
    total_cache_create = sum(m["cache_creation_input_tokens"] for m in model_stats.values())
    total_tokens = total_input + total_output
    total_messages = sum(m["message_count"] for m in model_stats.values())
    total_cost = estimate_cost(model_stats, model_pricing)

    # 准备 Chart.js 数据
    pie_labels = json.dumps(list(model_stats.keys()))
    pie_data = json.dumps([m["total_tokens"] for m in model_stats.values()])

    # 每日趋势数据
    sorted_dates = sorted(daily_stats.keys())
    all_models_for_daily = sorted(set(
        m for d in daily_stats.values() for m in d.keys()
    ))
    daily_datasets = []
    colors = _model_colors(all_models_for_daily)
    for model in all_models_for_daily:
        values = [daily_stats.get(d, {}).get(model, 0) for d in sorted_dates]
        daily_datasets.append({
            "label": model,
            "data": values,
            "borderColor": colors.get(model, "#999"),
            "backgroundColor": colors.get(model, "#999") + "20",
            "fill": False,
            "tension": 0.1,
        })

    # 项目名去公共前缀
    proj_name_map = _strip_common_prefix(list(project_stats.keys()))

    # 模型概览表
    model_rows = _build_model_rows(model_stats, model_pricing,
                                   model_pricing_display or {}, usd_cny_rate)
    # 计费表
    pricing_rows = _build_pricing_rows(model_pricing_display or {}, usd_cny_rate)
    # 按项目统计 Tab
    project_tabs = _build_project_tabs(project_stats, model_pricing,
                                       model_pricing_display or {}, usd_cny_rate,
                                       proj_name_map)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Token 消耗报告 — {date_from} ~ {date_to}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #f5f7fa; color: #333; line-height: 1.6; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
h1 {{ text-align: center; margin: 20px 0 10px; color: #1a1a2e; }}
.subtitle {{ text-align: center; color: #666; margin-bottom: 30px; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
         gap: 16px; margin-bottom: 30px; }}
.card {{ background: #fff; border-radius: 12px; padding: 20px;
        box-shadow: 0 2px 8px rgba(0,0,0,.06); text-align: center; }}
.card .value {{ font-size: 28px; font-weight: 700; color: #1a1a2e; }}
.card .label {{ font-size: 13px; color: #888; margin-top: 4px; }}
.charts {{ display: grid; grid-template-columns: 1fr 2fr; gap: 20px; margin-bottom: 30px; }}
@media (max-width: 768px) {{ .charts {{ grid-template-columns: 1fr; }} }}
.chart-box {{ background: #fff; border-radius: 12px; padding: 20px;
             box-shadow: 0 2px 8px rgba(0,0,0,.06); }}
.chart-box.full {{ grid-column: 1 / -1; }}
.chart-box h3 {{ margin-bottom: 16px; color: #555; font-size: 15px; }}
.section {{ background: #fff; border-radius: 12px; padding: 24px;
           box-shadow: 0 2px 8px rgba(0,0,0,.06); margin-bottom: 20px; }}
.section h2 {{ margin-bottom: 16px; color: #1a1a2e; font-size: 18px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ padding: 10px 12px; text-align: right; border-bottom: 1px solid #eee; }}
th:first-child, td:first-child {{ text-align: left; }}
th {{ background: #f8f9fc; color: #666; font-weight: 600; white-space: nowrap; }}
tr:hover {{ background: #f8f9ff; }}
.num {{ font-variant-numeric: tabular-nums; }}
.cost-positive {{ color: #e74c3c; }}
footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 30px; padding: 20px; }}
.model-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
               font-size: 12px; font-weight: 600; }}
.proj-tab-bar {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
.proj-tab-btn {{ padding: 8px 16px; border: 1px solid #ddd; border-radius: 8px;
                background: #fff; cursor: pointer; font-size: 13px; white-space: nowrap; }}
.proj-tab-btn.active {{ background: #667eea; color: #fff; border-color: #667eea; }}
.proj-tab-btn.active span {{ color: rgba(255,255,255,.7) !important; }}
.proj-tab-btn:hover:not(.active) {{ background: #f0f0ff; }}
.proj-panel table {{ margin-top: 0; }}
</style>
</head>
<body>
<div class="container">
<h1>🔢 Token 消耗报告</h1>
<p class="subtitle">{date_from} ~ {date_to} · 共 {stats['processed_session_count']} 个活跃 session<br>
<small style="color:#999">总 Token = 计费 Input + Output（缓存命中不计入总 Token，但按低单价计费）</small></p>

<div class="cards">
  <div class="card">
    <div class="value">{_fmt_num(total_tokens)}</div>
    <div class="label">总 Token 数</div>
  </div>
  <div class="card">
    <div class="value">{_fmt_num(total_input)}</div>
    <div class="label">Input Tokens</div>
  </div>
  <div class="card">
    <div class="value">{_fmt_num(total_output)}</div>
    <div class="label">Output Tokens</div>
  </div>
  <div class="card">
    <div class="value">{_fmt_num(total_cache_read)}</div>
    <div class="label">💾 缓存命中（节省）</div>
  </div>
  <div class="card">
    <div class="value">{total_messages:,}</div>
    <div class="label">LLM 调用次数</div>
  </div>
  <div class="card">
    <div class="value">¥{total_cost:.2f}</div>
    <div class="label">预估费用</div>
  </div>
</div>

<div class="charts">
  <div class="chart-box">
    <h3>模型 Token 占比</h3>
    <canvas id="pieChart"></canvas>
  </div>
  <div class="chart-box">
    <h3>小时 Token 趋势</h3>
    <canvas id="dailyChart"></canvas>
  </div>
</div>

<div class="section">
  <h2>📊 模型明细</h2>
  <table>
    <thead><tr>
      <th>模型</th><th>调用次数</th><th>Input</th><th>Output</th>
      <th>Cache 命中</th><th>Cache 写入</th><th>总 Token</th><th>预估费用</th>
    </tr></thead>
    <tbody>{model_rows}</tbody>
  </table>
</div>

<div class="section">
  <h2>💰 计费表</h2>
  <p style="color:#888;font-size:13px;margin-bottom:12px">
    实时汇率: 1 USD = {usd_cny_rate:.4f} CNY · 来源: {rate_source} · USD 模型显示 $ 原生价 + 等值 ¥
  </p>
  <table>
    <thead><tr>
      <th>模型</th><th>Input</th><th>Output</th><th>Cache 命中</th><th>Cache 写入</th>
      <th>Input (¥)</th><th>Output (¥)</th><th>Cache 命中 (¥)</th><th>Cache 写入 (¥)</th>
    </tr></thead>
    <tbody>{pricing_rows}</tbody>
  </table>
</div>

<div class="section">
  <h2>📁 按模型 · 项目分布（各 Top 10）</h2>
  {project_tabs}
</div>

<footer>
  Generated by token-stats · {datetime.now().strftime('%Y-%m-%d %H:%M')}
</footer>
</div>

<script>
// 模型占比饼图
new Chart(document.getElementById('pieChart'), {{
  type: 'doughnut',
  data: {{
    labels: {pie_labels},
    datasets: [{{
      data: {pie_data},
      backgroundColor: ['#667eea','#f093fb','#4facfe','#43e97b','#fa709a',
                        '#f6d365','#a18cd1','#f5576c','#36d1dc','#ff9a9e'],
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ padding: 20, usePointStyle: true }} }}
    }}
  }}
}});

// 小时趋势折线图
new Chart(document.getElementById('dailyChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(sorted_dates)},
    datasets: {json.dumps(daily_datasets)}
  }},
  options: {{
    responsive: true,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ usePointStyle: true }} }}
    }},
    scales: {{
      y: {{ beginAtZero: true, ticks: {{ callback: v => v >= 1000 ? (v/1000).toFixed(0)+'K' : v }} }}
    }},
    elements: {{ point: {{ radius: 0 }} }}
  }}
}});

// 项目 Tab 切换
function switchProjTab(idx) {{
  document.querySelectorAll('.proj-tab-btn').forEach((b, i) => b.classList.toggle('active', i === idx));
  document.querySelectorAll('.proj-panel').forEach((p, i) => p.style.display = i === idx ? 'block' : 'none');
}}
</script>
</body>
</html>"""


def _model_colors(models: list[str]) -> dict[str, str]:
    palette = [
        "#667eea", "#f093fb", "#4facfe", "#43e97b", "#fa709a",
        "#f6d365", "#a18cd1", "#f5576c", "#36d1dc", "#ff9a9e",
    ]
    return {m: palette[i % len(palette)] for i, m in enumerate(models)}


def _fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n/1_000:.0f}K"
    if n >= 1_000:
        return f"{n/1000:.1f}K"
    return str(n)


def _find_currency(model: str, display: dict[str, dict]) -> str:
    """查找模型的原生币种。"""
    price = _find_price(model, display)
    return price.get("currency", "cny") if price else "cny"


def _fmt_cost(cny_amount: float, model: str,
              display: dict[str, dict], rate: float) -> str:
    """格式化费用：USD 模型显示 $X (¥Y)，CNY 模型显示 ¥X。"""
    currency = _find_currency(model, display)
    if currency == "usd" and rate > 0:
        usd = cny_amount / rate
        return f'<td class="num cost-positive">${usd:.2f} <span style="color:#888;font-size:11px">(¥{cny_amount:.2f})</span></td>'
    return f'<td class="num cost-positive">¥{cny_amount:.2f}</td>'


def _build_model_rows(model_stats: dict, model_pricing: dict,
                      display: dict[str, dict], rate: float) -> str:
    rows = []
    sorted_models = sorted(model_stats.items(),
                           key=lambda x: x[1]["total_tokens"], reverse=True)
    for model, s in sorted_models:
        price = _find_price(model, model_pricing)
        cost = _calc_model_cost(s, price) if price else 0.0
        cost_cell = _fmt_cost(cost, model, display, rate) if price else '<td class="num">-</td>'
        rows.append(f"""<tr>
<td><strong>{model}</strong></td>
<td class="num">{s['message_count']:,}</td>
<td class="num">{_fmt_num(s['input_tokens'])}</td>
<td class="num">{_fmt_num(s['output_tokens'])}</td>
<td class="num">{_fmt_num(s['cache_read_input_tokens'])}</td>
<td class="num">{_fmt_num(s['cache_creation_input_tokens'])}</td>
<td class="num"><strong>{_fmt_num(s['total_tokens'])}</strong></td>
{cost_cell}
</tr>""")
    return "\n".join(rows)


def _build_pricing_rows(model_pricing_display: dict, usd_cny_rate: float) -> str:
    """生成计费表行：区分 CNY/USD 原生计价模型。

    CNY 模型显示 ¥ 价格；USD 模型同时显示 $ 原生价格和等值 ¥ 价格。
    """
    rows = []
    for prefix in sorted(model_pricing_display.keys()):
        d = model_pricing_display[prefix]
        currency = d.get("currency", "cny")
        in_val = d.get("input")
        out_val = d.get("output")
        hit_val = d.get("cache_hit_input")
        cw_val = d.get("cache_write_input")

        def _yuan(v: float | None) -> str:
            if v is None:
                return "-"
            if v < 0.01:
                return f"¥{v:.6f}".rstrip("0").rstrip(".")
            if v < 1:
                return f"¥{v:.4f}".rstrip("0").rstrip(".")
            return f"¥{v:.2f}"

        def _dollar(v: float | None) -> str:
            if v is None:
                return "-"
            return f"${v:.4f}".rstrip("0").rstrip(".")

        if currency == "usd":
            rows.append(f"""<tr>
<td><strong>{prefix}</strong> <span style="color:#888;font-size:11px">USD</span></td>
<td class="num">{_dollar(in_val)}</td>
<td class="num">{_dollar(out_val)}</td>
<td class="num">{_dollar(hit_val)}</td>
<td class="num">{_dollar(cw_val)}</td>
<td class="num">{_yuan(in_val * usd_cny_rate) if in_val is not None else "-"}</td>
<td class="num">{_yuan(out_val * usd_cny_rate) if out_val is not None else "-"}</td>
<td class="num">{_yuan(hit_val * usd_cny_rate) if hit_val is not None else "-"}</td>
<td class="num">{_yuan(cw_val * usd_cny_rate) if cw_val is not None else "-"}</td>
</tr>""")
        else:
            rows.append(f"""<tr>
<td><strong>{prefix}</strong> <span style="color:#888;font-size:11px">CNY</span></td>
<td class="num" colspan="4" style="color:#888">— 人民币原生计价 —</td>
<td class="num">{_yuan(in_val)}</td>
<td class="num">{_yuan(out_val)}</td>
<td class="num">{_yuan(hit_val)}</td>
<td class="num">{_yuan(cw_val)}</td>
</tr>""")
    return "\n".join(rows) if rows else '<tr><td colspan="7">无计费数据</td></tr>'


def _build_project_tabs(project_stats: dict, model_pricing: dict,
                        display: dict[str, dict], rate: float,
                        name_map: dict[str, str]) -> str:
    """生成按模型分 Tab，每个模型内展示 Top 10 项目明细。

    Tab = 模型名，内容 = 使用该模型的项目 token 统计。
    """
    # Pivot: {project: {model: stats}} → {model: {project: stats}}
    model_projects: dict[str, dict[str, dict]] = defaultdict(dict)
    for pname, pdata in project_stats.items():
        for model, s in pdata["models"].items():
            model_projects[model][pname] = s

    if not model_projects:
        return '<p>无数据</p>'

    # 按模型总 token 排序
    sorted_models = sorted(model_projects.items(),
                           key=lambda x: sum(s["total_tokens"] for s in x[1].values()),
                           reverse=True)

    tab_btns = []
    panels = []
    for mi, (model, projects) in enumerate(sorted_models):
        total = sum(s["total_tokens"] for s in projects.values())
        active = "active" if mi == 0 else ""
        tab_btns.append(
            f'<button class="proj-tab-btn {active}" onclick="switchProjTab({mi})">{model} '
            f'<span style="color:#888;font-size:11px">{_fmt_num(total)}</span></button>'
        )

        # Top 10 项目
        proj_rows = ""
        top_projects = sorted(projects.items(),
                              key=lambda x: x[1]["total_tokens"], reverse=True)[:10]
        for pname, s in top_projects:
            display_name = name_map.get(pname, pname)
            price = _find_price(model, model_pricing)
            cost = _calc_model_cost(s, price) if price else 0.0
            cost_cell = _fmt_cost(cost, model, display, rate) if price else '<td class="num">-</td>'
            proj_rows += f"""<tr>
<td title="{pname}">{display_name}</td>
<td class="num">{s['message_count']:,}</td>
<td class="num">{_fmt_num(s['input_tokens'])}</td>
<td class="num">{_fmt_num(s['output_tokens'])}</td>
<td class="num">{_fmt_num(s['cache_read_input_tokens'])}</td>
<td class="num">{_fmt_num(s['cache_creation_input_tokens'])}</td>
<td class="num"><strong>{_fmt_num(s['total_tokens'])}</strong></td>
{cost_cell}
</tr>"""

        panels.append(f"""<div class="proj-panel" id="proj-panel-{mi}" style="display:{'block' if mi==0 else 'none'}">
<table>
<thead><tr>
  <th>项目</th><th>调用</th><th>Input</th><th>Output</th><th>Cache 命中</th><th>Cache 写入</th><th>总 Token</th><th>费用</th>
</tr></thead>
<tbody>{proj_rows}</tbody>
</table>
</div>""")

    return f"""<div class="proj-tabs">
<div class="proj-tab-bar">{''.join(tab_btns)}</div>
{''.join(panels)}
</div>"""


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="静态分析 Claude Code session 文件，按模型统计 token 消耗并生成 HTML 报告"
    )
    today = date.today()
    default_from = today.replace(day=1)
    parser.add_argument(
        "--from", dest="date_from",
        default=default_from.isoformat(),
        help=f"开始日期 YYYY-MM-DD（默认: {default_from}）",
    )
    parser.add_argument(
        "--to", dest="date_to",
        default=today.isoformat(),
        help=f"结束日期 YYYY-MM-DD（默认: {today}）",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="HTML 输出路径（默认: token-stats/report.html）",
    )
    parser.add_argument(
        "--session-dirs",
        default=None,
        help="Session 文件搜索路径，逗号分隔（默认: ~/.claude/projects）",
    )

    args = parser.parse_args()

    # 加载配置
    worker_cfg = load_worker_config()
    log_retention = worker_cfg.get("log_retention_days", 7)
    global logger
    logger = setup_logging(backup_count=log_retention)

    config = get_config()

    # 解析日期
    date_from = date.fromisoformat(args.date_from)
    date_to = date.fromisoformat(args.date_to)
    if date_from > date_to:
        logger.error("日期范围错误: --from 不能晚于 --to")
        sys.exit(1)

    # session 目录可被 CLI 覆盖
    if args.session_dirs:
        config["session_dirs"] = [
            Path(os.path.expanduser(p.strip()))
            for p in args.session_dirs.split(",") if p.strip()
        ]

    output_path = Path(args.output) if args.output else (TOOL_DIR / "report.html")

    logger.info("=" * 60)
    logger.info("token-stats: %s ~ %s", date_from, date_to)
    logger.info("搜索目录: %s", ", ".join(str(d) for d in config["session_dirs"]))

    # 扫描文件
    files = find_session_files(config["session_dirs"])
    logger.info("发现 %d 个 session 文件", len(files))

    # 加载 session 名称映射
    session_names = load_session_names(config["session_meta_dir"])
    logger.info("加载 %d 个 session 名称映射", len(session_names))

    # 解析
    stats = parse_sessions(files, date_from, date_to, session_names, config["model_pricing"])
    logger.info("处理 %d/%d 个 session（有消息的）",
                stats["processed_session_count"], stats["total_session_count"])

    # 打印摘要
    total_tokens = sum(m["total_tokens"] for m in stats["model_stats"].values())
    total_msgs = sum(m["message_count"] for m in stats["model_stats"].values())
    total_cost = estimate_cost(stats["model_stats"], config["model_pricing"])
    logger.info("总计: %s tokens, %s 次 LLM 调用, 预估 ¥%.2f",
                f"{total_tokens:,}", f"{total_msgs:,}", total_cost)
    logger.info("模型分布:")
    for model, s in sorted(stats["model_stats"].items(),
                           key=lambda x: x[1]["total_tokens"], reverse=True):
        price = _find_price(model, config["model_pricing"])
        cost_str = ""
        if price:
            cost = _calc_model_cost(s, price)
            cost_str = f" ¥{cost:.2f}"
        logger.info("  %s: %s tokens (%s calls)%s",
                    model, f"{s['total_tokens']:,}", f"{s['message_count']:,}", cost_str)

    # 生成 HTML
    html = generate_html(stats, date_from, date_to, config["model_pricing"],
                         config.get("model_pricing_display", {}),
                         config["usd_cny_rate"],
                         config["rate_source"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info("汇率: 1 USD = %.4f CNY (来源: %s)", config["usd_cny_rate"], config["rate_source"])
    logger.info("报告已生成: %s", output_path)

    # 输出中显示 report 路径
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.critical("未捕获的异常导致退出", exc_info=True)
        sys.exit(1)

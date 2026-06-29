"""token-stats 单元测试。"""

import json
import sys
import tempfile
from pathlib import Path
from datetime import date

import pytest

# 将 token-stats 目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import main  # noqa: E402


# ---------------------------------------------------------------------------
# _parse_timestamp_date
# ---------------------------------------------------------------------------

def test_parse_timestamp_date():
    assert main._parse_timestamp_date("2026-06-24T14:05:20.970Z") == date(2026, 6, 24)
    assert main._parse_timestamp_date("2026-01-01T00:00:00.000Z") == date(2026, 1, 1)


# ---------------------------------------------------------------------------
# _extract_project_name
# ---------------------------------------------------------------------------

def test_extract_project_name():
    # 编码: / → -，前缀 -
    # /Users/duankaiqiang/proj/my-llm-workers → -Users-duankaiqiang-proj-my-llm-workers
    # 解码不可逆（原路径中的 - 无法与编码 - 区分），故保留编码形式
    p = Path("/Users/duankaiqiang/.claude/projects/-Users-duankaiqiang-proj-my-llm-workers/session.jsonl")
    result = main._extract_project_name(p)
    # 返回原始编码的路径形式（- 分割）
    assert "Users" in result
    assert "proj" in result
    assert "llm" in result


# ---------------------------------------------------------------------------
# find_session_files
# ---------------------------------------------------------------------------

def test_find_session_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 创建模拟项目目录
        proj = root / "-Users-test-proj"
        proj.mkdir()
        (proj / "a.jsonl").write_text("{}")
        (proj / "b.jsonl").write_text("{}")
        # 子 agent 目录应被跳过
        sub = proj / "abc" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text("{}")

        files = main.find_session_files([root])
        names = [f.name for f in files]
        assert "a.jsonl" in names
        assert "b.jsonl" in names
        assert "agent-1.jsonl" in names  # 子 agent 也扫描


def test_find_session_files_empty():
    with tempfile.TemporaryDirectory() as td:
        files = main.find_session_files([Path(td)])
        assert files == []


def test_find_session_files_nonexistent_dir():
    files = main.find_session_files([Path("/no/such/dir")])
    assert files == []


# ---------------------------------------------------------------------------
# load_session_names
# ---------------------------------------------------------------------------

def test_load_session_names():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "12345.json").write_text(json.dumps({
            "pid": 12345,
            "sessionId": "abc-def-123",
            "cwd": "/Users/test/proj",
            "name": "my-session",
        }))
        (d / "invalid.json").write_text("not json")

        names = main.load_session_names(d)
        assert "abc-def-123" in names
        assert names["abc-def-123"]["name"] == "my-session"
        assert names["abc-def-123"]["cwd"] == "/Users/test/proj"


# ---------------------------------------------------------------------------
# parse_sessions
# ---------------------------------------------------------------------------

def _make_session_file(path: Path, lines: list[dict]):
    """写入模拟 JSONL session 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _make_assistant(model: str, timestamp: str, input_t: int, output_t: int,
                    cache_read: int = 0, cache_create: int = 0) -> dict:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
            },
        },
    }


def test_parse_sessions_basic():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "-Users-test-proj"
        proj.mkdir()

        sid = "test-session-001"
        _make_session_file(proj / f"{sid}.jsonl", [
            _make_assistant("deepseek-v4-pro", "2026-06-15T10:00:00.000Z",
                            input_t=1000, output_t=500),
            _make_assistant("deepseek-v4-pro", "2026-06-15T11:00:00.000Z",
                            input_t=2000, output_t=300),
            _make_assistant("claude-opus-4-6", "2026-06-16T10:00:00.000Z",
                            input_t=100, output_t=50, cache_read=500),
        ])

        files = main.find_session_files([root])
        stats = main.parse_sessions(
            files, date.fromisoformat("2026-06-01"), date.fromisoformat("2026-06-30"),
            session_names={}, model_pricing={},
        )

        ms = stats["model_stats"]
        assert "deepseek-v4-pro" in ms
        assert "claude-opus-4-6" in ms
        assert ms["deepseek-v4-pro"]["input_tokens"] == 3000
        assert ms["deepseek-v4-pro"]["output_tokens"] == 800
        assert ms["deepseek-v4-pro"]["total_tokens"] == 3800
        assert ms["deepseek-v4-pro"]["message_count"] == 2
        assert ms["claude-opus-4-6"]["input_tokens"] == 100
        assert ms["claude-opus-4-6"]["cache_read_input_tokens"] == 500

        # session 级别
        assert stats["processed_session_count"] == 1
        assert len(stats["session_list"]) == 1
        assert stats["session_list"][0]["total_tokens"] == 3950  # 3000+800+100+50


def test_parse_sessions_date_filter():
    """超出日期范围的消息应被过滤。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "-Users-test-proj"
        proj.mkdir()

        _make_session_file(proj / "s1.jsonl", [
            _make_assistant("deepseek-v4-pro", "2026-05-31T23:59:00.000Z",
                            input_t=1000, output_t=100),   # 早于范围
            _make_assistant("deepseek-v4-pro", "2026-06-01T00:01:00.000Z",
                            input_t=2000, output_t=200),   # 范围内
            _make_assistant("deepseek-v4-pro", "2026-07-01T00:00:00.000Z",
                            input_t=3000, output_t=300),   # 晚于范围
        ])

        files = main.find_session_files([root])
        stats = main.parse_sessions(
            files, date.fromisoformat("2026-06-01"), date.fromisoformat("2026-06-30"),
            session_names={}, model_pricing={},
        )
        ms = stats["model_stats"]
        assert ms["deepseek-v4-pro"]["input_tokens"] == 2000


def test_parse_sessions_zero_token_skip():
    """input 和 output 均为 0 的消息应被跳过。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "-Users-test-proj"
        proj.mkdir()

        _make_session_file(proj / "s1.jsonl", [
            _make_assistant("<synthetic>", "2026-06-15T10:00:00.000Z",
                            input_t=0, output_t=0),
            _make_assistant("deepseek-v4-pro", "2026-06-15T11:00:00.000Z",
                            input_t=100, output_t=50),
        ])

        files = main.find_session_files([root])
        stats = main.parse_sessions(
            files, date.fromisoformat("2026-06-01"), date.fromisoformat("2026-06-30"),
            session_names={}, model_pricing={},
        )
        ms = stats["model_stats"]
        assert "<synthetic>" not in ms
        assert ms["deepseek-v4-pro"]["message_count"] == 1


def test_parse_sessions_empty():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "-Users-test-proj"
        proj.mkdir()
        _make_session_file(proj / "s1.jsonl", [])
        files = main.find_session_files([root])
        stats = main.parse_sessions(
            files, date.fromisoformat("2026-06-01"), date.fromisoformat("2026-06-30"),
            session_names={}, model_pricing={},
        )
        assert stats["model_stats"] == {}
        assert stats["session_list"] == []


# ---------------------------------------------------------------------------
# 费用估算
# ---------------------------------------------------------------------------

def test_calc_model_cost_no_cache():
    stats = {"input_tokens": 1_000_000, "output_tokens": 500_000,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    price = {"input": 3.0, "output": 6.0}
    cost = main._calc_model_cost(stats, price)
    assert cost == pytest.approx(3.0 + 3.0)  # 1M*3 + 0.5M*6 = 3+3=6


def test_calc_model_cost_with_cache_hit():
    stats = {"input_tokens": 500_000, "output_tokens": 200_000,
             "cache_read_input_tokens": 500_000, "cache_creation_input_tokens": 0}
    price = {"input": 3.0, "output": 6.0, "cache_hit_input": 0.025}
    cost = main._calc_model_cost(stats, price)
    # input: 0.5M * 3.0 = 1.5
    # cache_read: 0.5M * 0.025 = 0.0125
    # output: 0.2M * 6.0 = 1.2
    # total: 1.5 + 0.0125 + 1.2 = 2.7125
    assert cost == pytest.approx(2.7125)


def test_find_price():
    pricing = {
        "deepseek-v4-pro": {"input": 3.0, "output": 6.0},
        "claude-opus": {"input": 109.5, "output": 547.5},
    }
    assert main._find_price("deepseek-v4-pro", pricing) is not None
    assert main._find_price("claude-opus-4-6", pricing) is not None  # 前缀匹配
    assert main._find_price("unknown-model", pricing) is None


def test_calc_model_cost_with_cache_create():
    """缓存写入 token 应按基础输入单价计费。"""
    stats = {"input_tokens": 100_000, "output_tokens": 50_000,
             "cache_read_input_tokens": 500_000, "cache_creation_input_tokens": 1_000_000}
    price = {"input": 100.0, "output": 200.0, "cache_hit_input": 10.0}
    cost = main._calc_model_cost(stats, price)
    # input+create: (100K+1M)/1M * 100 = 1.1*100 = 110
    # output: 50K/1M * 200 = 10
    # cache_read: 500K/1M * 10 = 5
    # total: 110 + 10 + 5 = 125
    assert cost == pytest.approx(125.0)


def test_estimate_cost():
    model_stats = {
        "deepseek-v4-pro": {"input_tokens": 1_000_000, "output_tokens": 500_000,
                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "claude-opus-4-6": {"input_tokens": 100_000, "output_tokens": 50_000,
                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    }
    pricing = {
        "deepseek-v4-pro": {"input": 3.0, "output": 6.0},
        "claude-opus-4-6": {"input": 109.5, "output": 547.5},
    }
    cost = main.estimate_cost(model_stats, pricing)
    # deepseek: 1.0M * 3/1M + 0.5M * 6/1M = 3 + 3 = 6
    # opus: 0.1M * 109.5/1M + 0.05M * 547.5/1M = 10.95 + 27.375 = 38.325
    # total = 44.325
    assert cost == pytest.approx(44.325)


# ---------------------------------------------------------------------------
# HTML 报告
# ---------------------------------------------------------------------------

def test_generate_html_contains_key_elements():
    model_stats = {
        "deepseek-v4-pro": {"input_tokens": 1000, "output_tokens": 500,
                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                            "total_tokens": 1500, "message_count": 2},
    }
    stats = {
        "model_stats": model_stats,
        "daily_stats": {"2026-06-15T10": {"deepseek-v4-pro": 1500}},
        "project_stats": {},
        "processed_session_count": 1,
        "total_session_count": 1,
    }
    html = main.generate_html(stats, date(2026, 6, 1), date(2026, 6, 30), {})
    assert "<!DOCTYPE html>" in html
    assert "Token 消耗报告" in html
    assert "deepseek-v4-pro" in html
    assert "小时 Token 趋势" in html


def test_strip_common_prefix():
    names = [
        "Users-duankaiqiang-proj-foo",
        "Users-duankaiqiang-proj-bar",
        "Users-duankaiqiang-code-baz",
    ]
    result = main._strip_common_prefix(names)
    assert result["Users-duankaiqiang-proj-foo"] == "proj-foo"
    assert result["Users-duankaiqiang-proj-bar"] == "proj-bar"
    assert result["Users-duankaiqiang-code-baz"] == "code-baz"


def test_strip_common_prefix_single():
    names = ["Users-duankaiqiang-proj-foo"]
    result = main._strip_common_prefix(names)
    assert result["Users-duankaiqiang-proj-foo"] == "proj-foo"


def test_strip_common_prefix_all_same():
    # 所有分段都相同时至少保留最后 2 段
    names = ["a-b-c", "a-b-c"]
    result = main._strip_common_prefix(names)
    assert result["a-b-c"] == "b-c"


def test_fmt_cost():
    display = {
        "claude-opus-4-6": {"currency": "usd", "input": 5, "output": 25},
        "deepseek-v4-pro": {"currency": "cny", "input": 3, "output": 6},
    }
    # USD model
    cost_str = main._fmt_cost(34.04, "claude-opus-4-6", display, 6.8086)
    assert "$5.00" in cost_str
    assert "¥34.04" in cost_str
    # CNY model
    cost_str = main._fmt_cost(12.50, "deepseek-v4-pro", display, 6.8086)
    assert "¥12.50" in cost_str
    assert "$" not in cost_str


def test_fmt_num():
    assert main._fmt_num(100) == "100"
    assert main._fmt_num(999) == "999"
    assert main._fmt_num(1500) == "1.5K"
    assert main._fmt_num(9999) == "10.0K"
    assert main._fmt_num(15000) == "15K"
    assert main._fmt_num(1_500_000) == "1.5M"
    assert main._fmt_num(0) == "0"

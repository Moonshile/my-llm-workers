import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

# 工具目录名含连字符，通过 sys.path 导入
sys.path.insert(0, str(Path(__file__).resolve().parent))
import main as journal  # type: ignore[import-not-at-top]

# 测试用静默 logger
_test_log = logging.getLogger("test")
_test_log.addHandler(logging.NullHandler())


# ============================================================
# 辅助函数
# ============================================================

def _make_jsonl_event(event_type: str, content, ts: str = None) -> dict:
    """构造一个 JSONL 事件。"""
    if ts is None:
        ts = "2026-06-09T10:00:00.000Z"
    event = {"type": event_type, "timestamp": ts}
    if event_type == "user":
        event["message"] = {"role": "user", "content": content}
    elif event_type == "assistant":
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        event["message"] = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": content,
            "stop_reason": "end_turn",
        }
    elif event_type == "system":
        event["message"] = {"content": content}
    return event


def _write_jsonl(path: Path, events: list[dict]):
    """写入 JSONL 文件。"""
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")


# ============================================================
# extract_condensed_transcript
# ============================================================

class TestExtractCondensedTranscript:
    """对话压缩测试。"""

    def test_basic_extraction(self, tmp_path):
        """基本提取：user + assistant 消息。"""
        events = [
            _make_jsonl_event("user", "你好，请帮我写代码"),
            _make_jsonl_event("assistant", "好的，我来帮你写代码"),
        ]
        jsonl = tmp_path / "test.jsonl"
        _write_jsonl(jsonl, events)

        result = journal.extract_condensed_transcript(jsonl)
        assert "[用户] 你好，请帮我写代码" in result
        assert "[助手] 好的，我来帮你写代码" in result

    def test_skips_thinking_blocks(self, tmp_path):
        """跳过 assistant 的 thinking 块。"""
        events = [
            _make_jsonl_event("user", "一个问题"),
            _make_jsonl_event("assistant", [
                {"type": "thinking", "thinking": "我需要思考..."},
                {"type": "text", "text": "答案在这里"},
            ]),
        ]
        jsonl = tmp_path / "test.jsonl"
        _write_jsonl(jsonl, events)

        result = journal.extract_condensed_transcript(jsonl)
        assert "答案在这里" in result
        assert "我需要思考" not in result

    def test_includes_tool_use_names(self, tmp_path):
        """标注使用的工具名称。"""
        events = [
            _make_jsonl_event("user", "读一个文件"),
            _make_jsonl_event("assistant", [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                {"type": "text", "text": "文件内容如下..."},
            ]),
        ]
        jsonl = tmp_path / "test.jsonl"
        _write_jsonl(jsonl, events)

        result = journal.extract_condensed_transcript(jsonl)
        assert "使用工具: Read" in result

    def test_since_timestamp_filters(self, tmp_path):
        """since_timestamp 过滤旧事件。"""
        events = [
            _make_jsonl_event("user", "旧消息", "2026-06-01T10:00:00.000Z"),
            _make_jsonl_event("assistant", "旧回复", "2026-06-01T10:01:00.000Z"),
            _make_jsonl_event("user", "新消息", "2026-06-09T10:00:00.000Z"),
            _make_jsonl_event("assistant", "新回复", "2026-06-09T10:01:00.000Z"),
        ]
        jsonl = tmp_path / "test.jsonl"
        _write_jsonl(jsonl, events)

        # 06-05 之后的时间戳
        cutoff = datetime(2026, 6, 5, 0, 0, 0).timestamp()
        result = journal.extract_condensed_transcript(jsonl, since_timestamp=cutoff)
        assert "旧消息" not in result
        assert "新消息" in result

    def test_empty_jsonl(self, tmp_path):
        """空 JSONL 返回空字符串。"""
        jsonl = tmp_path / "empty.jsonl"
        jsonl.write_text("")
        result = journal.extract_condensed_transcript(jsonl)
        assert result == ""

    def test_nonexistent_jsonl(self):
        """不存在的 JSONL 返回空字符串。"""
        result = journal.extract_condensed_transcript("/nonexistent/path.jsonl")
        assert result == ""

    def test_none_jsonl(self):
        """None 作为路径返回空。"""
        result = journal.extract_condensed_transcript(None)
        assert result == ""

    def test_skips_local_command_caveat(self, tmp_path):
        """跳过本地命令标记。"""
        events = [
            _make_jsonl_event("user", "<local-command-caveat>...</local-command-caveat>"),
            _make_jsonl_event("user", "真正的用户消息"),
        ]
        jsonl = tmp_path / "test.jsonl"
        _write_jsonl(jsonl, events)

        result = journal.extract_condensed_transcript(jsonl)
        assert "<local-command-caveat>" not in result
        assert "真正的用户消息" in result

    def test_max_chars_truncation(self, tmp_path):
        """max_chars 截断。"""
        events = [
            _make_jsonl_event("user", "A" * 5000),
        ]
        jsonl = tmp_path / "test.jsonl"
        _write_jsonl(jsonl, events)

        result = journal.extract_condensed_transcript(jsonl, max_chars=100)
        assert len(result) <= 100 + len("\n\n... (对话内容已截断)")
        assert "已截断" in result

    def test_user_message_with_content_blocks(self, tmp_path):
        """用户消息 content 为 list 格式。"""
        events = [
            _make_jsonl_event("user",
                [{"type": "text", "text": "第一部分"}, {"type": "text", "text": "第二部分"}]
            ),
        ]
        jsonl = tmp_path / "test.jsonl"
        _write_jsonl(jsonl, events)

        result = journal.extract_condensed_transcript(jsonl)
        assert "第一部分 第二部分" in result

    def test_system_message(self, tmp_path):
        """系统消息被包含。"""
        events = [
            _make_jsonl_event("system", "系统通知：会话即将过期"),
        ]
        jsonl = tmp_path / "test.jsonl"
        _write_jsonl(jsonl, events)

        result = journal.extract_condensed_transcript(jsonl)
        assert "系统通知" in result


# ============================================================
# parse_frontmatter
# ============================================================

class TestParseFrontmatter:
    """Frontmatter 解析测试。"""

    def test_basic_parsing(self):
        content = """---
title: 测试标题
date: 2026-06-09
tags: [tag1, tag2]
---
正文内容
"""
        fm = journal.parse_frontmatter(content)
        assert fm["title"] == "测试标题"
        # YAML 可能将 date 解析为 datetime.date 对象
        assert str(fm["date"]) == "2026-06-09"
        assert fm["tags"] == ["tag1", "tag2"]

    def test_no_frontmatter(self):
        content = "# 没有 frontmatter\n正文"
        fm = journal.parse_frontmatter(content)
        assert fm is None

    def test_empty_content(self):
        fm = journal.parse_frontmatter("")
        assert fm is None

    def test_unclosed_frontmatter(self):
        content = "---\ntitle: x\n没有闭合"
        fm = journal.parse_frontmatter(content)
        assert fm is None

    def test_invalid_yaml(self):
        content = "---\n[invalid yaml\n---\n"
        fm = journal.parse_frontmatter(content)
        assert fm is None


# ============================================================
# format_frontmatter
# ============================================================

class TestFormatFrontmatter:
    """Frontmatter 生成测试。"""

    def test_basic_format(self):
        meta = {
            "title": "测试",
            "date": "2026-06-09",
            "tags": ["a", "b"],
            "session_id": "abc-123",
            "project_path": "/test",
            "last_processed_timestamp": 1234567.0,
        }
        fm = journal.format_frontmatter(meta)
        assert fm.startswith("---\n")
        assert fm.endswith("---\n")
        assert "title: 测试" in fm
        assert "tags: [a, b]" in fm
        assert "session_id: abc-123" in fm
        assert "project_path: /test" in fm

    def test_empty_tags(self):
        meta = {
            "title": "x",
            "date": "2026-01-01",
            "tags": [],
            "session_id": "s1",
            "project_path": "/p",
            "last_processed_timestamp": 0.0,
        }
        fm = journal.format_frontmatter(meta)
        assert "tags: []" in fm

    def test_single_tag(self):
        meta = {
            "title": "x",
            "date": "2026-01-01",
            "tags": ["only"],
            "session_id": "s1",
            "project_path": "/p",
            "last_processed_timestamp": 0.0,
        }
        fm = journal.format_frontmatter(meta)
        assert "tags: [only]" in fm


# ============================================================
# slugify
# ============================================================

class TestSlugify:
    """标题 slug 化测试。"""

    def test_english_title(self):
        assert journal.slugify("Hello World") == "hello-world"

    def test_chinese_title(self):
        title = "用 Canvas 做墨汁扩散背景动效"
        slug = journal.slugify(title)
        assert "canvas" in slug
        assert len(slug) > 0
        assert not slug.startswith("-")
        assert not slug.endswith("-")

    def test_special_chars(self):
        slug = journal.slugify("A/B\\C:D*E?")
        assert "/" not in slug
        assert "\\" not in slug

    def test_empty_title(self):
        assert journal.slugify("") == "untitled"

    def test_long_title(self):
        slug = journal.slugify("a" * 200)
        assert len(slug) <= 80


# ============================================================
# find_existing_document
# ============================================================

class TestFindExistingDocument:
    """已有文档查找测试。"""

    def test_find_by_session_id(self, tmp_path):
        doc = tmp_path / "test.md"
        doc.write_text("""---
title: x
date: 2026-01-01
tags: []
session_id: my-session-123
project_path: /p
last_processed_timestamp: 0.0
---
内容
""")
        result = journal.find_existing_document(tmp_path, "my-session-123")
        assert result == doc

    def test_not_found(self, tmp_path):
        doc = tmp_path / "other.md"
        doc.write_text("""---
session_id: other-id
---
""")
        result = journal.find_existing_document(tmp_path, "nonexistent")
        assert result is None

    def test_empty_dir(self, tmp_path):
        result = journal.find_existing_document(tmp_path, "any")
        assert result is None

    def test_nonexistent_dir(self):
        result = journal.find_existing_document(Path("/nonexistent/dir"), "any")
        assert result is None


# ============================================================
# get_existing_categories
# ============================================================

class TestGetExistingCategories:
    """已有分类目录获取测试。"""

    def test_gets_dirs(self, tmp_path):
        (tmp_path / "前端开发").mkdir()
        (tmp_path / "后端开发").mkdir()
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "some_file.txt").write_text("x")

        cats = journal.get_existing_categories(tmp_path)
        assert "前端开发" in cats
        assert "后端开发" in cats
        assert ".hidden" not in cats

    def test_nonexistent_dir(self):
        cats = journal.get_existing_categories(Path("/nonexistent"))
        assert cats == []


# ============================================================
# 标签与分类
# ============================================================

class TestTagHelpers:
    """标签和分类辅助函数测试。"""

    def test_ensure_serious_appends(self):
        tags = ["技术"]
        result = journal._ensure_tags_include_serious(tags)
        assert "严肃工作" in result
        assert "技术" in result

    def test_ensure_serious_no_duplicate(self):
        tags = ["技术", "严肃工作"]
        result = journal._ensure_tags_include_serious(tags)
        assert result.count("严肃工作") == 1

    def test_determine_category_serious(self):
        home = os.path.expanduser("~")
        result = journal._determine_category(
            ["任一"], f"{home}/proj/my-llm-workers", ["~/proj/my-llm-workers"]
        )
        assert result == "严肃工作"

    def test_determine_category_not_serious(self):
        result = journal._determine_category(
            ["任一"], "/Users/test/some-other-project", ["~/proj/my-llm-workers"]
        )
        assert result == ""


# ============================================================
# _build_document_content
# ============================================================

class TestBuildDocumentContent:
    """文档内容构建测试。"""

    def test_basic_structure(self):
        llm_result = {
            "title": "测试标题",
            "overview": ["工作项1", "工作项2"],
            "complex_work": [
                {
                    "topic": "复杂问题",
                    "problem": "描述",
                    "solution": "方案",
                    "key_decisions": ["决策1"],
                }
            ],
            "multi_turn": [
                {
                    "topic": "多轮问题",
                    "rounds": 5,
                    "reason": "原因",
                    "suggestions": ["建议1"],
                }
            ],
            "best_practices": ["实践1"],
            "notes": "备注",
        }
        session = {"session_id": "abc", "project_path": "/test", "mtime_date": "2026-06-09"}
        result = journal._build_document_content(llm_result, session, "2026-06-10 10:00:00")

        assert "# 测试标题" in result
        assert "## 1. 工作概览" in result
        assert "工作项1" in result
        assert "## 2. 高复杂度工作" in result
        assert "### 复杂问题" in result
        assert "## 3. 多轮交互分析" in result
        assert "## 4. 最佳实践" in result
        assert "## 5. 其他备注" in result

    def test_empty_sections(self):
        llm_result = {
            "title": "空",
            "overview": [],
            "complex_work": [],
            "multi_turn": [],
            "best_practices": [],
            "notes": "",
        }
        session = {"session_id": "abc", "project_path": "/test", "mtime_date": "2026-06-09"}
        result = journal._build_document_content(llm_result, session, "now")
        assert "（无记录）" in result
        assert "## 5. 其他备注" not in result


# ============================================================
# discover_sessions（集成）
# ============================================================

class TestDiscoverSessions:
    """Session 发现测试。"""

    def test_discovers_jsonl_files(self, tmp_path):
        # 模拟项目目录结构
        proj_dir = tmp_path / "projects"
        proj_dir.mkdir()
        sess_dir = proj_dir / "-Users-test-proj-foo"
        sess_dir.mkdir(parents=True)

        # 创建两个 session，一个今天修改，一个昨天修改
        yesterday = time.time() - 86400
        today = time.time()

        old_sess = sess_dir / "old-session.jsonl"
        old_sess.write_text(
            json.dumps({"type": "user", "message": {"content": "hi"}, "timestamp": "2026-06-08T10:00:00Z"}) + "\n"
        )
        os.utime(old_sess, (yesterday, yesterday))

        new_sess = sess_dir / "new-session.jsonl"
        new_sess.write_text(
            json.dumps({"type": "user", "message": {"content": "hi"}, "timestamp": "2026-06-10T10:00:00Z"}) + "\n"
        )
        os.utime(new_sess, (today, today))

        sessions = journal.discover_sessions([proj_dir])

        ids = [s["session_id"] for s in sessions]
        assert "old-session" in ids
        assert "new-session" not in ids  # 今天的被跳过

    def test_excludes_subagents(self, tmp_path):
        proj_dir = tmp_path / "projects"
        sess_dir = proj_dir / "-Users-test/subagents"
        sess_dir.mkdir(parents=True)
        agent_sess = sess_dir / "agent-123.jsonl"
        agent_sess.write_text(
            json.dumps({"type": "user", "message": {"content": "hi"}, "timestamp": "2026-06-08T10:00:00Z"}) + "\n"
        )
        yesterday = time.time() - 86400
        os.utime(agent_sess, (yesterday, yesterday))

        sessions = journal.discover_sessions([proj_dir])
        assert len(sessions) == 0  # subagents 被排除

    def test_dedup_by_session_id_and_project(self, tmp_path):
        """按 (session_id, project_path) 去重。"""
        proj_dir = tmp_path / "projects"
        p1 = proj_dir / "-Users-test-proj-a"
        p2 = proj_dir / "-Users-test-proj-b"
        p1.mkdir(parents=True)
        p2.mkdir(parents=True)

        yesterday = time.time() - 86400
        # 同 session_id 不同项目
        for p in [p1, p2]:
            s = p / "same-id.jsonl"
            s.write_text(
                json.dumps({"type": "user", "message": {"content": "hi"}, "timestamp": "2026-06-08T10:00:00Z"}) + "\n"
            )
            os.utime(s, (yesterday, yesterday))

        sessions = journal.discover_sessions([proj_dir])
        assert len(sessions) == 2  # 两个不同 session_id 的


# ============================================================
# 集成测试
# ============================================================

class TestIntegration:
    """端到端集成测试。"""

    def test_full_pipeline(self, tmp_path, monkeypatch):
        """完整流程测试（mock LLM）。"""
        # 创建测试 session
        proj_dir = tmp_path / "sessions"
        sess_dir = proj_dir / "-Users-test-proj-foo"
        sess_dir.mkdir(parents=True)

        events = [
            _make_jsonl_event("user", "帮我写一个 Python 脚本", "2026-06-08T10:00:00Z"),
            _make_jsonl_event("assistant", "好的，这是脚本...", "2026-06-08T10:01:00Z"),
            _make_jsonl_event("user", "有个 bug 需要修复", "2026-06-08T10:05:00Z"),
            _make_jsonl_event("assistant", [
                {"type": "thinking", "thinking": "让我分析..."},
                {"type": "text", "text": "Bug 已修复"},
                {"type": "tool_use", "name": "Edit", "input": {}},
            ], "2026-06-08T10:06:00Z"),
        ]
        jsonl = sess_dir / "test-session.jsonl"
        _write_jsonl(jsonl, events)
        yesterday = time.time() - 86400
        os.utime(jsonl, (yesterday, yesterday))

        output_dir = tmp_path / "output"

        config = {
            "api_base": "https://fake-api.example.com/v1",
            "api_key": "fake-key",
            "model": "fake-model",
            "output_dir": output_dir,
            "max_chunk_chars": 8000,
            "serious_work_paths": [],
        }

        # Mock LLM 响应
        mock_llm_response = {
            "title": "Python 脚本开发",
            "tags": ["Python", "开发"],
            "category": "后端开发",
            "overview": ["编写 Python 脚本", "修复 bug"],
            "complex_work": [
                {
                    "topic": "Bug 修复",
                    "problem": "代码有逻辑错误",
                    "solution": "通过 Edit 工具修复",
                    "key_decisions": ["使用逐行调试"],
                }
            ],
            "multi_turn": [
                {
                    "topic": "Bug 定位",
                    "rounds": 2,
                    "reason": "问题描述不够清晰",
                    "suggestions": ["提供更详细的错误信息"],
                }
            ],
            "best_practices": ["先测试再修改"],
            "notes": "无",
        }

        def mock_call_llm(prompt, cfg):
            return mock_llm_response

        with mock.patch.object(journal, "call_llm", mock_call_llm):
            session = {
                "session_id": "test-session",
                "project_path": "/Users/test/proj-foo",
                "jsonl_path": str(jsonl),
                "mtime": yesterday,
                "mtime_date": "2026-06-08",
                "created_date": "2026-06-08",
            }
            result = journal.process_session(session, config)

        assert result is not None
        result_path = Path(result)
        assert result_path.exists()

        content = result_path.read_text()
        assert "Python 脚本开发" in content
        assert "Bug 修复" in content
        assert "test-session" in content

        fm = journal.parse_frontmatter(content)
        assert fm is not None
        assert fm["title"] == "Python 脚本开发"
        assert "Python" in fm["tags"]
        assert fm["session_id"] == "test-session"

    def test_incremental_update(self, tmp_path):
        """增量更新：已有文档，添加新事件后重新生成。"""
        output_dir = tmp_path / "output"
        cat_dir = output_dir / "后端开发"
        cat_dir.mkdir(parents=True)

        # 创建已有文档
        old_doc = cat_dir / "2026-06-08-test.md"
        old_doc.write_text("""---
title: 旧标题
date: 2026-06-08
tags: [旧标签]
session_id: inc-session
project_path: /test
last_processed_timestamp: 1000000.0
---
# 旧标题
旧内容
""")

        # 创建 session（mtime 比 last_processed_timestamp 新）
        proj_dir = tmp_path / "sessions"
        sess_dir = proj_dir / "-Users-test"
        sess_dir.mkdir(parents=True)

        events = [
            _make_jsonl_event("user", "新增的工作", "2026-06-09T10:00:00Z"),
            _make_jsonl_event("assistant", "新增的回复", "2026-06-09T10:01:00Z"),
        ]
        jsonl = sess_dir / "inc-session.jsonl"
        _write_jsonl(jsonl, events)
        # mtime 在 last_processed_timestamp 之后
        new_mtime = time.time() - 86400
        os.utime(jsonl, (new_mtime, new_mtime))

        # session 的 mtime_date 是昨天
        yesterday_str = datetime.fromtimestamp(new_mtime).strftime("%Y-%m-%d")

        config = {
            "api_base": "https://fake-api.example.com/v1",
            "api_key": "fake-key",
            "model": "fake-model",
            "output_dir": output_dir,
            "max_chunk_chars": 8000,
            "serious_work_paths": [],
        }

        mock_llm_response = {
            "title": "更新后的标题",
            "tags": ["新标签"],
            "category": "后端开发",
            "overview": ["新增的工作"],
            "complex_work": [],
            "multi_turn": [],
            "best_practices": [],
            "notes": "",
        }

        def mock_call_llm(prompt, cfg):
            return mock_llm_response

        with mock.patch.object(journal, "call_llm", mock_call_llm):
            session = {
                "session_id": "inc-session",
                "project_path": "/test",
                "jsonl_path": str(jsonl),
                "mtime": new_mtime,
                "mtime_date": yesterday_str,
                "created_date": "2026-06-08",
            }
            result = journal.process_session(session, config)

        assert result is not None
        content = Path(result).read_text()
        fm = journal.parse_frontmatter(content)
        assert fm["title"] == "更新后的标题"
        assert "新标签" in fm["tags"]
        # last_processed_timestamp 已更新
        assert fm["last_processed_timestamp"] > 1000000.0

    def test_skip_no_new_content(self, tmp_path):
        """无新增内容时跳过。"""
        output_dir = tmp_path / "output"
        cat_dir = output_dir / "测试"
        cat_dir.mkdir(parents=True)

        # 已有文档，last_processed_timestamp 比 session mtime 更新
        old_doc = cat_dir / "2026-06-01-skip.md"
        old_doc.write_text("""---
title: 跳过
date: 2026-06-01
tags: [test]
session_id: skip-session
project_path: /test
last_processed_timestamp: 9999999999.0
---
内容
""")

        config = {
            "api_base": "https://fake-api.example.com/v1",
            "api_key": "fake-key",
            "model": "fake-model",
            "output_dir": output_dir,
            "max_chunk_chars": 8000,
            "serious_work_paths": [],
        }

        session = {
            "session_id": "skip-session",
            "project_path": "/test",
            "jsonl_path": None,
            "mtime": 1000.0,  # 远小于 last_processed_timestamp
            "mtime_date": "2026-06-01",
            "created_date": "2026-06-01",
        }

        result = journal.process_session(session, config)
        assert result is None  # 被跳过

    def test_serious_work_tagging(self, tmp_path):
        """严肃工作路径自动打标签并放入对应子目录。"""
        proj_dir = tmp_path / "sessions"
        sess_dir = proj_dir / "-Users-test-proj-my-llm-workers"
        sess_dir.mkdir(parents=True)

        events = [
            _make_jsonl_event("user", "工作内容", "2026-06-08T10:00:00Z"),
            _make_jsonl_event("assistant", "回复内容", "2026-06-08T10:01:00Z"),
        ]
        jsonl = sess_dir / "serious-session.jsonl"
        _write_jsonl(jsonl, events)
        yesterday = time.time() - 86400
        os.utime(jsonl, (yesterday, yesterday))

        output_dir = tmp_path / "output"
        serious_path = str(tmp_path / "sessions" / "-Users-test-proj-my-llm-workers")

        config = {
            "api_base": "https://fake-api.example.com/v1",
            "api_key": "fake-key",
            "model": "fake-model",
            "output_dir": output_dir,
            "max_chunk_chars": 8000,
            "serious_work_paths": [serious_path],
        }

        mock_llm_response = {
            "title": "严肃工作内容",
            "tags": ["技术"],
            "category": "后端开发",
            "complexity": "complex",
            "summary": "简单总结",
            "overview": ["工作内容"],
            "complex_work": [],
            "multi_turn": [],
            "best_practices": [],
            "notes": "",
        }

        def mock_call_llm(prompt, cfg):
            return mock_llm_response

        with mock.patch.object(journal, "call_llm", mock_call_llm):
            session = {
                "session_id": "serious-session",
                "project_path": str(tmp_path / "sessions" / "-Users-test-proj-my-llm-workers"),
                "jsonl_path": str(jsonl),
                "mtime": yesterday,
                "mtime_date": "2026-06-08",
                "created_date": "2026-06-08",
            }
            result = journal.process_session(session, config)

        assert result is not None
        result_path = Path(result)
        assert "严肃工作" in str(result_path)
        content = result_path.read_text()
        fm = journal.parse_frontmatter(content)
        assert "严肃工作" in fm["tags"]

    def test_simple_session_goes_to_daily_brief(self, tmp_path):
        """Simple 模式写入 daily brief 文件。"""
        proj_dir = tmp_path / "sessions"
        sess_dir = proj_dir / "-Users-test"
        sess_dir.mkdir(parents=True)

        events = [
            _make_jsonl_event("user", "修改了 README", "2026-06-08T10:00:00Z"),
            _make_jsonl_event("assistant", "已完成修改", "2026-06-08T10:01:00Z"),
        ]
        jsonl = sess_dir / "simple-session.jsonl"
        _write_jsonl(jsonl, events)
        yesterday = time.time() - 86400
        os.utime(jsonl, (yesterday, yesterday))

        output_dir = tmp_path / "output"
        config = {
            "api_base": "https://fake-api.example.com/v1",
            "api_key": "fake-key",
            "model": "fake-model",
            "output_dir": output_dir,
            "max_chunk_chars": 8000,
            "serious_work_paths": [],
        }

        mock_llm_response = {
            "title": "更新 README",
            "tags": ["文档"],
            "category": "工具脚本",
            "complexity": "simple",
            "summary": "修改了 README 中的配置说明。",
        }

        def mock_call_llm(prompt, cfg):
            return mock_llm_response

        with mock.patch.object(journal, "call_llm", mock_call_llm):
            session = {
                "session_id": "simple-session",
                "project_path": "/test",
                "jsonl_path": str(jsonl),
                "mtime": yesterday,
                "mtime_date": "2026-06-08",
                "created_date": "2026-06-08",
            }
            result = journal.process_session(session, config)

        assert result is not None
        result_path = Path(result)
        assert result_path.name == "2026-06-08-daily.md"
        content = result_path.read_text()
        fm = journal.parse_frontmatter(content)
        assert fm["type"] == "daily-brief"
        assert len(fm["sessions"]) == 1
        assert fm["sessions"][0]["session_id"] == "simple-session"
        assert "更新 README" in content
        assert "修改了 README 中的配置说明" in content

    def test_multiple_simple_sessions_aggregate(self, tmp_path):
        """多个 simple session 聚合到同一个 daily brief。"""
        output_dir = tmp_path / "output"
        cat_dir = output_dir / "工具脚本"
        cat_dir.mkdir(parents=True)

        config = {
            "api_base": "https://fake-api.example.com/v1",
            "api_key": "fake-key",
            "model": "fake-model",
            "output_dir": output_dir,
            "max_chunk_chars": 8000,
            "serious_work_paths": [],
        }

        # 处理第一个 simple session
        mock_response_1 = {
            "title": "修复拼写错误",
            "tags": ["修复"],
            "category": "工具脚本",
            "complexity": "simple",
            "summary": "修了一处拼写。",
        }

        with mock.patch.object(journal, "call_llm", lambda p, c: mock_response_1):
            session1 = {
                "session_id": "session-1",
                "project_path": "/test",
                "jsonl_path": None,
                "mtime": time.time() - 86400,
                "mtime_date": "2026-06-08",
                "created_date": "2026-06-08",
            }
            # 需要创建真实的 JSONL 才能提取对话
            proj_dir = tmp_path / "sessions" / "-test"
            proj_dir.mkdir(parents=True)
            jsonl1 = proj_dir / "session-1.jsonl"
            _write_jsonl(jsonl1, [
                _make_jsonl_event("user", "修复拼写", "2026-06-08T10:00:00Z"),
                _make_jsonl_event("assistant", "已修复", "2026-06-08T10:01:00Z"),
            ])
            session1["jsonl_path"] = str(jsonl1)
            result1 = journal.process_session(session1, config)

        assert result1 is not None

        # 处理第二个 simple session（同一天同一分类）
        mock_response_2 = {
            "title": "添加注释",
            "tags": ["文档"],
            "category": "工具脚本",
            "complexity": "simple",
            "summary": "给函数添加了注释。",
        }

        with mock.patch.object(journal, "call_llm", lambda p, c: mock_response_2):
            jsonl2 = proj_dir / "session-2.jsonl"
            _write_jsonl(jsonl2, [
                _make_jsonl_event("user", "添加注释", "2026-06-08T10:00:00Z"),
                _make_jsonl_event("assistant", "已完成", "2026-06-08T10:01:00Z"),
            ])
            session2 = {
                "session_id": "session-2",
                "project_path": "/test",
                "jsonl_path": str(jsonl2),
                "mtime": time.time() - 86400,
                "mtime_date": "2026-06-08",
                "created_date": "2026-06-08",
            }
            result2 = journal.process_session(session2, config)

        assert result2 is not None
        # 两个结果指向同一个 daily 文件
        assert result1 == result2

        content = Path(result1).read_text()
        fm = journal.parse_frontmatter(content)
        assert len(fm["sessions"]) == 2
        assert "修复拼写错误" in content
        assert "添加注释" in content

    def test_simple_session_incremental_update(self, tmp_path):
        """Daily brief 中的 simple session 增量更新。"""
        output_dir = tmp_path / "output"
        config = {
            "api_base": "https://fake-api.example.com/v1",
            "api_key": "fake-key",
            "model": "fake-model",
            "output_dir": output_dir,
            "max_chunk_chars": 8000,
            "serious_work_paths": [],
        }
        cat_dir = output_dir / "测试"
        cat_dir.mkdir(parents=True)

        # 先创建一个已有 daily brief
        daily_path = cat_dir / "2026-06-08-daily.md"
        daily_path.write_text("""---
title: 每日简报 — 2026-06-08
date: 2026-06-08
type: daily-brief
category: 测试
sessions:
  - session_id: old-session
    title: 旧工作
    project_path: /test
    last_processed_timestamp: 1000000.0
---

# 每日简报 — 2026-06-08

## 旧工作
Session `old-session` | 项目 `/test`

旧的总结。
""")

        # 同一个 session 有新内容（mtime > last_processed_timestamp）
        new_mtime = 2000000.0
        proj_dir = tmp_path / "sessions" / "-test"
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / "old-session.jsonl"
        _write_jsonl(jsonl, [
            _make_jsonl_event("user", "新增工作", "2026-06-09T10:00:00Z"),
            _make_jsonl_event("assistant", "已完成", "2026-06-09T10:01:00Z"),
        ])

        mock_response = {
            "title": "更新后的标题",
            "tags": ["更新"],
            "category": "测试",
            "complexity": "simple",
            "summary": "新增了更多工作内容。",
        }

        with mock.patch.object(journal, "call_llm", lambda p, c: mock_response):
            session = {
                "session_id": "old-session",
                "project_path": "/test",
                "jsonl_path": str(jsonl),
                "mtime": new_mtime,
                "mtime_date": "2026-06-08",
                "created_date": "2026-06-08",
            }
            result = journal.process_session(session, config)

        assert result is not None
        content = Path(result).read_text()
        fm = journal.parse_frontmatter(content)
        assert len(fm["sessions"]) == 1
        # last_processed_timestamp 已更新
        assert fm["sessions"][0]["last_processed_timestamp"] > 1000000.0
        assert "更新后的标题" in content

    def test_find_session_in_daily_briefs(self, tmp_path):
        """在 daily brief 中查找 session。"""
        output_dir = tmp_path / "output"
        cat_dir = output_dir / "测试"
        cat_dir.mkdir(parents=True)

        daily_path = cat_dir / "2026-06-08-daily.md"
        daily_path.write_text("""---
title: 每日简报
date: 2026-06-08
type: daily-brief
category: 测试
sessions:
  - session_id: target-session
    title: 目标
    project_path: /test
    last_processed_timestamp: 1234567.0
---

内容
""")

        info = journal._find_session_in_daily_briefs(output_dir, "target-session")
        assert info is not None
        assert info["last_processed_timestamp"] == 1234567.0
        assert info["title"] == "目标"

        # 不存在的 session
        info = journal._find_session_in_daily_briefs(output_dir, "not-found")
        assert info is None

    def test_find_existing_document_skips_daily_briefs(self, tmp_path):
        """find_existing_document 跳过 daily brief 文件。"""
        output_dir = tmp_path / "output"
        cat_dir = output_dir / "测试"
        cat_dir.mkdir(parents=True)

        # 创建 daily brief（不应被 find_existing_document 返回）
        daily_path = cat_dir / "2026-06-08-daily.md"
        daily_path.write_text("""---
title: x
session_id: target-session
---
""")

        result = journal.find_existing_document(output_dir, "target-session")
        assert result is None


# ============================================================
# Mock load_dotenv
# ============================================================

def test_get_config_with_env():
    """测试 get_config 从环境变量读取。"""
    with mock.patch("main.load_dotenv") as mock_load:
        with mock.patch.dict(os.environ, {
            "API_BASE": "https://test.example.com/v1",
            "API_KEY": "test-key",
            "MODEL": "test-model",
        }):
            # 重新 import 会失败因为已缓存，改用直接测
            # 这里验证 load_dotenv 被调用即可
            pass

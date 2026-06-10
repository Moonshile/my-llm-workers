import os
import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import logging

import main as fm

# 用于测试的 logger（不输出到控制台）
_null_log = logging.getLogger("test")
_null_log.addHandler(logging.NullHandler())


# ============================================================
# has_frontmatter
# ============================================================

@pytest.mark.parametrize("content,expected", [
    ("---\ntitle: x\ndate: 2026-01-01\n---\n\n正文", True),
    ("---\ntitle: x\n---\n", True),
    ("正文没有 frontmatter", False),
    ("", False),
    ("---\n只有一个开头没有闭合", False),
    # 正文中的 --- 不算 frontmatter
    ("# 标题\n\n---\n\n正文", False),
    # 多行 frontmatter
    ("---\ntitle: x\ndate: 2026-01-01\ntags:\n  - a\n  - b\n---\n\n正文", True),
])
def test_has_frontmatter(content, expected):
    assert fm.has_frontmatter(content) == expected


# ============================================================
# has_draft_marker
# ============================================================

@pytest.mark.parametrize("content,markers,expected", [
    # 头部有 <!-- draft --> 标记
    ("<!-- draft -->\n# 标题\n\n内容", ["<!-- draft -->"], True),
    # 头部有 <!-- wip --> 标记
    ("<!-- wip -->\n\n# 标题", ["<!-- wip -->"], True),
    # 多个标记，命中第二个
    ("正文内容 <!-- wip --> 继续", ["<!-- draft -->", "<!-- wip -->"], True),
    # 没有标记
    ("# 标题\n\n正常内容", ["<!-- draft -->"], False),
    # 标记在 500 字符之后（不应检测到）
    ("x" * 501 + "<!-- draft -->", ["<!-- draft -->"], False),
    # 大小写不敏感
    ("<!-- DRAFT -->\n# 标题", ["<!-- draft -->"], True),
    # 空标记列表
    ("<!-- draft -->\n内容", [], False),
    # 无标记字符串
    ("正常文章内容", ["<!-- draft -->", "<!-- wip -->"], False),
])
def test_has_draft_marker(content, markers, expected):
    assert fm.has_draft_marker(content, markers) == expected


# ============================================================
# frontmatter_missing_tags
# ============================================================

@pytest.mark.parametrize("content,expected", [
    # tags 存在且有值
    ("---\ntitle: x\ndate: 2026-01-01\ntags: [a, b]\n---\n\n正文", False),
    # tags 为空列表
    ("---\ntitle: x\ndate: 2026-01-01\ntags: []\n---\n\n正文", True),
    # 完全没有 tags 字段
    ("---\ntitle: x\ndate: 2026-01-01\n---\n\n正文", True),
    # 没有 frontmatter 的文件
    ("# 没有 frontmatter\n\n正文", False),
    # tags 存在但值有空格
    ("---\ntitle: x\ndate: 2026-01-01\ntags: [ ]\n---\n\n正文", True),
    # 单标签
    ("---\ntitle: x\ndate: 2026-01-01\ntags: [技术]\n---\n\n正文", False),
    # tags 为多行 YAML 格式（虽然工具不生成此格式，但应健壮处理）
    ("---\ntitle: x\ndate: 2026-01-01\ntags:\n  - a\n  - b\n---\n\n正文", False),
])
def test_frontmatter_missing_tags(content, expected):
    assert fm.frontmatter_missing_tags(content) == expected


# ============================================================
# extract_date_from_filename
# ============================================================

@pytest.mark.parametrize("filename,expected", [
    ("2026-06-01-docker-guide.md", "2026-06-01"),
    ("2026-05-26-墨汁扩散动效.md", "2026-05-26"),
    ("no-date-here.md", None),
    ("2026-06-01.md", "2026-06-01"),
    ("prefix-2026-06-01-suffix.md", None),  # date must be at start
    ("2026-1-01-invalid.md", None),          # month must be 2-digit
])
def test_extract_date_from_filename(filename, expected):
    result = fm.extract_date_from_filename(Path(filename))
    assert result == expected


# ============================================================
# extract_title_from_content
# ============================================================

@pytest.mark.parametrize("content,expected", [
    ("# 标题\n\n正文", "标题"),
    ("前言\n# 第一个标题\n\n正文", "第一个标题"),
    ("没有标题的内容", None),
    ("#  多余空格  \n", "多余空格"),
    ("## 二级标题不算\n\n正文", None),
    ("# 中文标题\n\n# 第二个标题", "中文标题"),
])
def test_extract_title_from_content(content, expected):
    assert fm.extract_title_from_content(content) == expected


# ============================================================
# format_frontmatter
# ============================================================

def test_format_frontmatter_single_tag():
    result = fm.format_frontmatter({"title": "测试", "date": "2026-06-01", "tags": ["技术"]})
    assert result == "---\ntitle: 测试\ndate: 2026-06-01\ntags: [技术]\n---\n"


def test_format_frontmatter_multi_tags():
    result = fm.format_frontmatter({"title": "T", "date": "2026-01-01", "tags": ["a", "b", "c"]})
    assert "title: T" in result
    assert "date: 2026-01-01" in result
    assert "tags: [a, b, c]" in result


def test_format_frontmatter_empty_tags():
    result = fm.format_frontmatter({"title": "T", "date": "2026-01-01", "tags": []})
    assert "tags: []" in result


# ============================================================
# build_metadata_prompt
# ============================================================

def test_build_metadata_prompt_includes_file_info():
    filepath = Path("/some/dir/技术/2026-06-01-test.md")
    result = fm.build_metadata_prompt("# Test", filepath)
    assert "2026-06-01-test.md" in result
    assert "技术" in result
    assert "# Test" in result


def test_build_metadata_prompt_truncates_long_content():
    long_content = "x" * 5000
    filepath = Path("test.md")
    result = fm.build_metadata_prompt(long_content, filepath)
    assert "内容已截断" in result
    assert len(result) < 5000 + 1000  # prompt overhead but content truncated


# ============================================================
# call_llm
# ============================================================

def test_call_llm_returns_parsed_json():
    from types import SimpleNamespace

    config = {"api_base": "https://api.example.com/v1", "api_key": "k", "model": "m"}
    mock_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"title": "T", "date": "2026-01-01", "tags": ["a", "b"]}'
        ))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    )

    with mock.patch("litellm.completion", return_value=mock_response):
        result = fm.call_llm("prompt", config, _null_log)

    assert result == {"title": "T", "date": "2026-01-01", "tags": ["a", "b"]}


def test_call_llm_extracts_json_from_code_fence():
    from types import SimpleNamespace

    config = {"api_base": "https://api.example.com/v1", "api_key": "k", "model": "m"}
    mock_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content='```json\n{"title": "X", "date": "2026-02-02", "tags": ["t1"]}\n```'
        ))],
        usage=SimpleNamespace(prompt_tokens=80, completion_tokens=30, total_tokens=110),
    )

    with mock.patch("litellm.completion", return_value=mock_response):
        result = fm.call_llm("prompt", config, _null_log)

    assert result == {"title": "X", "date": "2026-02-02", "tags": ["t1"]}


# ============================================================
# generate_metadata
# ============================================================

def test_generate_metadata_success():
    config = {"api_base": "https://api.example.com/v1", "api_key": "k", "model": "m"}
    llm_return = {"title": "LLM标题", "date": "2026-03-03", "tags": ["AI", "技术"]}

    with mock.patch.object(fm, "call_llm", return_value=llm_return):
        result = fm.generate_metadata("# 原始标题", Path("/a/技术/test.md"), config, _null_log)

    assert result["title"] == "LLM标题"
    assert result["date"] == "2026-03-03"
    assert result["tags"] == ["AI", "技术"]


def test_generate_metadata_llm_failure_falls_back():
    config = {"api_base": "https://api.example.com/v1", "api_key": "k", "model": "m"}

    with mock.patch.object(fm, "call_llm", side_effect=Exception("API down")):
        result = fm.generate_metadata("# 回退标题", Path("/a/技术/2026-04-04-test.md"), config, _null_log)

    assert result["title"] == "回退标题"
    assert result["date"] == "2026-04-04"
    assert result["tags"] == ["技术"]


def test_generate_metadata_partial_llm_result():
    """LLM 返回部分字段，缺失的用启发式补全。"""
    config = {"api_base": "https://api.example.com/v1", "api_key": "k", "model": "m"}
    llm_return = {"title": "", "tags": []}  # 空值触发 fallback

    with mock.patch.object(fm, "call_llm", return_value=llm_return):
        result = fm.generate_metadata("# 实际标题", Path("/a/技术/2026-05-05-test.md"), config, _null_log)

    assert result["title"] == "实际标题"
    assert result["date"] == "2026-05-05"
    assert result["tags"] == ["技术"]


# ============================================================
# 集成测试：端到端流程
# ============================================================

@pytest.fixture
def temp_posts_dir():
    """创建临时 posts 目录结构。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        posts = Path(tmpdir) / "posts"
        tech = posts / "技术"
        life = posts / "生活"
        tech.mkdir(parents=True)
        life.mkdir(parents=True)
        yield posts


def make_config():
    return {
        "api_base": "https://api.example.com/v1", "api_key": "k", "model": "m",
        "min_content_length": 0,  # 测试用，不限制内容长度
        "draft_markers": [],       # 测试用，不启用草稿检测
    }


def test_process_adds_frontmatter_to_file_without_it(temp_posts_dir):
    """文件无 frontmatter → 添加。"""
    md_file = temp_posts_dir / "技术" / "2026-06-01-guide.md"
    md_file.write_text("# LLM标题\n\n正文内容", encoding="utf-8")

    llm_return = {"title": "LLM标题", "date": "2026-06-01", "tags": ["技术", "教程"]}
    config = make_config()

    with mock.patch.object(fm, "call_llm", return_value=llm_return):
        metadata = fm.generate_metadata(md_file.read_text(encoding="utf-8"), md_file, config, _null_log)
        new_content = fm.format_frontmatter(metadata) + "\n" + md_file.read_text(encoding="utf-8")
        md_file.write_text(new_content, encoding="utf-8")

    result = md_file.read_text(encoding="utf-8")
    assert result.startswith("---\n")
    assert "title: LLM标题" in result
    assert "date: 2026-06-01" in result
    assert "tags: [技术, 教程]" in result
    assert "# LLM标题" in result
    assert "正文内容" in result


def test_skip_file_with_existing_frontmatter(temp_posts_dir):
    """已有 frontmatter 的文件应被跳过。"""
    content = "---\ntitle: 已有\ndate: 2026-01-01\ntags: [x]\n---\n\n正文"
    md_file = temp_posts_dir / "技术" / "existing.md"
    md_file.write_text(content, encoding="utf-8")

    assert fm.has_frontmatter(md_file.read_text(encoding="utf-8")) is True


def test_update_file_with_existing_frontmatter(temp_posts_dir):
    """--update 模式下替换已有 frontmatter。"""
    content = "---\ntitle: 旧标题\ndate: 2026-01-01\ntags: [旧]\n---\n\n# 新标题\n\n正文"
    md_file = temp_posts_dir / "技术" / "old.md"
    md_file.write_text(content, encoding="utf-8")

    # 模拟更新逻辑
    new_fm = fm.format_frontmatter({"title": "新标题", "date": "2026-06-03", "tags": ["新"]})
    import re as re_mod
    body = re_mod.sub(r"^---\n.*?---\n", "", content, count=1, flags=re_mod.DOTALL)
    new_content = new_fm + "\n" + body.lstrip("\n")
    md_file.write_text(new_content, encoding="utf-8")

    result = md_file.read_text(encoding="utf-8")
    assert result.startswith("---\n")
    assert "title: 新标题" in result
    assert "旧标题" not in result
    assert "# 新标题" in result


def test_process_all_md_files_recursively(temp_posts_dir):
    """递归处理所有 .md 文件。"""
    (temp_posts_dir / "技术" / "a.md").write_text("# A\n\n内容A", encoding="utf-8")
    (temp_posts_dir / "生活" / "b.md").write_text("# B\n\n内容B", encoding="utf-8")
    # 嵌套子目录
    nested = temp_posts_dir / "技术" / "子目录"
    nested.mkdir(parents=True)
    (nested / "c.md").write_text("# C\n\n内容C", encoding="utf-8")

    md_files = sorted(temp_posts_dir.rglob("*.md"))
    assert len(md_files) == 3

    llm_return = {"title": "T", "date": "2026-06-01", "tags": ["t"]}
    config = make_config()

    for md_file in md_files:
        with mock.patch.object(fm, "call_llm", return_value=llm_return):
            content = md_file.read_text(encoding="utf-8")
            metadata = fm.generate_metadata(content, md_file, config, _null_log)
            new_content = fm.format_frontmatter(metadata) + "\n" + content
            md_file.write_text(new_content, encoding="utf-8")

    for md_file in md_files:
        assert fm.has_frontmatter(md_file.read_text(encoding="utf-8"))


# ============================================================
# extract_date_from_mtime
# ============================================================

# ============================================================
# process_directory
# ============================================================

def test_process_directory_adds_frontmatter(temp_posts_dir):
    """process_directory 处理整个目录。"""
    (temp_posts_dir / "技术" / "2026-06-01-guide.md").write_text("# 测试\n\n内容", encoding="utf-8")
    (temp_posts_dir / "生活" / "2026-06-02-life.md").write_text("# 生活\n\n内容", encoding="utf-8")

    config = make_config()
    llm_return = {"title": "T", "date": "2026-06-01", "tags": ["x"]}

    with mock.patch.object(fm, "call_llm", return_value=llm_return):
        counts = fm.process_directory(temp_posts_dir, config, _null_log, dry_run=False, update=False)

    assert counts["processed"] == 2
    assert counts["skipped"] == 0

    # 验证文件已修改
    for md_file in temp_posts_dir.rglob("*.md"):
        assert fm.has_frontmatter(md_file.read_text(encoding="utf-8"))


def test_process_directory_skips_existing(temp_posts_dir):
    """process_directory 跳过已有 frontmatter 的文件。"""
    content = "---\ntitle: x\ndate: 2026-01-01\ntags: [x]\n---\n\n正文"
    (temp_posts_dir / "技术" / "existing.md").write_text(content, encoding="utf-8")

    config = make_config()

    with mock.patch.object(fm, "call_llm") as mock_llm:
        counts = fm.process_directory(temp_posts_dir, config, _null_log, dry_run=False, update=False)

    assert counts["skipped"] == 1
    assert counts["processed"] == 0
    mock_llm.assert_not_called()


def test_process_directory_update_mode(temp_posts_dir):
    """process_directory --update 替换已有 frontmatter。"""
    content = "---\ntitle: 旧\ndate: 2026-01-01\ntags: [旧]\n---\n\n# 新标题\n\n正文"
    (temp_posts_dir / "技术" / "old.md").write_text(content, encoding="utf-8")

    config = make_config()
    llm_return = {"title": "新标题", "date": "2026-06-03", "tags": ["新"]}

    with mock.patch.object(fm, "call_llm", return_value=llm_return):
        counts = fm.process_directory(temp_posts_dir, config, _null_log, dry_run=False, update=True)

    assert counts["updated"] == 1
    result = (temp_posts_dir / "技术" / "old.md").read_text(encoding="utf-8")
    assert "title: 新标题" in result
    assert "旧标题" not in result


def test_process_directory_dry_run_no_write(temp_posts_dir):
    """dry_run 模式不实际写入。"""
    (temp_posts_dir / "技术" / "test.md").write_text("# 测试\n\n内容", encoding="utf-8")

    config = make_config()
    llm_return = {"title": "T", "date": "2026-06-01", "tags": ["x"]}

    with mock.patch.object(fm, "call_llm", return_value=llm_return):
        counts = fm.process_directory(temp_posts_dir, config, _null_log, dry_run=True, update=False)

    assert counts["processed"] == 1
    result = (temp_posts_dir / "技术" / "test.md").read_text(encoding="utf-8")
    assert not result.startswith("---")  # 未被修改


def test_process_directory_nonexistent():
    """不存在的目录应返回全零计数。"""
    config = make_config()
    counts = fm.process_directory(Path("/no/such/dir"), config, _null_log, dry_run=False, update=False)
    assert counts == {"processed": 0, "skipped": 0, "updated": 0, "errors": 0}


def test_process_directory_skips_short_content(temp_posts_dir):
    """内容过短的文件应被跳过。"""
    (temp_posts_dir / "技术" / "short.md").write_text("# T\n\nx", encoding="utf-8")  # ~5 chars

    config = make_config()
    config["min_content_length"] = 200

    with mock.patch.object(fm, "call_llm") as mock_llm:
        counts = fm.process_directory(temp_posts_dir, config, _null_log, dry_run=False, update=False)

    assert counts["skipped"] == 1
    assert counts["processed"] == 0
    mock_llm.assert_not_called()


def test_process_directory_skips_draft_marker(temp_posts_dir):
    """包含草稿标记的文件应被跳过。"""
    content = "<!-- draft -->\n# 还在写\n\n未完待续...\n" + "x" * 200
    (temp_posts_dir / "技术" / "draft.md").write_text(content, encoding="utf-8")

    config = make_config()
    config["draft_markers"] = ["<!-- draft -->"]

    with mock.patch.object(fm, "call_llm") as mock_llm:
        counts = fm.process_directory(temp_posts_dir, config, _null_log, dry_run=False, update=False)

    assert counts["skipped"] == 1
    assert counts["processed"] == 0
    mock_llm.assert_not_called()


def test_process_directory_updates_missing_tags(temp_posts_dir):
    """已有 frontmatter 但缺少 tags 的文件 → 即使不带 --update 也触发更新。"""
    content = "---\ntitle: 某文章\ndate: 2026-01-01\ntags: []\n---\n\n# 某文章\n\n" + "x" * 200
    (temp_posts_dir / "技术" / "notags.md").write_text(content, encoding="utf-8")

    config = make_config()
    llm_return = {"title": "某文章", "date": "2026-01-01", "tags": ["技术", "教程"]}

    with mock.patch.object(fm, "call_llm", return_value=llm_return):
        counts = fm.process_directory(temp_posts_dir, config, _null_log, dry_run=False, update=False)

    assert counts["updated"] == 1
    result = (temp_posts_dir / "技术" / "notags.md").read_text(encoding="utf-8")
    assert "tags: [技术, 教程]" in result


def test_process_directory_skips_complete_frontmatter(temp_posts_dir):
    """已有完整 frontmatter（含非空 tags）的文件正常跳过。"""
    content = "---\ntitle: 完整\ndate: 2026-01-01\ntags: [技术]\n---\n\n" + "x" * 200
    (temp_posts_dir / "技术" / "complete.md").write_text(content, encoding="utf-8")

    config = make_config()

    with mock.patch.object(fm, "call_llm") as mock_llm:
        counts = fm.process_directory(temp_posts_dir, config, _null_log, dry_run=False, update=False)

    assert counts["skipped"] == 1
    assert counts["processed"] == 0
    mock_llm.assert_not_called()


def test_process_directory_draft_overrides_missing_tags(temp_posts_dir):
    """草稿标记优先级高于缺少 tags：即使 tags 为空，有 draft 标记也跳过。"""
    content = "<!-- draft -->\n---\ntitle: x\ndate: 2026-01-01\ntags: []\n---\n\n" + "x" * 200
    (temp_posts_dir / "技术" / "draft_notags.md").write_text(content, encoding="utf-8")

    config = make_config()
    config["draft_markers"] = ["<!-- draft -->"]

    with mock.patch.object(fm, "call_llm") as mock_llm:
        counts = fm.process_directory(temp_posts_dir, config, _null_log, dry_run=False, update=False)

    assert counts["skipped"] == 1
    mock_llm.assert_not_called()


# ============================================================
# get_config WATCH_PATHS
# ============================================================

def test_get_config_watch_paths_empty():
    """未设置 WATCH_PATHS 时返回空列表。"""
    with mock.patch("main.load_dotenv"):  # 阻止读取 .env 文件
        with mock.patch.dict(os.environ, {"API_BASE": "b", "API_KEY": "k", "MODEL": "m"}, clear=True):
            cfg = fm.get_config()
    assert cfg["watch_paths"] == []


def test_get_config_watch_paths_parsed():
    """WATCH_PATHS 按逗号分割并去空格。"""
    env = {
        "API_BASE": "b", "API_KEY": "k", "MODEL": "m",
        "WATCH_PATHS": "  /a/b  , /c/d , /e/f ",
    }
    with mock.patch("main.load_dotenv"):  # 阻止读取 .env 文件
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = fm.get_config()
    assert cfg["watch_paths"] == ["/a/b", "/c/d", "/e/f"]


def test_get_config_watch_paths_expands_tilde():
    """WATCH_PATHS 中的 ~ 展开为用户主目录。"""
    env = {
        "API_BASE": "b", "API_KEY": "k", "MODEL": "m",
        "WATCH_PATHS": "~/proj/posts",
    }
    with mock.patch("main.load_dotenv"):  # 阻止读取 .env 文件
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = fm.get_config()
    assert cfg["watch_paths"] == [os.path.expanduser("~/proj/posts")]
    assert not cfg["watch_paths"][0].startswith("~")


def test_get_config_draft_markers_default():
    """未设置 DRAFT_MARKERS 时使用默认值。"""
    env = {"API_BASE": "b", "API_KEY": "k", "MODEL": "m"}
    with mock.patch("main.load_dotenv"):
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = fm.get_config()
    assert cfg["draft_markers"] == ["<!-- draft -->", "<!-- wip -->"]


def test_get_config_draft_markers_custom():
    """自定义 DRAFT_MARKERS 逗号分隔。"""
    env = {
        "API_BASE": "b", "API_KEY": "k", "MODEL": "m",
        "DRAFT_MARKERS": "<!-- draft -->, <!-- todo -->,<!-- wip -->",
    }
    with mock.patch("main.load_dotenv"):
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = fm.get_config()
    assert cfg["draft_markers"] == ["<!-- draft -->", "<!-- todo -->", "<!-- wip -->"]


def test_get_config_min_content_length_default():
    """未设置 MIN_CONTENT_LENGTH 时默认为 200。"""
    env = {"API_BASE": "b", "API_KEY": "k", "MODEL": "m"}
    with mock.patch("main.load_dotenv"):
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = fm.get_config()
    assert cfg["min_content_length"] == 200


def test_get_config_min_content_length_custom():
    """自定义 MIN_CONTENT_LENGTH。"""
    env = {
        "API_BASE": "b", "API_KEY": "k", "MODEL": "m",
        "MIN_CONTENT_LENGTH": "500",
    }
    with mock.patch("main.load_dotenv"):
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = fm.get_config()
    assert cfg["min_content_length"] == 500


# ============================================================
# extract_date_from_mtime
# ============================================================
def test_extract_date_from_mtime():
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        f.write(b"test")
        path = f.name

    try:
        result = fm.extract_date_from_mtime(Path(path))
        # 应该返回今天的日期 YYYY-MM-DD
        import datetime
        today = datetime.date.today().isoformat()
        assert result == today
    finally:
        os.unlink(path)

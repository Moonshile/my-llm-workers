import logging
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

# 支持从项目根目录运行（pytest）和从 filesync 目录直接运行
try:
    from filesync import main as fs
except ImportError:
    import main as fs  # type: ignore[no-redef]

# 测试用静默 logger
_test_log = logging.getLogger("test")
_test_log.addHandler(logging.NullHandler())


# ============================================================
# expand_path
# ============================================================

def test_expand_path_home():
    """~ 展开为用户 home 目录。"""
    result = fs.expand_path("~/test/file.txt")
    assert str(result).startswith(str(Path.home()))
    assert result.is_absolute()


def test_expand_path_relative():
    """相对路径解析为绝对路径。"""
    result = fs.expand_path("foo/bar.txt")
    assert result.is_absolute()


def test_expand_path_absolute():
    """绝对路径 resolve 后保持绝对。"""
    result = fs.expand_path("/tmp/foo.txt")
    # macOS 上 /tmp 是 /private/tmp 的符号链接，resolve() 会展开
    assert result == Path("/tmp/foo.txt").resolve()


# ============================================================
# file_info
# ============================================================

def test_file_info():
    """读取文件元信息。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("hello world")
        tmp = Path(f.name)

    try:
        info = fs.file_info(tmp)
        assert info["size"] == 11
        assert len(info["hash"]) == 64  # SHA256
        assert info["content"] == b"hello world"
        assert info["mtime"] > 0
    finally:
        tmp.unlink()


# ============================================================
# sync_group
# ============================================================

class TestSyncGroup:
    """sync_group 函数测试。"""

    def _create_file(self, base: Path, name: str, content: str,
                      age_sec: float = 0, mtime: float | None = None):
        """在 base 下创建文件。

        age_sec: 多少秒前修改（与 mtime 互斥，后者优先级更高）
        mtime: 显式指定修改时间戳
        """
        p = base / name
        p.write_text(content, encoding="utf-8")
        ts = mtime if mtime is not None else time.time() - age_sec
        os.utime(p, (ts, ts))
        return p

    def test_basic_sync_newer_overwrites_older(self, tmp_path):
        """最新 mtime 的文件覆盖旧文件。"""
        a = self._create_file(tmp_path, "a.txt", "new content", age_sec=0)
        b = self._create_file(tmp_path, "b.txt", "old content", age_sec=10)

        group = {"name": "test", "paths": [str(a), str(b)]}
        with (
            mock.patch.object(fs.logger, "info") as mock_info,
            mock.patch.object(fs, "BACKUP_DIR", tmp_path / "backups"),
        ):
            result = fs.sync_group(group)
            assert result is True
            # 检查 info 日志中包含 diff
            info_calls = [c[0][0] for c in mock_info.call_args_list if c[0]]
            diff_log = "\n".join(info_calls)
            assert "old content" in diff_log
            assert "new content" in diff_log

        # b 内容被 a 覆盖
        assert b.read_text() == "new content"

    def test_already_in_sync_no_action(self, tmp_path):
        """内容已一致则跳过。"""
        a = self._create_file(tmp_path, "a.txt", "same", age_sec=0)
        b = self._create_file(tmp_path, "b.txt", "same", age_sec=5)

        group = {"name": "test", "paths": [str(a), str(b)]}
        result = fs.sync_group(group)
        assert result is False

    def test_conflict_same_mtime_different_content(self, tmp_path):
        """相同 mtime 但内容不同 → 冲突，跳过。"""
        fixed_mtime = time.time() - 100  # 使用固定时间戳确保 mtime 一致
        a = self._create_file(tmp_path, "a.txt", "content A", mtime=fixed_mtime)
        b = self._create_file(tmp_path, "b.txt", "content B", mtime=fixed_mtime)

        group = {"name": "test", "paths": [str(a), str(b)]}
        with mock.patch.object(fs.logger, "warning") as mock_warn:
            result = fs.sync_group(group)
            assert result is False
            # 应有冲突警告
            warn_msgs = [c[0][0] for c in mock_warn.call_args_list if c[0]]
            assert any("冲突" in m for m in warn_msgs)

        # 两个文件内容均未变
        assert a.read_text() == "content A"
        assert b.read_text() == "content B"

    def test_dry_run_no_actual_copy(self, tmp_path):
        """dry-run 模式不实际修改文件。"""
        a = self._create_file(tmp_path, "a.txt", "new", age_sec=0)
        b = self._create_file(tmp_path, "b.txt", "old", age_sec=10)

        group = {"name": "test", "paths": [str(a), str(b)]}
        result = fs.sync_group(group, dry_run=True)
        assert result is True
        # b 保持原内容
        assert b.read_text() == "old"

    def test_missing_file_created(self, tmp_path):
        """缺失的文件自动创建（含父目录）。"""
        a = self._create_file(tmp_path, "a.txt", "new content", age_sec=0)
        missing_dir = tmp_path / "subdir"
        missing = missing_dir / "missing.txt"

        group = {"name": "test", "paths": [str(a), str(missing)]}
        with (
            mock.patch.object(fs.logger, "info") as mock_info,
            mock.patch.object(fs, "BACKUP_DIR", tmp_path / "backups"),
        ):
            result = fs.sync_group(group)
            assert result is True
            info_msgs = [c[0][0] for c in mock_info.call_args_list if c[0]]
            log_text = "\n".join(info_msgs)
            assert "创建" in log_text

        # 文件已创建且内容正确
        assert missing.exists()
        assert missing.read_text() == "new content"

    def test_all_files_missing(self, tmp_path):
        """所有文件都不存在 → 警告并跳过。"""
        missing1 = str(tmp_path / "a.txt")
        missing2 = str(tmp_path / "b.txt")

        group = {"name": "test", "paths": [missing1, missing2]}
        with mock.patch.object(fs.logger, "warning") as mock_warn:
            result = fs.sync_group(group)
            assert result is False
            warn_msgs = [c[0][0] for c in mock_warn.call_args_list if c[0]]
            assert any("没有可用文件" in m for m in warn_msgs)

    def test_one_exists_one_missing_creates(self, tmp_path):
        """一个文件存在一个缺失 → 从存在的文件创建缺失的。"""
        a = self._create_file(tmp_path, "a.txt", "source", age_sec=0)
        missing = tmp_path / "deeply" / "nested" / "b.txt"

        group = {"name": "test", "paths": [str(a), str(missing)]}
        with mock.patch.object(fs, "BACKUP_DIR", tmp_path / "backups"):
            result = fs.sync_group(group)
        assert result is True
        assert missing.exists()
        assert missing.read_text() == "source"

    def test_three_files_sync_to_latest(self, tmp_path):
        """三个文件，最新覆盖其余两个。"""
        a = self._create_file(tmp_path, "a.txt", "latest", age_sec=0)
        b = self._create_file(tmp_path, "b.txt", "old1", age_sec=5)
        c = self._create_file(tmp_path, "c.txt", "old2", age_sec=10)

        group = {"name": "test", "paths": [str(a), str(b), str(c)]}
        with mock.patch.object(fs, "BACKUP_DIR", tmp_path / "backups"):
            result = fs.sync_group(group)
        assert result is True
        assert b.read_text() == "latest"
        assert c.read_text() == "latest"

    def test_group_without_name_defaults(self, tmp_path):
        """没有 name 字段时使用 'unnamed'。"""
        a = self._create_file(tmp_path, "a.txt", "new", age_sec=0)
        b = self._create_file(tmp_path, "b.txt", "old", age_sec=10)

        group = {"paths": [str(a), str(b)]}
        with (
            mock.patch.object(fs.logger, "info") as mock_info,
            mock.patch.object(fs, "BACKUP_DIR", tmp_path / "backups"),
        ):
            result = fs.sync_group(group)
            assert result is True
            info_msgs = [c[0][0] for c in mock_info.call_args_list if c[0]]
            assert any("unnamed" in m for m in info_msgs)

    def test_fewer_than_two_paths(self, tmp_path):
        """路径少于 2 个 → 跳过。"""
        a = self._create_file(tmp_path, "a.txt", "content", age_sec=0)
        group = {"name": "test", "paths": [str(a)]}
        result = fs.sync_group(group)
        assert result is False

    def test_empty_paths_list(self):
        """空路径列表 → 跳过。"""
        group = {"name": "test", "paths": []}
        result = fs.sync_group(group)
        assert result is False

    def test_binary_file_diff_fallback(self, tmp_path):
        """二进制文件的 diff 降级处理。"""
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"\x00\x01\x02")
        b.write_bytes(b"\xff\xfe\xfd")
        # a 更新
        os.utime(a, (time.time(), time.time()))
        os.utime(b, (time.time() - 10, time.time() - 10))

        group = {"name": "test", "paths": [str(a), str(b)]}
        with (
            mock.patch.object(fs.logger, "info") as mock_info,
            mock.patch.object(fs, "BACKUP_DIR", tmp_path / "backups"),
        ):
            result = fs.sync_group(group)
            assert result is True
            info_msgs = [c[0][0] for c in mock_info.call_args_list if c[0]]
            diff_log = "\n".join(info_msgs)
            assert "二进制" in diff_log or "binary" in diff_log.lower()


# ============================================================
# expand_group
# ============================================================

class TestExpandGroup:
    """expand_group 函数测试 — 目录级同步展开。"""

    def _create_dir_with_files(self, base: Path, dirname: str,
                                files: dict[str, str],
                                ages: dict[str, float] | None = None):
        """在 base 下创建目录并填充文件。返回目录 Path。"""
        d = base / dirname
        d.mkdir(parents=True, exist_ok=True)
        for fname, content in files.items():
            f = d / fname
            f.write_text(content, encoding="utf-8")
            age = (ages or {}).get(fname, 0)
            os.utime(f, (time.time() - age, time.time() - age))
        return d

    def test_no_pattern_returns_original(self):
        """无 pattern 时原样返回。"""
        group = {"name": "test", "paths": ["/tmp/a.txt", "/tmp/b.txt"]}
        result = fs.expand_group(group)
        assert result == [group]

    def test_basic_directory_expansion(self, tmp_path):
        """2 个目录各 2 个文件，展开为 2 个子组。"""
        dir1 = self._create_dir_with_files(tmp_path, "dir1",
            {"a.md": "v1", "b.md": "v1"})
        dir2 = self._create_dir_with_files(tmp_path, "dir2",
            {"a.md": "v2", "b.md": "v2"})

        group = {
            "name": "docs",
            "pattern": "*.md",
            "paths": [str(dir1), str(dir2)],
        }
        result = fs.expand_group(group)

        assert len(result) == 2
        names = {g["name"] for g in result}
        assert names == {"docs/a.md", "docs/b.md"}

        for g in result:
            assert len(g["paths"]) == 2
            # 每个子组的路径指向对应文件
            assert any("dir1" in p for p in g["paths"])
            assert any("dir2" in p for p in g["paths"])

    def test_missing_file_in_one_dir(self, tmp_path):
        """某文件只在一个目录存在时，缺失目录用占位路径。"""
        dir1 = self._create_dir_with_files(tmp_path, "src",
            {"shared.md": "v1", "only_here.md": "v1"})
        dir2 = self._create_dir_with_files(tmp_path, "dst",
            {"shared.md": "v2"})
        # only_here.md 不在 dir2

        group = {
            "name": "docs",
            "pattern": "*.md",
            "paths": [str(dir1), str(dir2)],
        }
        result = fs.expand_group(group)

        assert len(result) == 2
        # only_here.md 子组：dir2 的路径应为占位路径
        only_group = [g for g in result if g["name"] == "docs/only_here.md"][0]
        dst_path = [p for p in only_group["paths"] if "dst" in p][0]
        assert dst_path.endswith("only_here.md")

    def test_invalid_directory_warning(self, tmp_path):
        """目录不存在时记录 WARNING。"""
        dir1 = self._create_dir_with_files(tmp_path, "dir1", {"a.md": "v1"})
        missing_dir = tmp_path / "nonexistent"

        group = {
            "name": "docs",
            "pattern": "*.md",
            "paths": [str(dir1), str(missing_dir)],
        }
        with mock.patch.object(fs.logger, "warning") as mock_warn:
            result = fs.expand_group(group)
            assert result == []  # 有效目录少于 2
            warn_msgs = [c[0][0] for c in mock_warn.call_args_list if c[0]]
            assert any("不是目录或不存在" in m for m in warn_msgs)

    def test_fewer_than_two_valid_dirs(self, tmp_path):
        """有效目录少于 2 时返回空列表。"""
        dir1 = self._create_dir_with_files(tmp_path, "dir1", {"a.md": "v1"})

        group = {
            "name": "docs",
            "pattern": "*.md",
            "paths": [str(dir1)],
        }
        with mock.patch.object(fs.logger, "warning") as mock_warn:
            result = fs.expand_group(group)
            assert result == []
            warn_msgs = [c[0][0] for c in mock_warn.call_args_list if c[0]]
            assert any("少于 2" in m for m in warn_msgs)

    def test_pattern_no_match(self, tmp_path):
        """pattern 无匹配文件时返回空列表。"""
        dir1 = self._create_dir_with_files(tmp_path, "dir1", {"a.txt": "v1"})
        dir2 = self._create_dir_with_files(tmp_path, "dir2", {"b.txt": "v1"})

        group = {
            "name": "docs",
            "pattern": "*.md",  # 无 .md 文件
            "paths": [str(dir1), str(dir2)],
        }
        with mock.patch.object(fs.logger, "warning") as mock_warn:
            result = fs.expand_group(group)
            assert result == []
            warn_msgs = [c[0][0] for c in mock_warn.call_args_list if c[0]]
            assert any("未匹配到任何文件" in m for m in warn_msgs)

    def test_empty_paths(self):
        """空 paths 返回空列表。"""
        group = {"name": "docs", "pattern": "*.md", "paths": []}
        with mock.patch.object(fs.logger, "warning") as mock_warn:
            result = fs.expand_group(group)
            assert result == []
            warn_msgs = [c[0][0] for c in mock_warn.call_args_list if c[0]]
            assert any("少于 2" in m for m in warn_msgs)

    def test_group_without_name_defaults(self, tmp_path):
        """没有 name 字段时使用 'unnamed'。"""
        dir1 = self._create_dir_with_files(tmp_path, "dir1", {"a.md": "v1"})
        dir2 = self._create_dir_with_files(tmp_path, "dir2", {"a.md": "v2"})

        group = {"pattern": "*.md", "paths": [str(dir1), str(dir2)]}
        with mock.patch.object(fs.logger, "info") as mock_info:
            result = fs.expand_group(group)
            assert len(result) == 1
            assert result[0]["name"] == "unnamed/a.md"
            # 确认目录同步 info 日志包含 unnamed
            info_msgs = [c[0][0] for c in mock_info.call_args_list if c[0]]
            assert any("unnamed" in m for m in info_msgs)


# ============================================================
# 目录同步集成测试
# ============================================================

class TestDirectorySyncIntegration:
    """端到端目录同步测试。"""

    def test_full_directory_sync(self, tmp_path):
        """完整目录同步流程：2 目录 2 文件，同步 + 备份验证。"""
        # dir1 中 a.md 为最新，dir2 中 b.md 为最新
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        # dir1: a.md 最新, b.md 旧
        a1 = dir1 / "a.md"
        b1 = dir1 / "b.md"
        a1.write_text("a from dir1 latest")
        b1.write_text("b old in dir1")
        # dir2: a.md 旧, b.md 最新
        a2 = dir2 / "a.md"
        b2 = dir2 / "b.md"
        a2.write_text("a old in dir2")
        b2.write_text("b from dir2 latest")

        os.utime(a1, (time.time(), time.time()))              # 最新
        os.utime(b1, (time.time() - 10, time.time() - 10))   # 旧
        os.utime(a2, (time.time() - 10, time.time() - 10))   # 旧
        os.utime(b2, (time.time(), time.time()))              # 最新

        group = {
            "name": "cli",
            "pattern": "*.md",
            "paths": [str(dir1), str(dir2)],
        }

        with (
            mock.patch.object(fs, "BACKUP_DIR", tmp_path / "backups"),
        ):
            sub_groups = fs.expand_group(group)
            synced = 0
            for sub in sub_groups:
                if fs.sync_group(sub):
                    synced += 1

        # a 组：dir1 最新 → 覆盖 dir2
        # b 组：dir2 最新 → 覆盖 dir1
        assert synced == 2
        assert a2.read_text() == "a from dir1 latest"
        assert b1.read_text() == "b from dir2 latest"

        # 验证备份
        backups = list((tmp_path / "backups").iterdir())
        assert len(backups) >= 2

    def test_directory_sync_creates_missing(self, tmp_path):
        """某目录缺少文件时自动创建。"""
        dir1 = tmp_path / "src"
        dir2 = tmp_path / "dst"
        dir1.mkdir()
        dir2.mkdir()

        # dir1 有 new.md，dir2 没有
        new1 = dir1 / "new.md"
        new1.write_text("new content")
        os.utime(new1, (time.time(), time.time()))

        group = {
            "name": "create-test",
            "pattern": "*.md",
            "paths": [str(dir1), str(dir2)],
        }

        with mock.patch.object(fs, "BACKUP_DIR", tmp_path / "backups"):
            sub_groups = fs.expand_group(group)
            for sub in sub_groups:
                fs.sync_group(sub)

        new2 = dir2 / "new.md"
        assert new2.exists()
        assert new2.read_text() == "new content"

    def test_directory_sync_dry_run(self, tmp_path):
        """dry-run 模式不实际修改文件。"""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        a1 = dir1 / "a.md"
        a2 = dir2 / "a.md"
        a1.write_text("new")
        a2.write_text("old")
        os.utime(a1, (time.time(), time.time()))
        os.utime(a2, (time.time() - 10, time.time() - 10))

        group = {
            "name": "dry-test",
            "pattern": "*.md",
            "paths": [str(dir1), str(dir2)],
        }

        sub_groups = fs.expand_group(group)
        for sub in sub_groups:
            fs.sync_group(sub, dry_run=True)

        # dir2 内容不变
        assert a2.read_text() == "old"


# ============================================================
# _safe_path
# ============================================================

def test_safe_path_absolute():
    """绝对路径 / 替换为 _。"""
    assert fs._safe_path(Path("/Users/test/file.txt")) == "Users_test_file.txt"


def test_safe_path_relative():
    """相对路径无前导 /。"""
    assert fs._safe_path(Path("foo/bar.txt")) == "foo_bar.txt"


# ============================================================
# _parse_date_from_filename
# ============================================================

@pytest.mark.parametrize("filename,expected", [
    ("20260604-143052_zshrc__Users_test_.zshrc.bak", datetime(2026, 6, 4)),
    ("20260101-000000_test.bak", datetime(2026, 1, 1)),
    ("20261231-235959_x.bak", datetime(2026, 12, 31)),
    ("invalid.bak", None),
    ("2026-06-04_test.bak", None),  # 格式不对
    ("", None),
])
def test_parse_date_from_filename(filename, expected):
    assert fs._parse_date_from_filename(filename) == expected


# ============================================================
# backup_file
# ============================================================

def test_backup_file_creates_backup(tmp_path):
    """备份文件创建成功，文件名包含日期和路径。"""
    # 创建原始文件
    src = tmp_path / "original.txt"
    src.write_text("hello backup", encoding="utf-8")

    with mock.patch.object(fs, "BACKUP_DIR", tmp_path / "backups"):
        result = fs.backup_file(src, "test-group")

    assert result is not None
    assert result.exists()
    assert result.name.startswith("20")  # 以年份开头
    assert "test-group" in result.name
    assert "original.txt" in result.name
    assert result.name.endswith(".bak")
    # 备份内容与原始一致
    assert result.read_text() == "hello backup"


# ============================================================
# cleanup_backups
# ============================================================

def test_cleanup_backups_removes_old(tmp_path):
    """删除超过指定天数的旧备份。"""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    # 创建 3 个备份：40 天前、20 天前、5 天前
    now = datetime.now()
    old = (now - timedelta(days=40)).strftime("%Y%m%d-120000_old.bak")
    mid = (now - timedelta(days=20)).strftime("%Y%m%d-120000_mid.bak")
    new = (now - timedelta(days=5)).strftime("%Y%m%d-120000_new.bak")

    (backup_dir / old).write_text("old")
    (backup_dir / mid).write_text("mid")
    (backup_dir / new).write_text("new")

    with mock.patch.object(fs, "BACKUP_DIR", backup_dir):
        deleted = fs.cleanup_backups(backup_days=30)

    assert deleted == 1  # 只删除 40 天前的
    assert not (backup_dir / old).exists()
    assert (backup_dir / mid).exists()
    assert (backup_dir / new).exists()


def test_cleanup_backups_no_dir():
    """备份目录不存在时返回 0。"""
    with mock.patch.object(fs, "BACKUP_DIR", Path("/nonexistent/backups")):
        assert fs.cleanup_backups() == 0


def test_cleanup_backups_skips_non_bak(tmp_path):
    """非 .bak 文件不会被删除。"""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    other = backup_dir / "readme.txt"
    other.write_text("keep me")

    with mock.patch.object(fs, "BACKUP_DIR", backup_dir):
        deleted = fs.cleanup_backups(backup_days=1)

    assert deleted == 0
    assert other.exists()


# ============================================================
# load_config
# ============================================================

def test_load_config_not_found():
    """配置文件不存在时 sys.exit(1)。"""
    with mock.patch.object(fs, "CONFIG_FILE", Path("/nonexistent/config.yaml")):
        with pytest.raises(SystemExit) as exc:
            fs.load_config()
        assert exc.value.code == 1


def test_load_config_valid(tmp_path):
    """正确加载 YAML 配置。"""
    config_file = tmp_path / ".filesync.yaml"
    config_file.write_text(
        "groups:\n"
        "  - name: test\n"
        "    paths:\n"
        "      - /tmp/a.txt\n"
        "      - /tmp/b.txt\n",
        encoding="utf-8",
    )
    with mock.patch.object(fs, "CONFIG_FILE", config_file):
        config = fs.load_config()
        assert len(config["groups"]) == 1
        assert config["groups"][0]["name"] == "test"


# ============================================================
# 集成测试
# ============================================================

class TestIntegration:
    """端到端集成测试。"""

    def test_full_sync_flow(self, tmp_path, monkeypatch):
        """完整同步流程（含备份验证）。"""
        # 创建源文件
        src = tmp_path / "src.txt"
        dst1 = tmp_path / "dst1.txt"
        dst2 = tmp_path / "dst2.txt"

        src.write_text("version 2", encoding="utf-8")
        dst1.write_text("version 1", encoding="utf-8")
        dst2.write_text("version 1", encoding="utf-8")
        # src 是最新的
        os.utime(src, (time.time(), time.time()))
        os.utime(dst1, (time.time() - 5, time.time() - 5))
        os.utime(dst2, (time.time() - 10, time.time() - 10))

        # 模拟配置文件 + 备份目录
        config_file = tmp_path / ".filesync.yaml"
        config_file.write_text(
            f"groups:\n"
            f"  - name: integration-test\n"
            f"    paths:\n"
            f"      - {src}\n"
            f"      - {dst1}\n"
            f"      - {dst2}\n",
            encoding="utf-8",
        )

        with (
            mock.patch.object(fs, "CONFIG_FILE", config_file),
            mock.patch.object(fs, "BACKUP_DIR", tmp_path / "backups"),
        ):
            config = fs.load_config()
            group = config["groups"][0]
            result = fs.sync_group(group)
            assert result is True

        # 验证同步结果
        assert dst1.read_text() == "version 2"
        assert dst2.read_text() == "version 2"

        # 验证备份已创建
        backups = list((tmp_path / "backups").iterdir())
        assert len(backups) >= 2  # dst1 和 dst2 各一个备份
        for b in backups:
            assert b.name.endswith(".bak")
            assert "integration-test" in b.name

    def test_full_sync_already_consistent(self, tmp_path):
        """已一致时不同步。"""
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("same")
        b.write_text("same")

        config_file = tmp_path / ".filesync.yaml"
        config_file.write_text(
            f"groups:\n"
            f"  - name: consistent\n"
            f"    paths:\n"
            f"      - {a}\n"
            f"      - {b}\n",
            encoding="utf-8",
        )

        with mock.patch.object(fs, "CONFIG_FILE", config_file):
            config = fs.load_config()
            group = config["groups"][0]
            result = fs.sync_group(group)
            assert result is False

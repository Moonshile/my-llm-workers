# git-stats

统计指定目录下所有 git 仓库中指定作者的提交记录，按天/周汇总。

支持增量运行：首次扫描全部历史，后续运行仅拉取新提交。缓存文件记录每个仓库最后处理的 commit SHA。

## 使用方式

```bash
# 扫描默认目录 (~/code)
uv run python _oneshot/git-stats/main.py

# 指定目录
uv run python _oneshot/git-stats/main.py --root ~/proj

# 增量运行（从缓存之后开始，默认行为）
uv run python _oneshot/git-stats/main.py

# 仅用本地数据（跳过 fetch）
uv run python _oneshot/git-stats/main.py --no-fetch

# 指定日期范围和作者
uv run python _oneshot/git-stats/main.py --since 2026-05-01 --until 2026-06-01 --author Kaiqiang

# 指定仓库
uv run python _oneshot/git-stats/main.py --repos livekit-agent-card vector-server

# JSON 输出（便于管道处理）
uv run python _oneshot/git-stats/main.py --json

# 清除缓存，强制全量重新扫描
uv run python _oneshot/git-stats/main.py --reset-cache

# 仅列出仓库
uv run python _oneshot/git-stats/main.py --list-repos
```

## 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--root` | `~/code` | 要扫描的根目录 |
| `--since` | `2024-10-01` | 起始日期 |
| `--until` | 当天 | 结束日期（exclusive） |
| `--author` | `kaiqiangduan` | 作者匹配字符串 |
| `--no-fetch` | 否 | 跳过 git fetch |
| `--reset-cache` | 否 | 清除缓存，强制全量扫描 |
| `--repos` | 全部 | 指定仓库名列表 |
| `--json` | 否 | JSON 格式输出 |
| `--list-repos` | 否 | 仅列出仓库 |

## 工作机制

1. 扫描目标目录下所有包含 `.git` 的子目录（最多两层）
2. 对每个仓库执行 `git fetch --all`（可通过 `--no-fetch` 跳过）
3. 通过 `git log --all` 遍历所有分支，按作者和日期过滤
4. 按 author date 二次过滤，消除 rebase/cherry-pick 导致的时间偏差
5. 按天/周聚合提交次数，输出统计报告

## 缓存

缓存文件位于 `~/.cache/git-stats-cache.json`，记录每个仓库最后处理的 commit SHA。

- 下次运行时自动从缓存位置继续，仅拉取新提交
- `--reset-cache` 可清除缓存强制全量扫描
- 如果某仓库被 force-push，该仓库的新旧提交均会被重新统计

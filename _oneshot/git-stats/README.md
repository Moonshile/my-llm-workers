# git-stats

统计指定目录下所有 git 仓库中指定作者的提交记录，按天/周汇总。

支持增量运行：首次扫描全部历史，后续运行仅拉取新提交。缓存文件记录每个仓库最后处理的 commit SHA。

每次运行自动生成一份详尽的 GitHub 风格 HTML 报告（热力图 + 周/月柱状图 + 仓库排行 + 每日明细 + 最近提交），并在浏览器中自动打开。

## 使用方式

```bash
# 扫描默认目录 (~/code)，生成报告并自动打开
uv run python _oneshot/git-stats/main.py

# 不自动打开浏览器
uv run python _oneshot/git-stats/main.py --no-open

# 指定输出路径
uv run python _oneshot/git-stats/main.py --output ~/Desktop/commits.html

# 指定目录
uv run python _oneshot/git-stats/main.py --root ~/proj

# 指定日期范围和作者
uv run python _oneshot/git-stats/main.py --since 2026-05-01 --until 2026-06-01 --author Kaiqiang

# JSON 输出（不生成 HTML）
uv run python _oneshot/git-stats/main.py --json

# 清除缓存，强制全量重新扫描
make cache-clear
uv run python _oneshot/git-stats/main.py --reset-cache
```

## 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--root` | `~/code` | 要扫描的根目录 |
| `--since` | `2024-11-01` | 起始日期 |
| `--until` | 当天 | 结束日期（exclusive） |
| `--author` | `kaiqiangduan` | 作者匹配字符串 |
| `--no-fetch` | 否 | 跳过 git fetch |
| `--reset-cache` | 否 | 清除缓存，强制全量扫描 |
| `--repos` | 全部 | 指定仓库名列表 |
| `--json` | 否 | JSON 格式输出（不生成 HTML） |
| `--list-repos` | 否 | 仅列出仓库 |
| `--output` | `report.html` | HTML 报告输出路径 |
| `--no-open` | 否 | 不自动打开 HTML 报告 |

## HTML 报告内容

- **提交热力图** — GitHub 风格的日历热力图，鼠标悬浮显示每日提交数
- **周/月柱状图** — 按周和月聚合的提交分布
- **仓库排行** — 各仓库提交数及占比
- **每日明细** — 每天提交次数及进度条
- **最近提交** — 最近 100 条提交记录（SHA、时间、仓库、信息）
- **统计卡片** — 总提交、活跃天数、日均、最高单日、最长连续天数

## 工作机制

1. 扫描目标目录下所有包含 `.git` 的子目录（最多两层）
2. 对每个仓库执行 `git fetch --all`（可通过 `--no-fetch` 跳过）
3. 通过 `git log --all` 遍历所有分支，按作者和日期过滤
4. 按 author date 二次过滤，消除 rebase/cherry-pick 导致的时间偏差
5. 按天/周聚合提交次数，输出控制台报告 + HTML 报告

## 缓存

缓存文件位于 `~/.cache/git-stats-cache.json`，记录每个仓库最后处理的 commit SHA。

- 下次运行时自动从缓存位置继续，仅拉取新提交
- 统计报告始终展示时间范围内的全部数据
- `make cache-clear` 或 `--reset-cache` 可清除缓存

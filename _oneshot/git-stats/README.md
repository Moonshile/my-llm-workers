# git-stats

统计指定目录下所有 git 仓库中的提交记录，按天/周/月汇总，生成 GitHub 风格 HTML 报告。

- 支持多作者独立统计，每个作者可有多个 git 身份（name/email）
- 增量运行：缓存已处理 commit，下次只拉新提交
- 中国法定节假日 & 调休感知（基于 chinesecalendar）
- 工作时间/非工作时间双维度分析

## 使用方式

```bash
# 默认扫描 ~/code，生成 report.html 并自动打开
uv run python _oneshot/git-stats/main.py

# 多作者（每人可有多组身份模式）
uv run python _oneshot/git-stats/main.py \
  --author kaiqiangduan duankaiqiang \
  --author panjia

# 跳过 fetch + 不打开浏览器
uv run python _oneshot/git-stats/main.py --no-fetch --no-open

# 指定日期范围和目录
uv run python _oneshot/git-stats/main.py --since 2025-06-01 --root ~/proj

# 指定仓库
uv run python _oneshot/git-stats/main.py --repos livekit-agent-card

# JSON 输出（不生成 HTML）
uv run python _oneshot/git-stats/main.py --json

# 清除缓存
make cache-clear
uv run python _oneshot/git-stats/main.py --reset-cache
```

## 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--root` | `~/code` | 要扫描的根目录 |
| `--since` | `2024-11-01` | 起始日期 |
| `--until` | 当天 | 结束日期（exclusive） |
| `--author` | `kaiqiangduan duankaiqiang` | 作者组，可重复：`--author 模式1 模式2 --author 模式3` |
| `--no-fetch` | 否 | 跳过 git fetch |
| `--reset-cache` | 否 | 清除缓存，强制全量扫描 |
| `--repos` | 全部 | 指定仓库名列表 |
| `--json` | 否 | JSON 格式输出（不生成 HTML） |
| `--list-repos` | 否 | 仅列出仓库 |
| `--output` | `_oneshot/git-stats/report.html` | HTML 报告输出路径 |
| `--no-open` | 否 | 不自动打开 HTML 报告 |

## HTML 报告内容

两个 Tab：

| Tab | 内容 |
|------|------|
| 📊 全部提交 | 热力图、仓库排行柱状图、月/周分布、统计卡片、最近提交 |
| ⏰ 非工作时间 | 非工作热力图、按小时分布、仓库排行、统计卡片 |

统计卡片：

- 总提交、活跃天数及占比（分母为首末提交跨度）
- 日均（按活跃日/全部天数）、最高单日
- 最长连续天数、当前连续天数
- 非工作 Tab 额外：工作/非工作提交占比、高峰时段

## 工作时间判定

- 工作日 9:00-20:00 → 工作时间
- 其余（周末、法定节假日、夜间）→ 非工作时间
- 基于 chinesecalendar 库，含中国法定节假日和调休

每年 12 月运行时会提示升级 chinesecalendar 以获取下一年节假日数据：

```bash
uv sync --upgrade-package chinesecalendar
```

## 工作机制

1. 扫描目标目录下所有包含 `.git` 的子目录（最多两层）
2. `git fetch --all` 拉取远端（`--no-fetch` 跳过）
3. `git log --all --author=<regex>` 遍历所有分支，按作者和日期过滤
4. 按 author date 二次过滤（消除 rebase 导致的时间偏差）
5. 提交按 author name/email 归类到对应作者组
6. 按天/周/月聚合，输出控制台报告 + HTML 报告

## 缓存

缓存文件 `~/.cache/git-stats-cache.json`，记录每个仓库最后处理的 commit SHA。

- 统计报告始终展示时间范围内的**全部数据**（非仅增量）
- `--reset-cache` 或 `make cache-clear` 清除缓存
- 仓库被 force-push 后新旧提交均会重新统计

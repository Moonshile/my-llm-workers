# agent-session-journal

扫描 Claude Code session 并生成结构化工作日志。

调度器按 `worker.yaml` 中的 cron 表达式自动执行，默认每天上午 10:00。
分析所有最后修改时间为昨天及之前的 session，通过 LLM（LiteLLM）生成包含
工作概览、高复杂度工作、多轮交互分析、最佳实践等维度的日志文档。
支持增量更新。

## 使用方式

```bash
python main.py                    # 处理所有符合条件的 session
python main.py --dry-run          # 预览模式，仅列出待处理 session
python main.py --session-id <uuid> # 仅处理指定 session
```

## 配置

所有配置通过 `.env` 文件注入（参考 `.env.example`）：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `API_BASE` | LLM API 地址 | 必填 |
| `API_KEY` | API 密钥 | 必填 |
| `MODEL` | LiteLLM 模型标识 | 必填 |
| `OUTPUT_DIR` | 文档输出目录 | `~/Documents/claude-session-journals` |
| `SESSION_DIRS` | 会话扫描目录（逗号分隔） | `~/.claude/sessions,~/.claude/projects` |
| `MAX_CHUNK_CHARS` | 大 session 分块大小（字符） | `8000` |
| `SERIOUS_WORK_PATHS` | 严肃工作路径（逗号分隔） | 无（不启用） |

## 实现逻辑

### 1. Session 发现

- 扫描 `SESSION_DIRS` 配置的目录：
  - `~/.claude/projects/**/*.jsonl`：含完整对话记录的 JSONL 文件
  - `~/.claude/sessions/*.json`：全局 session 元数据
- 排除 `subagents/` 和 `memory/` 子目录
- 按 `(session_id, project_path)` 组合键去重
- 跳过当天修改的 session（mtime 日期 == 今天）

### 2. 增量判断

对于每个 session，先查找 `OUTPUT_DIR` 下是否已有对应的日志文档：

- 按 **frontmatter 中的 `session_id`** 匹配（非文件名），支持文件名变化
- 读取已有文档的 `last_processed_timestamp`
- 若 session mtime ≤ `last_processed_timestamp` → 跳过（无新内容）
- 若已有文档但 session 有更新 → 提取增量部分，进入全量重生成流程

### 3. 对话压缩

从 JSONL 文件中提取并压缩对话内容，减少无关信息：

| 事件类型 | 处理方式 |
| --- | --- |
| `user` | 提取文本（字符串和 content block 列表格式均支持） |
| `assistant` | 仅提取 `text` 块，跳过 `thinking`，标注 `tool_use` 名称 |
| `system` | 有信息量时包含 |
| 元数据事件 | `mode`/`permission-mode`/`file-history-snapshot`/`ai-title`/`last-prompt`/`attachment` 等均跳过 |
| 本地命令 | `<local-command-caveat>`、`<command-name>` 跳过 |

增量模式下仅提取时间戳在 `last_processed_timestamp` 之后的事件。

### 4. 大 Session 分块

当压缩后的对话超过 `MAX_CHUNK_CHARS`（默认 8000 字符）时：

1. 按 ~6000 字符切分为多个块，块间重叠 1000 字符
2. 每块独立调用 LLM 生成结构化部分摘要
3. 所有分块摘要汇总后，再调用一次 LLM 合成为最终文档
4. 合成时合并同类项、去除各分块间的重复内容

### 5. LLM 调用

- 使用 **LiteLLM** 统一接口，通过 `API_BASE`、`API_KEY`、`MODEL` 配置
- 系统提示要求 LLM 严格只返回 JSON（不含 markdown 代码块）
- 每次调用的日志输出：
  - **INFO 日志**：model、prompt 长度、input/output/total tokens、
    cost（如有）
  - **DEBUG 日志**：同上 + 完整请求详情和计费明细
- 失败重试一次，仍失败则记录 error 并跳过该 session

### 6. 增量重生成

- 已有文档时，prompt 中标注「自上次处理后新增的内容」，
  并附已有文档作为参考
- LLM 在已有文档基础上融合增量内容，生成完整的更新版文档
- 全量重生成后覆盖写入，`last_processed_timestamp` 更新为最新时间戳

### 7. 文档生成与输出

LLM 先判断会话复杂度，然后按模式输出：

- **simple 模式**：内容简单、一两句话能讲清的会话。输出简要总结，
  写入当天的**每日简报**文件（`yyyy-mm-dd-daily.md`），同一天同分类的简单
  会话聚合在同一文件中
- **complex 模式**：内容丰富、包含较多决策或反复调试的会话。
  输出完整 5 章节分析，写入独立文件

工具负责：生成 YAML frontmatter、转换正文、确定分类和文件名后写入。
增量时 simple 会话在 daily brief 中按 `session_id` 匹配条目并更新；
complex 会话按独立文件处理。

## 目录分类逻辑

生成的文档放在 `OUTPUT_DIR/<分类>/` 子目录下，分类规则如下：

### 规则 1：严肃工作强制分类

如果 session 的 `project_path` 以 `SERIOUS_WORK_PATHS` 中任一配置路径
为前缀（展开 `~` 后比较），则：

- **分类固定为** `严肃工作`，忽略 LLM 输出的 category
- **标签自动追加** `严肃工作`（已存在则不重复添加）

### 规则 2：LLM 智能分类

非严肃工作的 session，由 LLM 根据对话内容智能判断分类名：

- 优先从 `OUTPUT_DIR` 中**已存在的子目录**里选择匹配的分类
- 若现有分类都不匹配，创建新分类名
- 无法判断时使用 `未分类` 作为 fallback
- 分类名**只能有一级**（不含 `/`），非法分类名归入 `未分类`

### 输出结构示意

```text
OUTPUT_DIR/                     # 由 OUTPUT_DIR 配置
├── 严肃工作/                    # 规则 1 强制分类
│   ├── yyyy-mm-dd-标题slug.md  # complex 独立文件
│   └── yyyy-mm-dd-daily.md     # simple 每日简报
├── 前端开发/                    # 规则 2 LLM 智能分类
│   ├── yyyy-mm-dd-标题slug.md
│   └── yyyy-mm-dd-daily.md
└── 未分类/                     # 规则 2 fallback
```

## 文件命名

格式：`yyyy-mm-dd-标题slug.md`

- `yyyy-mm-dd`：session **创建日期**（从 JSONL 首个事件或全局元数据中的
  `startedAt` 提取），保证文件名不随更新变化
- `标题slug`：由 LLM 生成的 title 转换而来（全小写、特殊字符替换为连字符、
  限长 80 字符）

增量更新时若 title 变化导致文件名变化，删除旧文件后创建新文件。
查找已有文档始终按 frontmatter 中的 `session_id` 匹配，不依赖文件名。

## 日志文档结构

每份文档的 YAML frontmatter：

```yaml
---
title: 由 LLM 生成的描述性标题
date: Session 最后更新日期（来自 mtime）
tags: [标签1, 标签2, ...]        # LLM 生成 + 自动规则（如 严肃工作）
session_id: 00000000-0000-0000-0000-000000000000
project_path: /path/to/project
last_processed_timestamp: 1700000000.000
---
```

**complex 模式**正文包含 5 个标准章节：

**simple 模式**仅包含一个简要总结段落，多个 simple 会话聚合在
`yyyy-mm-dd-daily.md` 文件中，每个会话一个 `## 标题` 小节。

1. **工作概览** — 该 session 中完成的所有工作项
2. **高复杂度工作** — 难度较高的工作，含问题、方案、关键决策
3. **多轮交互分析** — 多轮才解决的问题，分析原因和解决思路
4. **最佳实践** — 可沉淀的经验总结
5. **其他备注** — 其他值得记录的信息（无内容时不生成此章节）

## 日志

遵循项目统一的日志规范，logger 名称为 `agent-session-journal`：

| Handler | 目标 | 级别 | 内容 |
| --- | --- | --- | --- |
| `StreamHandler(sys.stdout)` | 控制台 | INFO | 纯文本，无时间戳 |
| `TimedRotatingFileHandler` | `logs/run.log` | INFO | 每日轮转，含 LLM token/billing |
| `TimedRotatingFileHandler` | `logs/debug.log` | DEBUG | 每日轮转，含 SKIP 和计费明细 |

日志保留天数由 `worker.yaml` 中的 `log_retention_days` 控制，默认 7 天。

## 调度

`worker.yaml` 关键配置：

```yaml
schedule:
  enabled: true
  cron: "0 10 * * *"             # 每天上午 10:00
  job_options:
    misfire_grace_time: 43200    # 12 小时，休眠恢复后补执行
    coalesce: true               # 错过多次只执行最新一次
    max_instances: 1             # 同时只允许一个实例
```

结合 `misfire_grace_time`（12 小时）和 `coalesce: true`，
电脑休眠恢复后可补执行。即使跨多天错过多次也仅触发一次，
而工具逻辑会扫描昨天及之前所有 session，不会丢数据。

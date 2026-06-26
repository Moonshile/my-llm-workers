# token-stats

静态分析 Claude Code session 文件，按模型统计 token 消耗并生成 HTML 报告。纯数据解析，不调用 LLM。

## 使用方式

```bash
# 默认：当月 1 日至今
python main.py

# 指定日期范围
python main.py --from 2026-06-01 --to 2026-06-15

# 指定输出路径
python main.py -o ~/Desktop/token-report.html

# 指定 session 搜索目录
python main.py --session-dirs ~/.claude/projects,~/another-dir
```

## 报告内容

生成的 HTML 报告包含：
- **摘要卡片** — 总 token 数、Input/Output 分布、Cache 命中、调用次数、预估费用
- **模型占比饼图** — 各模型 token 消耗占比（Chart.js 渲染）
- **每日趋势折线图** — 按日期展示 token 消耗变化
- **模型明细表** — 每模型的 Input/Output/Cache/调用次数/费用
- **按项目统计表** — 每个项目的总 token 消耗和费用
- **Session 明细表** — 每个 session 的调用次数、token、费用

## 配置

### .env（隐私项）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SESSION_DIRS` | Session 文件搜索路径，逗号分隔 | `~/.claude/projects` |
| `SESSION_META_DIR` | Session 元数据目录 | `~/.claude/sessions` |

无需创建 `.env`，所有配置都有默认值。如需自定义，参考 `.env.example`。

### worker.yaml（非敏感配置）

- `model_pricing` — 模型单价（¥/1M tokens），用于费用估算。支持前缀匹配。
- `log_retention_days` — 日志保留天数，默认 7

## 数据来源

扫描 `~/.claude/projects/` 下所有 `*.jsonl` session 文件，解析 `type: "assistant"` 的消息中的 `message.usage` 字段：

- `input_tokens` — 实际计费的输入 token（已排除缓存命中）
- `output_tokens` — 输出 token
- `cache_read_input_tokens` — 缓存命中（按低单价计费）
- `cache_creation_input_tokens` — 缓存写入

## 费用估算

价格单位：¥/1M tokens。配置在 `worker.yaml` 的 `model_pricing` 中，可扩展。

费用计算：`input_tokens × 输入单价 + cache_read × 缓存命中单价 + output_tokens × 输出单价`

注意：费用仅为基于单价的估算值，非实际账单。

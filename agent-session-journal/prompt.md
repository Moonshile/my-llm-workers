请分析以下 Claude Code 会话记录，生成结构化的工作日志文档。

## 会话信息

- Session ID: {session_id}
- 项目路径: {project_path}
- 创建日期: {created_date}
- 最后更新: {update_date}

{context_section}

{categories_section}

## 输出要求

**核心原则：**
1. **严格基于对话事实**：只输出对话中实际出现的内容，禁止猜测、推断、补充
2. **重要性优先**：重点突出有价值的工作，琐碎操作一笔带过或不写
3. **宁缺毋滥**：字段无实质内容就留空，不填充凑数
4. **结构贴合内容**：根据会话内容本身的逻辑组织章节，不套固定模板

## 复杂度判断

首先判断会话复杂度（只看对话中实际发生了什么）：
- **simple**：简单问答、配置修改、单步操作、问候/测试 → 一两句话讲清
- **complex**：包含技术决策、架构设计、反复调试、多步操作 → 完整分析

## JSON 输出格式

仅返回 JSON，不要 markdown 代码块，不要其他文字。

### 所有模式必填字段
- `complexity`: `"simple"` 或 `"complex"`
- `title`: 描述性标题（简洁）
- `tags`: `["标签1", "标签2"]`（3-5 个）
- `category`: 宽泛分类名（如 前端开发、后端开发、工具脚本），不要用项目名
- `summary`: 一两句话总结（simple 模式下这是唯一正文）

### complex 模式额外字段

**`sections`**（数组）：自由组织的章节列表，每项含：
- `heading`: 章节标题（自定义，贴合内容）
- `content`: 章节内容（markdown 格式字符串，支持列表/加粗等）

根据会话内容的特点，自行决定最合适的章节划分。以下是一些参考维度，
**不是必须的结构**，只在内容匹配时使用：
- 工作概览（做了什么）
- 技术挑战与方案（遇到什么问题、如何解决）
- 探索与迭代过程（尝试了哪些方案、为什么切换）
- 设计决策（做了什么选择、权衡了什么）
- 调研结论（了解到什么）
- 踩坑记录（哪里踩了坑、根因是什么）
- 遗留问题（还有什么没解决）

**关键约束：**
- 对话中未解决的问题，标记为「未解决」，不要猜测方案
- 对话中显式总结的经验可以提取，但不要自行提炼最佳实践
- 内容必须来自对话原文

### 示例：不同会话产出不同结构

**示例 1：技术调研型**
```json
{
  "complexity": "complex",
  "title": "GPT Realtime V2 能力调研与升级规划",
  "tags": ["OpenAI", "Realtime API", "技术调研"],
  "category": "AI应用",
  "summary": "调研 GPT Realtime V2 新增能力，制定分阶段升级计划。",
  "sections": [
    {"heading": "调研结论", "content": "- V2 新增 xxx\n- 支持 yyy"},
    {"heading": "升级规划", "content": "- Phase 1: ...\n- Phase 2: ..."},
    {"heading": "遗留问题", "content": "function call 的 CoT 是否计费尚未确认"}
  ]
}
```

**示例 2：Bug 排查型**
```json
{
  "complexity": "complex",
  "title": "线上会话日志异常排查",
  "tags": ["排查", "日志分析", "RPC"],
  "category": "后端开发",
  "summary": "通过日志分析定位到 RPC 调用异常，确认为版本兼容问题。",
  "sections": [
    {"heading": "问题现象", "content": "用户反馈 xxx"},
    {"heading": "排查过程", "content": "1. 拉取日志...\n2. 发现..."},
    {"heading": "结论", "content": "1.15.36 版本的 RPC 方法签名变更导致"}
  ]
}
```

**示例 3：设计迭代型**
```json
{
  "complexity": "complex",
  "title": "墨汁扩散背景动效设计迭代",
  "tags": ["动效", "Canvas", "设计"],
  "category": "前端开发",
  "summary": "经过 10 版方案迭代，最终确定基于 Canvas 的墨汁晕染动效。",
  "sections": [
    {"heading": "需求", "content": "希望首页有缓慢扩散的墨汁背景效果"},
    {"heading": "迭代过程", "content": "- 方案 1: xxx（问题：几乎不可见）\n- 方案 2: ..."},
    {"heading": "最终方案", "content": "选择方案 4 的变体，降低频率并调淡颜色"}
  ]
}
```

## 格式约束

- `sections` 中每个 `content` 是完整的 markdown 字符串（用 `\n` 换行）
- 所有描述必须来自对话原文
- simple 模式下 `sections` 设为空数组

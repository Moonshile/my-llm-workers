# md-frontmatter

递归扫描指定目录下的所有 `.md` 文件，使用 LLM 为缺少 YAML frontmatter 的文件自动添加元数据（title、date、tags）。

## 快速开始

```bash
# 1. 配置
cp md-frontmatter/.env.example md-frontmatter/.env
# 编辑 .env，填入 API 配置和监听目录

# 2. 安装依赖
make install

# 3. 使用
python3 md-frontmatter/main.py                  # 处理 WATCH_PATHS 中的所有目录
python3 md-frontmatter/main.py /path/to/posts   # 手动指定目录
python3 md-frontmatter/main.py --dry-run        # 预览模式
python3 md-frontmatter/main.py --update         # 更新已有 frontmatter
```

## 配置

所有配置通过 `md-frontmatter/.env` 文件注入：

| 环境变量 | 说明 |
|---------|------|
| `API_BASE` | OpenAI 兼容 API 地址 |
| `API_KEY` | API key |
| `MODEL` | 模型名 |
| `WATCH_PATHS` | 监听目录列表，逗号分隔（绝对路径） |

`.env` 文件已被 `.gitignore` 忽略，不会提交到仓库。

## 工作原理

每个 `.md` 文件发送给 LLM，LLM 根据文件内容、文件名和所在目录生成元数据：
- **title**：文章标题（优先使用文中第一个 `# 标题`）
- **date**：发布日期（优先从文件名 `YYYY-MM-DD-` 提取）
- **tags**：3-5 个相关标签

LLM 调用失败时自动回退到启发式模式（从内容提取标题、从文件名提取日期、用父目录名作标签）。

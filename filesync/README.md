# filesync — 文件同步工具

按 **最后修改时间 (mtime)** 将组内最新文件同步到其他位置。
覆盖前自动备份，备份 30 天滚动删除。

## 快速开始

1. 创建配置文件：

```bash
cp .filesync.example.yaml .filesync.yaml
```

2. 编辑 `.filesync.yaml`，定义你的文件组：

```yaml
groups:
  - name: "zshrc"
    paths:
      - "~/.zshrc"
      - "~/dotfiles/zsh/.zshrc"
```

3. 预览同步操作：

```bash
uv run python filesync/main.py --dry-run
```

4. 执行同步：

```bash
uv run python filesync/main.py
```

## 命令

| 命令 | 说明 |
|------|------|
| `python filesync/main.py` | 执行一次性同步（含备份+清理） |
| `python filesync/main.py --dry-run` | 预览模式，不修改文件也不备份 |
| `python filesync/main.py --check` | 检查模式，有差异则 exit 1 |
| `python filesync/main.py --backup-days 60` | 自定义备份保留 60 天（默认 30） |

## 同步规则

- **按 mtime 判断**：每组中找到修改时间最新的文件，将其内容复制覆盖到组内其他文件
- **冲突检测**：当多个文件具有相同的最新 mtime 但内容不同时，记录 WARNING 并**跳过**该组
- **安全保护**：内容已一致的文件不会重复写入；覆盖前自动备份被覆盖文件
- **路径支持**：`~` 展开、环境变量展开、相对/绝对路径

## 备份

每次覆盖同步前，被覆盖的文件会自动备份到 `filesync/backups/` 目录。

备份文件名格式：`{YYYYMMDD-HHMMSS}_{组名}__{原始路径}.bak`

示例：
```
20260604-143052_zshrc__Users_duankaiqiang_.zshrc.bak
20260604-143052_zshrc__Users_duankaiqiang_dotfiles_zsh_.zshrc.bak
```

备份默认保留 **30 天**，每次运行同步后自动清理过期备份（可通过 `--backup-days` 调整）。

## 日志

日志存放在 `filesync/logs/` 目录下：

| 文件 | 级别 | 内容 |
|------|------|------|
| `run.log` | INFO | 同步操作记录、备份路径、错误、汇总，含完整 diff |
| `debug.log` | DEBUG | 全部日志，含 SKIP 细节、备份和清理操作 |

日志按天轮转，默认保留 7 天。

## 配置

`.filesync.yaml` 不提交到版本管理（已在 `.gitignore` 中忽略）。

`.filesync.example.yaml` 是模板文件，可提交，方便其他开发者参考格式。

### 文件级同步

每组 `paths` 列出具体文件路径，组内按 mtime 最新者覆盖其他：

```yaml
groups:
  - name: "zshrc"
    paths:
      - "~/.zshrc"
      - "~/dotfiles/zsh/.zshrc"
```

### 目录级同步

新增 `pattern` 字段后，所有 `paths` 视为目录，自动扫描目录内匹配 glob 的文件，按文件名跨目录匹配后同步：

```yaml
groups:
  - name: "cli-commands"
    pattern: "*.md"
    paths:
      - "~/proj/journal/.claude/commands/"
      - "~/proj/diary/.claude/commands/"
```

- `pattern` 支持 `pathlib.Path.glob()` 的通配符，如 `*.md`、`*.txt`、`*.json`
- 按文件名（`Path.name`）跨目录匹配：`dir1/foo.md` 对应 `dir2/foo.md`
- 某目录缺少匹配文件时，自动从其他目录的最新文件创建

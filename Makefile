.PHONY: run test install clean lint lint-md list help cache-clear

install:
	uv sync

run:
	uv run python scheduler/main.py

test:
	uv run pytest -q

lint: lint-md

lint-md:
	npx markdownlint-cli README.md CLAUDE.md agent-session-journal/README.md --config .markdownlint.json

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name logs -exec rm -rf {} + 2>/dev/null || true

# ---------- 一次性工具 ----------

ONEShot_DIR := _oneshot

# 列出所有一次性工具
list:
	@echo "One-shot tools (${ONEShot_DIR}/):"
	@echo ""
	@ok=0; \
	for d in $(ONEShot_DIR)/*/; do \
		[ -d "$$d" ] || continue; \
		yaml="$$d/worker.yaml"; \
		if [ -f "$$yaml" ]; then \
			name=$$(grep '^name:' "$$yaml" 2>/dev/null | head -1 | sed 's/^name: *//'); \
			desc=$$(grep '^description:' "$$yaml" 2>/dev/null | head -1 | sed 's/^description: *//'); \
			[ -z "$$name" ] && name=$$(basename "$$d"); \
			printf "  \033[1m%-24s\033[0m %s\n" "$$name" "$$desc"; \
			ok=1; \
		fi; \
	done; \
	if [ $$ok -eq 0 ]; then echo "  (暂无一次性工具)"; fi
	@echo ""
	@echo "Run 'make help <name>' to see usage of a specific tool."

# 展示指定工具的 README
# 用法: make help <tool-name>
help:
	@tool="$(word 2,$(MAKECMDGOALS))"; \
	if [ -z "$$tool" ]; then \
		echo "Usage: make help <tool-name>"; \
		echo ""; \
		$(MAKE) -s list; \
	else \
		dir="$(ONEShot_DIR)/$$tool"; \
		if [ -f "$$dir/README.md" ]; then \
			cat "$$dir/README.md"; \
		else \
			echo "Unknown tool: $$tool"; \
			echo ""; \
			$(MAKE) -s list; \
		fi; \
	fi

# 清除 git-stats 缓存
cache-clear:
	@rm -f ~/.cache/git-stats-cache.json && echo "缓存已清除: ~/.cache/git-stats-cache.json" || echo "缓存文件不存在"

# 捕获 make help <tool> 中的 <tool> 参数（作为空规则忽略）
%:
	@:

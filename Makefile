.PHONY: run test install clean lint lint-md

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

.PHONY: help install dev test lint clean

help:
	@echo "reccshield monorepo"
	@echo ""
	@echo "Targets:"
	@echo "  install   Install backend (uv) + frontend (bun) dependencies"
	@echo "  dev       Run backend + frontend dev servers concurrently"
	@echo "  test      Run backend pytest + frontend vitest"
	@echo "  lint      Run ruff + basedpyright (backend) and typecheck (frontend)"
	@echo "  clean     Remove build artifacts and dependency caches"

install:
	cd backend && uv sync --extra dev
	cd frontend && bun install

dev:
	@command -v concurrently >/dev/null 2>&1 || { \
		echo "concurrently not installed; install with: bun add -g concurrently"; \
		echo "Falling back to sequential start (backend in background, then frontend)…"; \
		cd backend && uv run uvicorn app.main:app --reload --port 8000 & \
		cd frontend && bun run dev; \
		exit 0; \
	}
	concurrently --names "backend,frontend" --prefix-colors "cyan,magenta" \
		"cd backend && uv run uvicorn app.main:app --reload --port 8000" \
		"cd frontend && bun run dev"

test:
	cd backend && uv run pytest
	cd frontend && bun run test

lint:
	cd backend && uv run ruff check . && uv run basedpyright
	cd frontend && bun run typecheck

clean:
	rm -rf backend/.venv backend/.basedpyright
	rm -rf frontend/node_modules frontend/dist frontend/.vite
	find . -type d -name __pycache__ -exec rm -rf {} +
.PHONY: help up down restart logs jupyter jupyter-stop producer sales-producer clean status build

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Docker ──────────────────────────────────────────────────────────────────

build: ## Build Docker images
	docker compose build

up: ## Start all Docker services (Spark Connect + Kafka + History Server)
	@mkdir -p .tmp/spark-events .tmp/local_iceberg_warehouse .tmp/local_delta_warehouse logs
	docker compose up -d
	@echo ""
	@echo "Services starting..."
	@echo "  Spark Connect : sc://localhost:$${SPARK_CONNECT_PORT:-15002}"
	@echo "  Spark UI      : http://localhost:$${SPARK_UI_PORT:-4040}"
	@echo "  History Server: http://localhost:$${SPARK_HISTORY_PORT:-18080}"
	@echo "  Kafka UI      : http://localhost:$${KAFKA_UI_PORT:-8080}"
	@echo ""
	@echo "Run 'make jupyter' to start JupyterLab locally."

down: ## Stop all Docker services
	docker compose down

restart: down up ## Restart all services

logs: ## Tail Docker service logs
	docker compose logs -f

status: ## Show status of all services
	docker compose ps

# ── Local Development ───────────────────────────────────────────────────────

jupyter: jupyter-stop ## Start JupyterLab locally (kills existing session first)
	@set -a && [ -f .env ] && . ./.env && set +a && \
	uv run jupyter lab \
		--ip=127.0.0.1 \
		--port=$${JUPYTER_PORT:-8888} \
		--no-browser \
		--ServerApp.token="$${JUPYTER_TOKEN:-}" \
		--ServerApp.password=""

jupyter-stop: ## Stop any running JupyterLab session
	@-pkill -f "jupyter-lab" 2>/dev/null && echo "Stopped existing JupyterLab." || true

producer: ## Start file-based event producer
	@set -a && [ -f .env ] && . ./.env && set +a && \
	uv run python app/utils/producer.py

sales-producer: ## Start Kafka sales event producer
	@set -a && [ -f .env ] && . ./.env && set +a && \
	uv run python app/utils/sales_producer.py

# ── Maintenance ─────────────────────────────────────────────────────────────

clean: ## Remove generated data (warehouses, checkpoints, events)
	rm -rf .tmp/local_iceberg_warehouse .tmp/local_delta_warehouse \
	       .tmp/local_parquet_warehouse .tmp/spark-events \
	       .tmp/checkpoint_* app/data/streaming_input/*.json
	@echo "Cleaned generated data. Run notebooks to regenerate."

clean-all: clean ## Remove all generated data + Docker volumes
	docker compose down -v
	@echo "Cleaned Docker volumes too."

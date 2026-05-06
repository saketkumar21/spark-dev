.PHONY: help up down restart logs jupyter jupyter-stop producer sales-producer clean status build \
       dbt-build dbt-debug airflow-up airflow-down airflow-logs airflow-clean

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
	PYTHONPATH="$(PWD)" \
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

# ── dbt (convenience — or: cd dbt && source ../.env && dbt <cmd>) ───────────

dbt-build: ## Full dbt pipeline: seed + run + test
	@set -a && [ -f .env ] && . ./.env && set +a && \
	cd dbt && uv run dbt build --profiles-dir .

dbt-debug: ## Verify dbt connection to Spark Thrift Server
	@set -a && [ -f .env ] && . ./.env && set +a && \
	cd dbt && uv run dbt debug --profiles-dir .

# ── Airflow ─────────────────────────────────────────────────────────────────

AIRFLOW_HOME := $(PWD)/airflow/.airflow_home
AIRFLOW_LOG  := $(AIRFLOW_HOME)/logs/standalone.log

airflow-up: airflow-down ## Start Airflow locally (standalone, UI at :5000)
	@mkdir -p $(AIRFLOW_HOME)/logs
	@echo ""
	@echo "Airflow starting in background..."
	@echo "  Web UI : http://localhost:$${AIRFLOW_PORT:-5000}"
	@echo "  Login  : airflow / airflow"
	@echo "  DAGs   : ./airflow/dags/"
	@echo "  Logs   : make airflow-logs"
	@echo ""
	@AIRFLOW_HOME=$(AIRFLOW_HOME) \
	AIRFLOW__CORE__DAGS_FOLDER=$(PWD)/airflow/dags \
	AIRFLOW__CORE__LOAD_EXAMPLES=false \
	AIRFLOW__WEBSERVER__WEB_SERVER_PORT=$${AIRFLOW_PORT:-5000} \
	AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS=airflow:admin \
	AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE=$(PWD)/airflow/passwords.json \
	bash -c 'cd $(PWD)/airflow && nohup uv run airflow standalone >> $(AIRFLOW_LOG) 2>&1 &'

airflow-logs: ## Tail Airflow standalone logs
	@tail -f $(AIRFLOW_LOG)

airflow-down: ## Stop Airflow (kills all Airflow processes)
	@-pkill -f "$(PWD)/airflow/.venv/bin/airflow" 2>/dev/null && echo "Stopped Airflow." || echo "Airflow is not running."

airflow-clean: airflow-down ## Remove Airflow runtime state (DB, logs)
	rm -rf $(AIRFLOW_HOME)
	@echo "Cleaned Airflow state. Next 'make airflow-up' will reinitialize."

# ── Maintenance ─────────────────────────────────────────────────────────────

clean: ## Remove generated data (warehouses, checkpoints, events)
	rm -rf .tmp app/data/streaming_input/*.json
	@echo "Cleaned generated data. Run notebooks to regenerate."

clean-all: clean ## Remove all generated data + Docker volumes
	docker compose down -v
	@echo "Cleaned Docker volumes too."

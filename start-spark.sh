#!/bin/bash
echo "🚀 Initializing PySpark local environment..."

# 1. Create required directories (safe for fresh clones)
mkdir -p logs app/data/streaming_input
mkdir -p .tmp/spark-events
mkdir -p .tmp/checkpoint_user_events
mkdir -p .tmp/checkpoint_sales_enriched
mkdir -p .tmp/local_iceberg_warehouse
mkdir -p .tmp/local_delta_warehouse

# 2. Load .env and export all variables to child processes
set -a
[ -f .env ] && source .env
set +a

# 3. Set Spark conf directory if not already set
export SPARK_CONF_DIR="${SPARK_CONF_DIR:-./spark_conf}"

# 4. Start the History Server in the background quietly
echo "📈 Starting Spark History Server..."
uv run spark-class org.apache.spark.deploy.history.HistoryServer > .tmp/history_server.log 2>&1 &

# To kill history server
# pkill -f "org.apache.spark.deploy.history.HistoryServer"

echo "✅ History Server is running at http://localhost:18080 (Logs: .tmp/history_server.log)"
echo "📓 Launching Jupyter Lab..."

# 5. Start Jupyter Lab in the foreground
uv run jupyter lab

#!/bin/bash
echo "🚀 Initializing PySpark local environment..."

# 1. Dynamically calculate the absolute path to make configs auto-load and shareable
# export PROJECT_ROOT="$(pwd)"

# 2. Load .env and export all variables to child processes
set -a
source .env
set +a

# 4. Start the History Server in the background quietly
echo "📈 Starting Spark History Server..."
uv run spark-class org.apache.spark.deploy.history.HistoryServer > .tmp/history_server.log 2>&1 &

# To kill history server
# pkill -f "org.apache.spark.deploy.history.HistoryServer"

echo "✅ History Server is running at http://localhost:18080 (Logs: .tmp/history_server.log)"
echo "📓 Launching Jupyter Lab..."

# 5. Start Jupyter Lab in the foreground
uv run jupyter lab

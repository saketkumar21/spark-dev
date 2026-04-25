#!/bin/bash
echo "🚀 Initializing Kafka local environment..."

# 1. Dynamically calculate the absolute path to make configs auto-load and shareable
# export PROJECT_ROOT="$(pwd)"

# 2. Load .env and export all variables to child processes
set -a
source .env
set +a

# 4. Start the History Server in the background quietly
echo "📈 Starting Kafka Server..."

# One-time: format the storage
# kafka-storage format \
#   --config /opt/homebrew/etc/kafka/server.properties \
#   --cluster-id $(kafka-storage random-uuid) \
#   --standalone

# Start broker (runs on localhost:9092)
kafka-server-start /opt/homebrew/etc/kafka/server.properties > .tmp/kafka_server.log 2>&1 &

# Verify it's running:
kafka-topics --bootstrap-server localhost:9092 --list

echo "✅ Kafka Server is running at http://localhost:9092 (Logs: .tmp/kafka_server.log)"
echo "📓 Running sales_producer.py ..."

export KAFKA_BOOTSTRAP_SERVERS=localhost:9092

# Terminal 2 — sales events
uv run python sales_producer.py > .tmp/sales_producer.log 2>&1 &

# Verify messages arrived
kafka-console-consumer --bootstrap-server localhost:9092 \
  --topic sales-events --from-beginning --max-messages 5

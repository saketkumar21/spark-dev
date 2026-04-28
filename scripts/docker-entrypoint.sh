#!/bin/bash
set -e

cd /app

# Create required directories
mkdir -p .tmp/spark-events .tmp/local_iceberg_warehouse .tmp/local_delta_warehouse \
         .tmp/checkpoint_user_events .tmp/checkpoint_sales_enriched \
         app/data/streaming_input logs

MODE="${1:-connect}"

case "$MODE" in
  connect)
    echo "=== Starting Spark Connect Server ==="
    echo "  gRPC port : ${SPARK_CONNECT_PORT:-15002}"
    echo "  Spark UI  : http://localhost:4040"

    # Start connect server as daemon
    $SPARK_HOME/sbin/start-connect-server.sh \
      --conf "spark.connect.grpc.binding.port=${SPARK_CONNECT_PORT:-15002}" 2>&1

    sleep 5

    PID_FILE=$(ls /tmp/spark-*.pid 2>/dev/null | head -1)

    if [ -z "$PID_FILE" ]; then
      echo "ERROR: Spark Connect Server failed to start. Logs:"
      cat $SPARK_HOME/logs/*.out 2>/dev/null || true
      exit 1
    fi

    PID=$(cat "$PID_FILE")
    echo "Spark Connect Server running (PID: $PID)"

    # Forward logs to stdout
    tail -F $SPARK_HOME/logs/*.out 2>/dev/null &

    # Wait for process — exit if it dies (Docker will restart)
    while kill -0 "$PID" 2>/dev/null; do
      sleep 5
    done

    echo "Spark Connect Server stopped unexpectedly"
    exit 1
    ;;

  history)
    echo "=== Starting Spark History Server ==="
    echo "  UI: http://localhost:18080"

    $SPARK_HOME/sbin/start-history-server.sh 2>&1

    sleep 3

    PID_FILE=$(ls /tmp/spark-*.pid 2>/dev/null | head -1)

    if [ -z "$PID_FILE" ]; then
      echo "ERROR: History Server failed to start. Logs:"
      cat $SPARK_HOME/logs/*.out 2>/dev/null || true
      exit 1
    fi

    PID=$(cat "$PID_FILE")
    echo "History Server running (PID: $PID)"

    tail -F $SPARK_HOME/logs/*.out 2>/dev/null &

    while kill -0 "$PID" 2>/dev/null; do
      sleep 5
    done

    echo "History Server stopped unexpectedly"
    exit 1
    ;;

  *)
    exec "$@"
    ;;
esac

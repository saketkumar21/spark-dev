# =============================================================================
# Spark Unified Server (Thrift + Connect) + History Server
# Base: Apache Spark 4.0.2 with Scala 2.13, Java 17, Python 3, Ubuntu
# =============================================================================

# ── Stage 1: Resolve JARs via Ivy (handles all transitive dependencies) ─────
FROM spark:4.0.2-scala2.13-java17-python3-ubuntu AS deps

ARG SPARK_PACKAGES="\
org.apache.iceberg:iceberg-spark-runtime-4.0_2.13:1.10.1,\
io.delta:delta-spark_2.13:4.0.0,\
org.apache.spark:spark-sql-kafka-0-10_2.13:4.0.2"

RUN echo ':quit' | $SPARK_HOME/bin/spark-shell \
      --conf spark.jars.ivy=/tmp/ivy \
      --packages "${SPARK_PACKAGES}" 2>/dev/null || true

# ── Stage 2: Final image ────────────────────────────────────────────────────
FROM spark:4.0.2-scala2.13-java17-python3-ubuntu

USER root

# System utilities
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl procps netcat-openbsd && \
    rm -rf /var/lib/apt/lists/*

# uv — fast Python package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Pre-resolved JARs from stage 1 → system classpath (fixes Thrift classloader)
COPY --from=deps /tmp/ivy/jars/ /opt/spark/jars/

WORKDIR /app

# Python dependencies (cached layer — only rebuilds when manifests change)
COPY pyproject.toml uv.lock ./
COPY dbt-spark-qualify/ ./dbt-spark-qualify/
RUN uv sync --frozen 2>/dev/null || uv sync

# Spark configuration
COPY conf/spark-defaults.conf /opt/spark/conf/spark-defaults.conf
COPY conf/log4j2.properties /opt/spark/conf/log4j2.properties

# Entrypoint
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Create required directories
RUN mkdir -p .tmp/spark-events .tmp/local_iceberg_warehouse .tmp/local_delta_warehouse \
             .tmp/checkpoint_user_events .tmp/checkpoint_sales_enriched \
             .tmp/metastore .tmp/spark-warehouse \
             app/data/streaming_input logs /opt/spark/logs

# Copy project files
COPY . .

# Ports: 10000=Thrift JDBC, 15002=Spark Connect, 4040=Spark UI, 18080=History
EXPOSE 10000 15002 4040 18080

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["connect"]

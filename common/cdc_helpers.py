"""
cdc_helpers (Phase 4) — Postgres DML + Kafka Connect / Debezium admin for the CDC track.

The pipeline these modules build:
    Postgres (wal_level=logical) → Debezium (Kafka Connect) → Kafka → Spark → Iceberg MERGE

Topology (see docker-compose.yml; CDC services are opt-in via `make cdc-up`):
    • Postgres      host  → ``localhost:5432``      (``PG_*`` below; psycopg2 from the notebook)
    • Kafka Connect host  → ``http://localhost:8083`` (``CONNECT_URL``; Debezium REST API)
    • Kafka         Spark → ``kafka:9092``            (``SPARK_BOOTSTRAP`` from kafka_helpers)
                    host  → ``localhost:29092``       (``BOOTSTRAP``      from kafka_helpers)

Debezium publishes one Kafka topic per captured table, named
    ``<topic.prefix>.<schema>.<table>``   e.g.  ``dbz.public.orders``
so Spark's ``readStream`` subscribes to that topic via ``SPARK_BOOTSTRAP``.

Everything here is laptop-safe: tiny seed tables, bounded polling with timeouts, and a
``teardown()`` that deletes the connector, drops the replication slot, and drops the table.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import psycopg2
from kafka import KafkaConsumer

BOOTSTRAP = "localhost:29092"   # Kafka host listener (same as common.kafka_helpers.BOOTSTRAP)

# ── connection points (host side) ────────────────────────────────────────────
PG_HOST = "localhost"
PG_PORT = 5432
PG_USER = "cdc"
PG_PASSWORD = "cdc"
PG_DB = "inventory"

CONNECT_URL = "http://localhost:8083"     # Kafka Connect REST API (host)
TOPIC_PREFIX = "dbz"                       # Debezium server name → topic prefix


# ── Postgres ──────────────────────────────────────────────────────────────────
def pg_connect(autocommit: bool = True):
    """Open a psycopg2 connection to the CDC Postgres (autocommit by default — DDL/DML
    take effect immediately, which is what the CDC demos want)."""
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                            password=PG_PASSWORD, dbname=PG_DB)
    conn.autocommit = autocommit
    return conn


def pg_exec(sql: str, params=None, fetch: bool = False):
    """Run one statement. Returns fetched rows when ``fetch=True`` (list of tuples), else None."""
    conn = pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if fetch else None
    finally:
        conn.close()


def pg_exec_many(statements: list[str]) -> None:
    """Run several statements on one connection (each autocommits)."""
    conn = pg_connect()
    try:
        with conn.cursor() as cur:
            for s in statements:
                cur.execute(s)
    finally:
        conn.close()


def seed_orders(table: str = "orders", n: int = 20, replica_identity_full: bool = False) -> int:
    """(Re)create a simple ``public.<table>`` source table and seed ``n`` rows.

    ``replica_identity_full=True`` sets ``REPLICA IDENTITY FULL`` so UPDATE/DELETE events
    carry the full ``before`` image (see CDC-6). Returns ``n``.
    """
    stmts = [
        f"DROP TABLE IF EXISTS public.{table}",
        f"""CREATE TABLE public.{table} (
                id       INT PRIMARY KEY,
                customer TEXT NOT NULL,
                amount   NUMERIC(10,2) NOT NULL,
                status   TEXT NOT NULL DEFAULT 'NEW',
                updated  TIMESTAMP NOT NULL DEFAULT now()
            )""",
    ]
    if replica_identity_full:
        stmts.append(f"ALTER TABLE public.{table} REPLICA IDENTITY FULL")
    conn = pg_connect()
    try:
        with conn.cursor() as cur:
            for s in stmts:
                cur.execute(s)
            for i in range(n):
                cur.execute(
                    f"INSERT INTO public.{table} (id, customer, amount, status) VALUES (%s,%s,%s,%s)",
                    (i, f"cust-{i % 7}", round(10 + i * 1.5, 2), "NEW"),
                )
    finally:
        conn.close()
    return n


# ── replication slots (the CDC-5 'Prove it' — WAL retention) ──────────────────
def list_slots() -> list[dict]:
    """Rows from ``pg_replication_slots`` with retained-WAL bytes.

    ``retained_bytes`` = WAL the slot is pinning (``pg_current_wal_lsn() - restart_lsn``).
    An inactive slot with a growing ``retained_bytes`` is the WAL-growth pathology (CDC-5).
    """
    rows = pg_exec(
        """
        SELECT slot_name, active, restart_lsn,
               pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes
        FROM pg_replication_slots
        ORDER BY slot_name
        """,
        fetch=True,
    ) or []
    return [
        {"slot_name": r[0], "active": r[1], "restart_lsn": str(r[2]),
         "retained_bytes": int(r[3]) if r[3] is not None else None}
        for r in rows
    ]


def drop_slot(slot_name: str, retries: int = 5, wait: float = 1.5) -> bool:
    """Drop a logical replication slot. A slot is only droppable once **inactive**, and a
    just-deleted connector can take a moment to release it — so retry a few times. Returns
    True if the slot is gone (dropped or never existed)."""
    for _ in range(retries):
        present = pg_exec("SELECT 1 FROM pg_replication_slots WHERE slot_name=%s",
                          (slot_name,), fetch=True)
        if not present:
            return True
        try:
            pg_exec("SELECT pg_drop_replication_slot(%s)", (slot_name,))
            return True
        except Exception:  # noqa: BLE001 — likely still active; wait and retry
            time.sleep(wait)
    return False


# ── Kafka Connect REST API (Debezium connector lifecycle) ─────────────────────
def _req(method: str, path: str, body: dict | None = None, timeout: float = 15.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{CONNECT_URL}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:400]}


def _safe_slot(name: str) -> str:
    """Postgres replication-slot names allow only ``[a-z0-9_]`` (≤63 chars), so a connector
    name with hyphens (e.g. ``cdc-orders``) can't be reused verbatim."""
    s = "".join(c if (c.islower() or c.isdigit() or c == "_") else "_" for c in name.lower())
    return f"{s}_slot"[:63]


def debezium_pg_config(name: str, table: str = "orders", *, slot: str | None = None,
                       snapshot_mode: str = "initial", extra: dict | None = None) -> dict:
    """A standard Debezium Postgres connector config (pgoutput plugin, JSON converter).

    Captures ``public.<table>`` → Kafka topic ``<TOPIC_PREFIX>.public.<table>``. ``slot``
    defaults to a sanitized ``<name>_slot`` (slot names allow only ``[a-z0-9_]``). ``extra``
    overrides/extends any field (e.g. add an ``ExtractNewRecordState`` transform for CDC-4).
    """
    cfg = {
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
        "database.hostname": "postgres",          # service name on the docker network
        "database.port": "5432",
        "database.user": PG_USER,
        "database.password": PG_PASSWORD,
        "database.dbname": PG_DB,
        "topic.prefix": TOPIC_PREFIX,
        "plugin.name": "pgoutput",                # built into Postgres 16
        "slot.name": slot or _safe_slot(name),
        # Unique publication PER connector — Debezium's default name is the shared
        # "dbz_publication", so two connectors with different table.include.list would
        # fight over one publication and silently stop emitting. Isolate them.
        "publication.name": f"{_safe_slot(name)[:-5]}_pub",
        "publication.autocreate.mode": "filtered",
        "table.include.list": f"public.{table}",
        "snapshot.mode": snapshot_mode,
        "tombstones.on.delete": "true",
        "decimal.handling.mode": "double",        # NUMERIC → readable double (not base64 bytes)
        "key.converter": "org.apache.kafka.connect.json.JsonConverter",
        "value.converter": "org.apache.kafka.connect.json.JsonConverter",
        "key.converter.schemas.enable": "false",
        "value.converter.schemas.enable": "false",
    }
    if extra:
        cfg.update(extra)
    return cfg


def register_connector(name: str, config: dict) -> dict:
    """Create (or replace) a connector. Uses PUT /connectors/<name>/config so it's idempotent."""
    status, body = _req("PUT", f"/connectors/{name}/config", config)
    return {"status": status, "body": body}


def delete_connector(name: str) -> int:
    """Delete a connector (ignore if absent). Returns the HTTP status."""
    status, _ = _req("DELETE", f"/connectors/{name}")
    return status


def reset_offsets(name: str) -> bool:
    """Reset a connector's stored offsets so a re-registration with the same name re-runs the
    **initial snapshot** (Connect persists offsets in the connect_offsets topic — deleting the
    connector alone does NOT clear them, so a re-created connector would skip the snapshot and
    only stream). Connect 3.x flow: STOP → DELETE /offsets. Returns True on success."""
    # The connector must exist and be STOPPED before its offsets can be deleted.
    if _req("GET", f"/connectors/{name}")[0] != 200:
        return False
    _req("PUT", f"/connectors/{name}/stop")
    for _ in range(10):
        st = connector_status(name).get("connector", {}).get("state")
        if st == "STOPPED":
            break
        time.sleep(1.0)
    status, _ = _req("DELETE", f"/connectors/{name}/offsets")
    return status in (200, 204)


def connector_status(name: str) -> dict:
    """GET /connectors/<name>/status — connector + task states (RUNNING / FAILED / ...)."""
    _, body = _req("GET", f"/connectors/{name}/status")
    return body or {}


def wait_for_connector(name: str, timeout: float = 60.0, poll: float = 2.0) -> str:
    """Poll until the connector AND its task report RUNNING (or FAILED), or timeout.
    Returns the final connector state string."""
    deadline = time.time() + timeout
    state = "UNKNOWN"
    while time.time() < deadline:
        st = connector_status(name)
        state = st.get("connector", {}).get("state", "UNKNOWN")
        tasks = st.get("tasks", [])
        task_states = {t.get("state") for t in tasks}
        if state == "RUNNING" and tasks and task_states == {"RUNNING"}:
            return "RUNNING"
        if state == "FAILED" or "FAILED" in task_states:
            return "FAILED"
        time.sleep(poll)
    return state


def connect_up(timeout: float = 60.0) -> bool:
    """True once the Connect REST API answers (the worker finished starting)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = _req("GET", "/", timeout=5)
            if status == 200:
                return True
        except Exception:  # noqa: BLE001 — not up yet
            pass
        time.sleep(2.0)
    return False


def topic_name(table: str = "orders", schema: str = "public") -> str:
    """The Kafka topic Debezium publishes a captured table to: ``<prefix>.<schema>.<table>``."""
    return f"{TOPIC_PREFIX}.{schema}.{table}"


def read_cdc_events(topic: str, max_ms: int = 12000, bootstrap: str = BOOTSTRAP) -> list[dict]:
    """Drain a Debezium topic once (from the beginning) and return parsed events.

    Each item is ``{"op": "r|c|u|d"|None, "before": dict|None, "after": dict|None, "raw": value}``;
    a ``None`` op is a **tombstone** (the null-value record Debezium emits after a delete). Bounded
    by ``max_ms`` so it always returns. The reusable 'show me the envelope' read for CDC-2/3/4/6.
    """
    c = KafkaConsumer(topic, bootstrap_servers=bootstrap, auto_offset_reset="earliest",
                      enable_auto_commit=False, consumer_timeout_ms=max_ms,
                      value_deserializer=lambda b: b.decode() if b else None)
    out = []
    try:
        for m in c:
            if m.value is None:
                out.append({"op": None, "before": None, "after": None, "raw": None})
                continue
            v = json.loads(m.value)
            out.append({"op": v.get("op"), "before": v.get("before"),
                        "after": v.get("after"), "raw": v})
    finally:
        c.close()
    return out


def op_counts(events: list[dict]) -> dict:
    """Summarize :func:`read_cdc_events` output: ``{op_or_'tombstone': count}`` (snapshot=r,
    create=c, update=u, delete=d, tombstone=null-value record)."""
    counts: dict = {}
    for e in events:
        k = e["op"] if e["op"] is not None else "tombstone"
        counts[k] = counts.get(k, 0) + 1
    return counts


def delete_data_topic(table: str = "orders", schema: str = "public") -> None:
    """Delete the Debezium data topic for a table so a re-run starts clean (Kafka topics
    persist across connector re-registrations — stale events would otherwise pile up)."""
    admin = __import__("kafka").KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        admin.delete_topics([topic_name(table, schema)])
    except Exception:  # noqa: BLE001 — absent
        pass
    finally:
        admin.close()


def teardown(name: str, table: str | None = None, slot: str | None = None,
             drop_topic: bool = True) -> None:
    """Clean up a CDC demo: delete the connector, drop its replication slot, drop the table,
    and (by default) delete the Debezium data topic so the module re-runs from a clean slate.
    Resets Connect offsets first so a same-named re-registration re-runs the initial snapshot
    (otherwise it resumes from stored offsets and skips the snapshot)."""
    reset_offsets(name)   # stop + clear offsets so the next run snapshots (no-op if absent)
    delete_connector(name)
    time.sleep(2.0)  # let Connect begin releasing the slot
    drop_slot(slot or _safe_slot(name))   # retries until the slot is inactive & gone
    if table:
        pg_exec(f"DROP TABLE IF EXISTS public.{table}")
        if drop_topic:
            delete_data_topic(table)

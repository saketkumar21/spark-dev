#!/usr/bin/env python3
"""
Event producer — writes 2 user-event rows as a new JSONL file every 10 seconds
into ./data/streaming_input/, the directory that Spark Structured Streaming watches.

Usage:
    uv run python producer.py

Stop with Ctrl+C.
"""

import json
import os
import random
import time
import uuid
from datetime import datetime, timezone

OUTPUT_DIR    = "./data/streaming_input"
INTERVAL_SEC  = 10

EVENT_TYPES = ["page_view", "click", "search", "add_to_cart", "purchase", "logout"]
PAGES       = ["/home", "/products", "/cart", "/checkout", "/profile", "/search", "/orders"]
USERS       = [f"user_{i:03d}" for i in range(1, 21)]   # 20 virtual users

os.makedirs(OUTPUT_DIR, exist_ok=True)
print(f"Producer started. Writing to {OUTPUT_DIR!r} every {INTERVAL_SEC}s — Ctrl+C to stop.\n")

batch = 0
try:
    while True:
        batch += 1
        ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        filename = f"{OUTPUT_DIR}/events_{ts}_{batch:04d}.json"

        rows = [
            {
                "event_id":   str(uuid.uuid4()),
                "user_id":    random.choice(USERS),
                "event_type": random.choice(EVENT_TYPES),
                "page":       random.choice(PAGES),
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "session_id": str(uuid.uuid4())[:8],
            }
            for _ in range(2)
        ]

        with open(filename, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

        print(f"[Batch {batch:04d}] {ts}  →  {os.path.basename(filename)}")
        for row in rows:
            print(f"           {row['user_id']}  {row['event_type']:<14}  {row['page']}")
        print()

        time.sleep(INTERVAL_SEC)

except KeyboardInterrupt:
    print(f"\nProducer stopped after {batch} batch(es) / {batch * 2} events.")

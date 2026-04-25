#!/usr/bin/env python3
"""
Sales-event producer — publishes 2 sale events every 10 seconds to the Kafka
topic `sales-events`.  The sales streaming notebook subscribes to this topic
and enriches events with a stream-static join against customers.csv.

Usage (host machine, Kafka exposed on localhost:29092):
    uv run python sales_producer.py

Usage (inside Docker network, Kafka on kafka:9092):
    KAFKA_BOOTSTRAP_SERVERS=kafka:9092 uv run python sales_producer.py

Stop with Ctrl+C.
"""

import json
import os
import random
import time
import uuid
from datetime import datetime, timezone

from kafka import KafkaProducer

BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:29092")
TOPIC             = "sales-events"
INTERVAL_SEC      = int(os.environ.get("PRODUCER_INTERVAL", 10))

CUSTOMERS = [f"C{i:03d}" for i in range(1, 21)]   # C001–C020 (matches customers.csv)
STORES    = [f"S{i:03d}" for i in range(1, 6)]    # S001–S005

PAYMENT_METHODS = ["credit_card", "debit_card", "paypal", "apple_pay"]

PRODUCTS = [
    {"product_id": "P001", "product_name": "Laptop Pro 15",          "category": "Electronics", "unit_price": 1299.99},
    {"product_id": "P002", "product_name": "Wireless Mouse",          "category": "Electronics", "unit_price":   29.99},
    {"product_id": "P003", "product_name": "Mechanical Keyboard",     "category": "Electronics", "unit_price":   79.99},
    {"product_id": "P004", "product_name": "4K Monitor",              "category": "Electronics", "unit_price":  399.99},
    {"product_id": "P005", "product_name": "Noise-Cancel Headphones", "category": "Electronics", "unit_price":  149.99},
    {"product_id": "P006", "product_name": "Espresso Machine",        "category": "Home",        "unit_price":  249.99},
    {"product_id": "P007", "product_name": "Air Fryer XL",            "category": "Home",        "unit_price":   89.99},
    {"product_id": "P008", "product_name": "Python Data Engineering",  "category": "Books",       "unit_price":   49.99},
    {"product_id": "P009", "product_name": "Running Shoes",           "category": "Clothing",    "unit_price":  119.99},
    {"product_id": "P010", "product_name": "USB-C Hub 7-in-1",        "category": "Electronics", "unit_price":   59.99},
]

producer = KafkaProducer(
    bootstrap_servers=BOOTSTRAP_SERVERS,
    value_serializer=lambda v: json.dumps(v).encode("utf-8"),
)

print(f"Sales-event producer started.")
print(f"  Broker : {BOOTSTRAP_SERVERS}")
print(f"  Topic  : {TOPIC}")
print(f"  Rate   : 2 events every {INTERVAL_SEC}s — Ctrl+C to stop.\n")

batch = 0
try:
    while True:
        batch += 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

        for _ in range(2):
            product  = random.choice(PRODUCTS)
            quantity = random.randint(1, 4)
            event = {
                "sale_id":        str(uuid.uuid4()),
                "customer_id":    random.choice(CUSTOMERS),
                "product_id":     product["product_id"],
                "product_name":   product["product_name"],
                "category":       product["category"],
                "quantity":       quantity,
                "unit_price":     product["unit_price"],
                "total_amount":   round(quantity * product["unit_price"], 2),
                "store_id":       random.choice(STORES),
                "payment_method": random.choice(PAYMENT_METHODS),
                "timestamp":      datetime.now(timezone.utc).isoformat(),
            }
            producer.send(TOPIC, value=event)
            print(f"[Batch {batch:04d}] {ts}  {event['customer_id']}  {event['product_name']:<28}  ${event['total_amount']:>8.2f}  {event['payment_method']}")

        producer.flush()
        print()
        time.sleep(INTERVAL_SEC)

except KeyboardInterrupt:
    producer.close()
    print(f"\nSales producer stopped after {batch} batch(es) / {batch * 2} events.")

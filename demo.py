"""Demo: generate 14 days of realistic per-key LLM usage, inject a 3AM-style
cost spike, and detect it. Deterministic — same output on every run.

Usage: python demo.py   (writes demo_usage.jsonl next to this file, then analyses it)
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

from alerter import detect, load_records, payloads_json

BASE_TIME = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
SEED = 20261017
HERE = os.path.dirname(os.path.abspath(__file__))
USAGE_FILE = os.path.join(HERE, "demo_usage.jsonl")

# (key_id, base $/hr, hourly noise, daily peak hour)
KEYS = [
    ("prod-key-1", 4.20, 0.35, 14),
    ("prod-key-2", 1.10, 0.30, 10),
    ("staging-key", 0.25, 0.50, 16),
]

# The injected incident: key, hour offset from BASE_TIME, cost multiplier.
SPIKE = ("prod-key-2", 301, 25.0)


def generate_usage():
    rng = random.Random(SEED)
    rows = []
    for key_id, base, noise, peak_hour in KEYS:
        for h in range(336):  # 14 days of hourly records
            ts = BASE_TIME + timedelta(hours=h)
            # business-hours hump + noise (kept gentle so only real spikes alert)
            wave = 1.0 + 0.3 * max(0.0, 1.0 - abs(ts.hour - peak_hour) / 8.0)
            jitter = 1.0 + rng.uniform(-noise, noise)
            cost = round(base * wave * jitter, 4)
            if (key_id, h) == (SPIKE[0], SPIKE[1]):
                cost = round(base * SPIKE[2], 4)  # the 3AM surprise
            rows.append({
                "timestamp": ts.isoformat(),
                "key_id": key_id,
                "tokens": int(cost * 75000),
                "cost": cost,
            })
    rows.sort(key=lambda r: r["timestamp"])
    with open(USAGE_FILE, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return len(rows)


def main():
    started = time.time()
    n = generate_usage()
    records, skipped = load_records(USAGE_FILE)
    anomalies = detect(records)
    elapsed = time.time() - started

    print(f"records analysed : {n} hourly usage rows (skipped {skipped})")
    print(f"keys monitored   : {len({r['key_id'] for r in records})}")
    print(f"anomalies found  : {len(anomalies)}")
    # deterministic, honest bound: it really did finish in under 0.20s
    print(f"analysis time    : {'<0.20s' if elapsed < 0.20 else f'{elapsed:.2f}s'}")
    print()
    for a in anomalies:
        print(
            f"[{a['severity'].upper():8s}] {a['key_id']} @ {a['hour']}  "
            f"spent ${a['observed_cost']:.2f} vs baseline "
            f"${a['baseline_median']:.2f}/hr  (score {a['score']})"
        )
    print()
    print("Alert payload (PagerDuty-style, POST to any webhook):")
    print(payloads_json(anomalies))


if __name__ == "__main__":
    main()

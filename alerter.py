"""llm-cost-anomaly-alerter: catch LLM API spend spikes before the invoice does.

Ingests per-API-key usage records (CSV or JSONL), builds a per-key rolling
baseline (median + MAD), flags upward cost anomalies with a severity, and
emits alert payloads shaped for any webhook / PagerDuty-style endpoint.

Stdlib only.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

REQUIRED_FIELDS = ("timestamp", "key_id", "tokens", "cost")

# PagerDuty v2 severity ladder mapped from our internal severities.
SEVERITY_MAP = {"warn": "warning", "high": "error", "critical": "critical"}


# ---------------------------------------------------------------- ingestion

def parse_timestamp(value):
    """Parse an ISO-8601 timestamp; naive values are assumed UTC."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_records(path):
    """Load usage records from a CSV or JSONL file.

    Expected fields per record: timestamp, key_id, tokens, cost.
    Returns (records, skipped) where records is a list of dicts with
    parsed/normalised values and skipped is the number of malformed rows.
    """
    path = str(path)
    if path.endswith(".jsonl") or path.endswith(".ndjson"):
        raw = _load_jsonl(path)
    elif path.endswith(".csv"):
        raw = _load_csv(path)
    else:
        raise ValueError(f"unsupported file type (want .csv or .jsonl): {path}")

    records, skipped = [], 0
    for row in raw:
        try:
            records.append({
                "timestamp": parse_timestamp(row["timestamp"]),
                "key_id": str(row["key_id"]),
                "tokens": int(float(row["tokens"])),
                "cost": float(row["cost"]),
            })
        except (KeyError, TypeError, ValueError):
            skipped += 1
    records.sort(key=lambda r: r["timestamp"])
    return records, skipped


def _load_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------- baselines

def bucket_hourly(records):
    """Aggregate records into per-key, per-hour cost buckets.

    Returns {key_id: [(hour_start_iso, cost, tokens, n_records), ...]} sorted
    by hour.
    """
    buckets = {}
    for rec in records:
        hour = rec["timestamp"].replace(minute=0, second=0, microsecond=0)
        key = buckets.setdefault(rec["key_id"], {})
        cell = key.setdefault(hour, {"cost": 0.0, "tokens": 0, "n": 0})
        cell["cost"] += rec["cost"]
        cell["tokens"] += rec["tokens"]
        cell["n"] += 1
    out = {}
    for key_id, hours in buckets.items():
        out[key_id] = [
            (h.isoformat(), v["cost"], v["tokens"], v["n"])
            for h, v in sorted(hours.items())
        ]
    return out


def rolling_median_mad(values, window):
    """Rolling median and MAD over the *previous* `window` values.

    Returns a list the same length as `values`; entry i is (median, mad) of
    values[max(0, i-window):i], or (None, None) when there is no history.
    """
    out = []
    for i in range(len(values)):
        hist = values[max(0, i - window):i]
        if not hist:
            out.append((None, None))
            continue
        med = _median(hist)
        mad = _median([abs(x - med) for x in hist])
        out.append((med, mad))
    return out


def _median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


# ---------------------------------------------------------------- detection

def detect(records, window=168, thresholds=None, min_history=24,
           min_abs_deviation=1.0):
    """Flag upward cost anomalies per API key.

    - window: how many prior hourly buckets form the baseline (default 168 = 1 week)
    - thresholds: {severity: mad_score} e.g. {"warn": 3.0, "high": 5.0, "critical": 8.0}
    - min_history: buckets of history required before a key can alert
    - min_abs_deviation: ignore deviations smaller than this in dollars
    """
    thresholds = thresholds or {"warn": 3.0, "high": 5.0, "critical": 8.0}
    ordered = sorted(thresholds.items(), key=lambda kv: kv[1])
    anomalies = []
    for key_id, series in bucket_hourly(records).items():
        costs = [c for _, c, _, _ in series]
        stats = rolling_median_mad(costs, window)
        for idx, ((hour, cost, tokens, n), (med, mad)) in enumerate(zip(series, stats)):
            if med is None or idx < min_history:
                continue
            deviation = cost - med
            if deviation < min_abs_deviation:
                continue  # only upward spikes, above a noise floor
            # MAD scaled to be comparable to a standard deviation for
            # roughly-normal data; falls back gracefully when MAD is 0.
            scale = mad * 1.4826 if mad > 0 else max(min_abs_deviation, med * 0.05)
            score = deviation / scale
            severity = None
            for name, bar in ordered:
                if score >= bar:
                    severity = name
            if severity is None:
                continue
            anomalies.append({
                "key_id": key_id,
                "hour": hour,
                "observed_cost": round(cost, 4),
                "baseline_median": round(med, 4),
                "mad": round(mad, 4),
                "score": round(score, 2),
                "severity": severity,
                "tokens": tokens,
                "records": n,
            })
    anomalies.sort(key=lambda a: (a["hour"], a["key_id"]))
    return anomalies


# ---------------------------------------------------------------- alerting

def alert_payload(anomaly, source="llm-cost-anomaly-alerter", routing_key=None):
    """Build a PagerDuty-Events-v2-shaped payload for one anomaly.

    Any generic webhook can consume the same JSON; `routing_key` is only
    needed for real PagerDuty delivery.
    """
    return {
        "routing_key": routing_key or "REPLACE_WITH_PAGERDUTY_ROUTING_KEY",
        "event_action": "trigger",
        "payload": {
            "summary": (
                f"LLM cost anomaly [{anomaly['severity']}]: key "
                f"'{anomaly['key_id']}' spent ${anomaly['observed_cost']:.2f} "
                f"in hour {anomaly['hour']} vs baseline "
                f"${anomaly['baseline_median']:.2f}/hr"
            ),
            "severity": SEVERITY_MAP[anomaly["severity"]],
            "source": source,
            "custom_details": {
                "key_id": anomaly["key_id"],
                "hour": anomaly["hour"],
                "observed_cost": anomaly["observed_cost"],
                "baseline_median": anomaly["baseline_median"],
                "mad": anomaly["mad"],
                "mad_score": anomaly["score"],
                "tokens": anomaly["tokens"],
            },
        },
    }


def payloads_json(anomalies, **kwargs):
    """Serialise alert payloads for all anomalies as pretty JSON."""
    return json.dumps(
        [alert_payload(a, **kwargs) for a in anomalies],
        indent=2, sort_keys=True,
    )

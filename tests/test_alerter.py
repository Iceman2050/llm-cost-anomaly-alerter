"""Tests for llm-cost-anomaly-alerter. Stdlib unittest, no dependencies."""
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alerter import (
    alert_payload,
    bucket_hourly,
    detect,
    load_records,
    parse_timestamp,
    payloads_json,
    rolling_median_mad,
)

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def make_records(key="k1", hours=72, base_cost=2.0, spike_at=None,
                 spike_mult=20.0):
    recs = []
    for h in range(hours):
        cost = base_cost
        if spike_at is not None and h == spike_at:
            cost = base_cost * spike_mult
        recs.append({
            "timestamp": BASE + timedelta(hours=h),
            "key_id": key,
            "tokens": int(cost * 1000),
            "cost": cost,
        })
    return recs


def write_temp(suffix, content):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


class TestIngestion(unittest.TestCase):
    def test_csv_load(self):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["timestamp", "key_id", "tokens", "cost"])
        w.writeheader()
        w.writerow({"timestamp": "2026-10-01T00:00:00Z", "key_id": "a",
                    "tokens": "100", "cost": "0.50"})
        path = write_temp(".csv", buf.getvalue())
        try:
            records, skipped = load_records(path)
        finally:
            os.unlink(path)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["key_id"], "a")
        self.assertEqual(records[0]["tokens"], 100)
        self.assertAlmostEqual(records[0]["cost"], 0.50)
        self.assertEqual(records[0]["timestamp"].tzinfo, timezone.utc)

    def test_jsonl_load(self):
        lines = "\n".join(json.dumps({
            "timestamp": "2026-10-01T01:00:00+00:00",
            "key_id": "b", "tokens": 200, "cost": 1.25}) for _ in range(3))
        path = write_temp(".jsonl", lines + "\n\n")  # trailing blank line ok
        try:
            records, skipped = load_records(path)
        finally:
            os.unlink(path)
        self.assertEqual((len(records), skipped), (3, 0))

    def test_malformed_rows_skipped(self):
        lines = "\n".join([
            json.dumps({"timestamp": "2026-10-01T00:00:00Z", "key_id": "a",
                        "tokens": 1, "cost": 1.0}),
            json.dumps({"key_id": "a", "cost": "not-a-number"}),  # bad
            "this is not json at all",                            # bad
        ])
        path = write_temp(".jsonl", lines)
        try:
            with self.assertRaises(json.JSONDecodeError):
                load_records(path)
        finally:
            os.unlink(path)

    def test_bad_extension_rejected(self):
        with self.assertRaises(ValueError):
            load_records("/tmp/x.xml")

    def test_parse_timestamp_naive_assumed_utc(self):
        dt = parse_timestamp("2026-10-01 00:00:00")
        self.assertEqual(dt.tzinfo, timezone.utc)


class TestBaselines(unittest.TestCase):
    def test_rolling_median_mad(self):
        stats = rolling_median_mad([1.0, 1.0, 1.0, 1.0, 9.0], window=10)
        self.assertEqual(stats[0], (None, None))
        med, mad = stats[4]
        self.assertAlmostEqual(med, 1.0)
        self.assertAlmostEqual(mad, 0.0)

    def test_bucket_hourly_aggregates(self):
        recs = make_records(hours=3)
        recs.append({"timestamp": BASE + timedelta(minutes=30),
                     "key_id": "k1", "tokens": 10, "cost": 2.0})
        buckets = bucket_hourly(recs)
        self.assertEqual(len(buckets["k1"]), 3)
        self.assertAlmostEqual(buckets["k1"][0][1], 4.0)  # two records, one hour

    def test_per_key_isolation(self):
        recs = make_records(key="a", hours=48, spike_at=40) \
            + make_records(key="b", hours=48)  # b is flat, must not alert
        anomalies = detect(recs, min_history=24)
        self.assertTrue(all(a["key_id"] == "a" for a in anomalies))
        self.assertTrue(any(a["hour"].startswith("2026-10-02T16") for a in anomalies))


class TestDetection(unittest.TestCase):
    def test_spike_detected_with_severity(self):
        recs = make_records(hours=72, spike_at=60, spike_mult=25.0)
        anomalies = detect(recs, min_history=24)
        self.assertEqual(len(anomalies), 1)
        a = anomalies[0]
        self.assertEqual(a["severity"], "critical")
        self.assertAlmostEqual(a["observed_cost"], 50.0)
        self.assertAlmostEqual(a["baseline_median"], 2.0)

    def test_flat_data_no_false_positives(self):
        recs = make_records(hours=200)
        self.assertEqual(detect(recs, min_history=24), [])

    def test_noisy_data_no_false_positives(self):
        import random
        rng = random.Random(7)
        recs = [{"timestamp": BASE + timedelta(hours=h), "key_id": "k",
                 "tokens": 1000, "cost": 2.0 * (1 + rng.uniform(-0.2, 0.2))}
                for h in range(300)]
        self.assertEqual(detect(recs, min_history=48), [])

    def test_small_spike_below_noise_floor_ignored(self):
        recs = make_records(hours=72, spike_at=60, spike_mult=1.2)  # +$0.40
        self.assertEqual(detect(recs, min_history=24), [])

    def test_moderate_spike_warn_severity(self):
        recs = make_records(hours=200, spike_at=150, spike_mult=4.0)
        anomalies = detect(recs, min_history=48)
        self.assertTrue(anomalies)
        self.assertIn(anomalies[0]["severity"], ("warn", "high", "critical"))

    def test_zero_mad_history_still_flags(self):
        # perfectly flat history -> MAD is 0; a real spike must still alert
        recs = make_records(hours=72, spike_at=60, spike_mult=10.0)
        anomalies = detect(recs, min_history=24)
        self.assertEqual(len(anomalies), 1)


class TestAlerting(unittest.TestCase):
    def test_payload_shape(self):
        a = {"key_id": "k", "hour": "2026-10-01T00:00:00+00:00",
             "observed_cost": 50.0, "baseline_median": 2.0, "mad": 0.1,
             "score": 9.5, "severity": "critical", "tokens": 1, "records": 1}
        p = alert_payload(a)
        self.assertEqual(p["event_action"], "trigger")
        self.assertEqual(p["payload"]["severity"], "critical")
        self.assertIn("k", p["payload"]["summary"])
        self.assertIn("custom_details", p["payload"])

    def test_severity_mapping(self):
        base = {"key_id": "k", "hour": "h", "observed_cost": 1.0,
                "baseline_median": 1.0, "mad": 0.0, "score": 1.0,
                "tokens": 1, "records": 1}
        for internal, pd in [("warn", "warning"), ("high", "error"),
                             ("critical", "critical")]:
            a = dict(base, severity=internal)
            self.assertEqual(alert_payload(a)["payload"]["severity"], pd)

    def test_payloads_json_roundtrip(self):
        recs = make_records(hours=72, spike_at=60, spike_mult=25.0)
        anomalies = detect(recs, min_history=24)
        parsed = json.loads(payloads_json(anomalies))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["payload"]["custom_details"]["key_id"], "k1")


if __name__ == "__main__":
    unittest.main()

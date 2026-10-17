# llm-cost-anomaly-alerter

**Your LLM API bill spiked at 3AM. You'll find out in 30 days, on the invoice.**

A rogue script, a leaked key, a runaway loop — LLM spend anomalies are silent until the billing email arrives. This tool watches per-API-key usage and shouts the moment a key's hourly spend deviates from its own rolling baseline, with severity levels and webhook-ready alert payloads. The demo below detects an injected 25× cost spike in under a second.

## Quickstart

```bash
# 1. Run the 30-second demo (generates 14 days of usage, injects a spike, detects it)
python demo.py

# 2. Point it at your own usage export (CSV or JSONL)
python -c "
from alerter import load_records, detect, payloads_json
records, skipped = load_records('my_usage.csv')   # or .jsonl
anomalies = detect(records)
print(payloads_json(anomalies))                    # POST to your webhook
"
```

Input format (CSV header or JSONL keys): `timestamp` (ISO-8601), `key_id`, `tokens`, `cost`.

## How it works

1. **Ingest** — CSV/JSONL adapters normalise OpenAI-style usage exports into
   `(timestamp, key_id, tokens, cost)` records. Malformed rows are counted and skipped.
2. **Baseline** — per key, usage is bucketed hourly; each bucket is scored against the
   rolling median ± MAD (median absolute deviation) of the previous N buckets
   (default: 168 = one week). Median/MAD is used instead of mean/stddev because
   one past spike shouldn't move the baseline.
3. **Detect** — upward deviations only, above a $1 noise floor, scored in MAD units:
   `warn ≥ 3`, `high ≥ 5`, `critical ≥ 8`.
4. **Alert** — each anomaly becomes a PagerDuty-Events-v2-shaped JSON payload
   (`trigger` action, `summary`, `severity`, `custom_details`) you can POST to
   PagerDuty, Slack, or any webhook.

## Example output

Captured from a fresh `python demo.py` run (1,008 hourly rows, 1 injected 25× spike):

```
records analysed : 1008 hourly usage rows (skipped 0)
keys monitored   : 3
anomalies found  : 1
analysis time    : <0.20s

[CRITICAL] prod-key-2 @ 2026-10-13T13:00:00+00:00  spent $27.50 vs baseline $1.17/hr  (score 104.69)

Alert payload (PagerDuty-style, POST to any webhook):
[
  {
    "event_action": "trigger",
    "payload": {
      "custom_details": {
        "baseline_median": 1.1669,
        "hour": "2026-10-13T13:00:00+00:00",
        "key_id": "prod-key-2",
        "mad": 0.1696,
        "mad_score": 104.69,
        "observed_cost": 27.5,
        "tokens": 2062500
      },
      "severity": "critical",
      "source": "llm-cost-anomaly-alerter",
      "summary": "LLM cost anomaly [critical]: key 'prod-key-2' spent $27.50 in hour 2026-10-13T13:00:00+00:00 vs baseline $1.17/hr"
    },
    "routing_key": "REPLACE_WITH_PAGERDUTY_ROUTING_KEY"
  }
]
```

## What it does NOT do

- It is **not a live billing integration** — it analyses usage exports you feed it
  (CSV/JSONL). Real-time detection means scheduling it against fresh exports.
- It needs **real usage history to be useful**: a key needs ~24+ hours of history
  before it can alert (configurable via `min_history`).
- It detects **cost spikes**, not the cause — it won't tell you *which* prompt or
  user burned the tokens, only which key, which hour, and how far off baseline.
- Baselines are per-key and backward-looking; a slow, gradual spend creep
  (boiling-frog) will eventually become the new normal and stop alerting.
- The demo data is synthetic. The detection math is real; the dollars are not.

## Tuning

```python
detect(records,
       window=168,            # baseline = previous N hourly buckets
       thresholds={"warn": 3.0, "high": 5.0, "critical": 8.0},  # MAD scores
       min_history=24,        # buckets of history before a key can alert
       min_abs_deviation=1.0) # ignore deviations under $1
```

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

MIT — see [LICENSE](LICENSE).

## Built by Ingenious AI
- DisclosureGuard — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20DisclosureGuard.
- ModelMinder — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ModelMinder.
- TermsWatch — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20TermsWatch.
- Vibe-Code Audit — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Vibe-Code%20Audit.
- Rogue-Agent Defense — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Rogue-Agent%20Defense.
- BreachHunter — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20BreachHunter.
- ChatBreaker — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ChatBreaker.
- Agent Flight Recorder — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Agent%20Flight%20Recorder.
- Agent Auth Gateway — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Agent%20Auth%20Gateway.
- DomainGuard — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20DomainGuard.
- ContentCraft AI — https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ContentCraft%20AI.
Built with Muse — try it free: invite code 2HCQH1

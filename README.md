# Triathlon PMC

A personal training load management tool for triathletes. Syncs Garmin Connect activity data, computes CTL/ATL/TSB (the Performance Management Chart), overlays sleep and HRV trends, and includes a streaming AI coach powered by Claude Opus.

Built for my own training — 5 years of data, runs entirely locally.

---

## What It Does

- Syncs activities from Garmin Connect (swim, bike, run, strength)
- Computes Training Stress Score (TSS) per activity using heart rate and power
- Calculates Chronic Training Load (CTL), Acute Training Load (ATL), and Training Stress Balance (TSB) over time
- Overlays sleep score and HRV from Garmin health data
- Interactive Chart.js visualization with toggleable data layers
- Streaming AI coach (Claude Opus) with full training history context — ask anything about your fitness, fatigue, readiness

---

## Setup

### Requirements
- Python 3.10+
- Garmin Connect account
- Anthropic API key ([get one here](https://console.anthropic.com))
- Garmin device with HRV/sleep support (Forerunner 255+, Fenix 6+, Venu 2+, or equivalent)

### Install

```bash
git clone https://github.com/rafrodriguezn/triathlon-pmc
cd triathlon-pmc
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` with your credentials:

```
ANTHROPIC_API_KEY=your-anthropic-key
LTHR=155
SYNC_DAYS=365
```

### Sync Data

Garmin Connect blocks automated login via Cloudflare, so syncing requires a one-time browser cookie step:

1. Open Garmin Connect in Safari or Chrome
2. Open DevTools → Network tab
3. Trigger any API request (load your dashboard)
4. Right-click the request → Copy as cURL
5. Paste into `curl_command.txt` in the project root
6. Run: `python sync_from_browser.py`

This syncs your activity history into `cache.json`. Re-run whenever you want to update.

### Run

```bash
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000)

---

## Configuration

All config lives in `.env`:

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key | required |
| `LTHR` | Lactate Threshold Heart Rate | required |
| `SYNC_DAYS` | How many days of history to sync | 365 |
| `CLAUDE_MODEL` | Claude model to use for coaching | claude-opus-4-6 |

**Finding your LTHR:** Run a hard 30-60 min effort at maximum sustainable pace. Average HR is approximately your LTHR. Or use 85-90% of your max HR as an estimate.

---

## Important Notes

- `cache.json` (your activity data) is excluded from this repo via `.gitignore` — your data stays local
- `.env` is also excluded — never commit your API key
- `.garth/` (Garmin auth tokens) is excluded
- The browser cURL method gives a session that lasts a few hours — re-copy when sync fails

---

## Stack

- Python / Flask
- Chart.js
- Claude API (Anthropic) — streaming responses
- Garmin Connect (browser cookie auth)
- SQLite-style local cache (JSON)

---

## Cost

The AI coach uses Claude Opus 4.6 by default. Typical conversation: $0.02–0.05. You can switch to `claude-sonnet-4-6` in `.env` for lower cost with similar quality.

---

## License

MIT — use it, fork it, adapt it for your sport.

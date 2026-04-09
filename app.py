"""
Flask backend for Triathlon PMC.

Routes:
  GET  /                     → serve index.html
  GET  /api/pmc?days=180     → PMC data from cache
  GET  /api/sync             → pull fresh data from Garmin, update cache
  GET  /api/status           → cache metadata
  POST /api/chat             → streaming Claude training coach (SSE)
"""

import csv
import io
import json
import logging
import os
import pathlib
from datetime import date, datetime, timedelta

import anthropic
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from calculations import compute_pmc
from garmin_client import (
    activities_to_daily_tss,
    build_client,
    fetch_activities,
    load_cache,
    save_cache,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# Garmin client — created once at startup, reused for all /api/sync calls.
# Build lazily so the server starts even without credentials (for offline use).
_garmin_client = None


def get_garmin_client():
    global _garmin_client
    if _garmin_client is None:
        _garmin_client = build_client()  # may raise — caller handles it
    return _garmin_client


def reset_garmin_client():
    """Call this after a failed sync so the next attempt retries auth."""
    global _garmin_client
    _garmin_client = None


# ---------------------------------------------------------------------------
# Sleep data helpers
# ---------------------------------------------------------------------------
SLEEP_CACHE_FILE = pathlib.Path("sleep_cache.json")


def load_sleep_cache() -> dict:
    if SLEEP_CACHE_FILE.exists():
        try:
            raw = json.loads(SLEEP_CACHE_FILE.read_text())
            # Migrate legacy flat format {date: float} → {date: {score: float}}
            migrated = {}
            for k, v in raw.items():
                migrated[k] = v if isinstance(v, dict) else {"score": float(v)}
            return migrated
        except Exception:
            return {}
    return {}


def save_sleep_cache(data: dict):
    SLEEP_CACHE_FILE.write_text(json.dumps(data, sort_keys=True, indent=2))


LTHR = float(os.getenv("LTHR", "155"))
SYNC_DAYS = int(os.getenv("SYNC_DAYS", "1825"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    cache = load_cache()
    return jsonify(
        {
            "synced_at": cache.get("synced_at"),
            "activity_count": cache.get("count", 0),
            "cache_file_exists": (
                __import__("pathlib").Path("cache.json").exists()
            ),
        }
    )


@app.route("/api/sync")
def api_sync():
    """Pull fresh activities from Garmin → write cache.json → return summary.
    Also attempts sleep sync via curl_command.txt if present (browser cookie method).
    """
    # --- Activities ---
    try:
        client = get_garmin_client()
        activities = fetch_activities(client, days=SYNC_DAYS)
        save_cache(activities)
        activities_msg = f"Synced {len(activities)} activities."
    except Exception as exc:
        log.exception("Activity sync failed")
        reset_garmin_client()
        return jsonify({"ok": False, "error": str(exc)}), 500

    # --- Sleep (best-effort via browser cookie) ---
    sleep_msg = None
    curl_file = pathlib.Path("curl_command.txt")
    if curl_file.exists():
        try:
            from sync_from_browser import (
                extract_cookie,
                extract_csrf_token,
                fetch_sleep_scores,
            )
            curl_text  = curl_file.read_text()
            cookie_str = extract_cookie(curl_text)
            csrf_token = extract_csrf_token(curl_text)
            sleep_data = fetch_sleep_scores(cookie_str, csrf_token)
            if sleep_data:
                save_sleep_cache(sleep_data)
                sleep_msg = f"{len(sleep_data)} sleep days synced."
                log.info("Sleep sync OK: %d days", len(sleep_data))
        except Exception as exc:
            log.warning("Sleep sync skipped: %s", exc)

    return jsonify({
        "ok": True,
        "activities_fetched": len(activities),
        "message": activities_msg + (f" {sleep_msg}" if sleep_msg else ""),
    })


@app.route("/api/sleep")
def api_sleep():
    """Return stored sleep scores keyed by date."""
    return jsonify({"ok": True, "sleep": load_sleep_cache()})


@app.route("/api/sync_sleep")
def api_sync_sleep():
    """Sync sleep scores via browser cookie (curl_command.txt). Same flow as sync_from_browser.py."""
    curl_file = pathlib.Path("curl_command.txt")
    if not curl_file.exists():
        return jsonify({
            "ok": False,
            "error": "curl_command.txt not found. Copy a cURL from Garmin Connect DevTools first.",
        }), 400
    try:
        from sync_from_browser import (
            extract_cookie,
            extract_csrf_token,
            fetch_sleep_scores,
        )
        curl_text  = curl_file.read_text()
        cookie_str = extract_cookie(curl_text)
        csrf_token = extract_csrf_token(curl_text)
        sleep_data = fetch_sleep_scores(cookie_str, csrf_token)
        if not sleep_data:
            return jsonify({"ok": False, "error": "No sleep data returned — endpoint may differ. Try the CSV upload fallback."}), 502
        save_sleep_cache(sleep_data)
        return jsonify({"ok": True, "days": len(sleep_data), "message": f"{len(sleep_data)} sleep days synced."})
    except Exception as exc:
        log.exception("Sleep sync failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/sleep/upload", methods=["POST"])
def api_sleep_upload():
    """
    Accept a Garmin sleep export CSV (or any CSV with date + sleep score columns).
    Merges into sleep_cache.json.  Returns count of days added / total stored.
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    raw = request.files["file"].read().decode("utf-8-sig")  # handles BOM
    reader = csv.DictReader(io.StringIO(raw))
    headers = [h.strip() for h in (reader.fieldnames or [])]

    # Flexible column detection
    date_col = next(
        (h for h in headers if h.lower() in ("date", "calendar date", "day")),
        None,
    )
    score_col = next(
        (h for h in headers if "sleep score" in h.lower() or h.lower() == "score"),
        None,
    )

    if not date_col or not score_col:
        return jsonify({
            "ok": False,
            "error": f"Could not find date/score columns. Headers found: {headers}",
        }), 400

    existing = load_sleep_cache()
    added = 0
    for row in reader:
        raw_date = row.get(date_col, "").strip()
        raw_score = row.get(score_col, "").strip()
        if not raw_date or not raw_score:
            continue
        try:
            score = float(raw_score)
        except ValueError:
            continue
        # Normalize date to YYYY-MM-DD
        parsed = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        if not parsed:
            continue
        existing[parsed] = score
        added += 1

    save_sleep_cache(existing)
    log.info("Sleep upload: %d days added, %d total", added, len(existing))
    return jsonify({"ok": True, "added": added, "total": len(existing)})


@app.route("/api/pmc")
def api_pmc():
    """
    Return PMC data for the requested date window.

    Query params:
      days=180   — how many days from today to return (default 180)
    """
    days = int(request.args.get("days", 180))
    end_date = date.today()
    start_date = end_date - timedelta(days=days - 1)

    cache = load_cache()
    activities = cache.get("activities", [])

    if not activities:
        return jsonify(
            {
                "ok": False,
                "error": "No cached data. Click 'Sync Garmin' first.",
            }
        ), 404

    daily_tss, daily_names = activities_to_daily_tss(activities, LTHR)

    pmc_rows = compute_pmc(
        tss_by_date=daily_tss,
        start_date=start_date,
        end_date=end_date,
        warmup_days=60,
    )

    # Attach activity names for tooltip
    for row in pmc_rows:
        row["activities"] = daily_names.get(row["date"], [])

    return jsonify(
        {
            "ok": True,
            "synced_at": cache.get("synced_at"),
            "lthr": LTHR,
            "rows": pmc_rows,
        }
    )


def _build_sleep_context() -> str:
    """Summarize sleep score + HRV data for injection into the coach prompt."""
    sleep = load_sleep_cache()
    if not sleep:
        return ""

    today = date.today()

    def _score(v): return v.get("score") if isinstance(v, dict) else float(v) if v else None
    def _hrv(v):   return v.get("hrv")   if isinstance(v, dict) else None

    # Last 30 days averages
    recent = {k: v for k, v in sleep.items()
              if k >= (today - timedelta(days=30)).isoformat()}
    scores_30  = [s for s in (_score(v) for v in recent.values()) if s is not None]
    hrv_30     = [h for h in (_hrv(v)   for v in recent.values()) if h is not None]
    avg_score  = sum(scores_30) / len(scores_30) if scores_30 else None
    avg_hrv    = sum(hrv_30)    / len(hrv_30)    if hrv_30    else None

    # Monthly averages
    monthly_scores: dict = {}
    monthly_hrv: dict    = {}
    for d, v in sleep.items():
        ym = d[:7]
        s  = _score(v)
        h  = _hrv(v)
        if s is not None: monthly_scores.setdefault(ym, []).append(s)
        if h is not None: monthly_hrv.setdefault(ym, []).append(h)

    score_lines = "  ".join(
        f"{ym}: {sum(v)/len(v):.0f}" for ym, v in sorted(monthly_scores.items())
    )
    hrv_lines = "  ".join(
        f"{ym}: {sum(v)/len(v):.0f}" for ym, v in sorted(monthly_hrv.items())
    )

    lines = ["\nSleep data (Garmin):"]
    if avg_score is not None:
        lines.append(f"- Avg sleep score last 30d: {avg_score:.0f}/100 ({len(scores_30)} nights)")
    if avg_hrv is not None:
        lines.append(f"- Avg overnight HRV last 30d: {avg_hrv:.0f} ms ({len(hrv_30)} nights)")
    if score_lines:
        lines.append(f"- Monthly sleep scores:  {score_lines}")
    if hrv_lines:
        lines.append(f"- Monthly HRV averages:  {hrv_lines}")
    return "\n".join(lines)


def _build_pmc_context() -> str:
    """Build PMC summary to inject into the coach's system prompt."""
    cache = load_cache()
    activities = cache.get("activities", [])
    if not activities:
        return "No training data loaded yet."

    daily_tss, daily_names = activities_to_daily_tss(activities, LTHR)
    today = date.today()

    # Full history for long-term analysis
    full_start = today - timedelta(days=1825)
    all_rows = compute_pmc(daily_tss, full_start, today, warmup_days=60)

    if not all_rows:
        return "No recent training data."

    latest = all_rows[-1]

    # Last 7 days workouts
    last_7 = [r for r in all_rows if r["date"] >= (today - timedelta(days=6)).isoformat()]
    weekly_tss = sum(r["tss"] for r in last_7)
    recent_workouts = []
    for r in all_rows[-7:]:
        names = daily_names.get(r["date"], [])
        if names:
            recent_workouts.append(f"{r['date']}: {', '.join(names)} (TSS {r['tss']})")

    # Monthly CTL summary (first day of each month)
    seen_months = set()
    monthly_ctl = []
    for r in all_rows:
        ym = r["date"][:7]
        if ym not in seen_months:
            seen_months.add(ym)
            monthly_ctl.append(f"{r['date'][:7]}: CTL {r['ctl']:.0f}")

    # Yearly TSS totals
    yearly_tss: dict = {}
    for r in all_rows:
        yr = r["date"][:4]
        yearly_tss[yr] = yearly_tss.get(yr, 0) + r["tss"]
    yearly_summary = "  ".join(f"{yr}: {int(tss)} TSS" for yr, tss in sorted(yearly_tss.items()))

    # Peak CTL ever
    peak = max(all_rows, key=lambda r: r["ctl"])

    # Monthly sport breakdown — TSS and session count by sport category
    SPORT_GROUPS = {
        "cycling": {"road_biking","cycling","indoor_cycling","virtual_ride","gravel_cycling","mountain_biking","track_cycling"},
        "running": {"running","trail_running","treadmill_running"},
        "swimming": {"lap_swimming","open_water_swimming","swimming"},
        "strength": {"strength_training"},
        "other": set(),
    }
    monthly_breakdown: dict = {}
    for act in activities:
        d = (act.get("startTimeLocal") or "")[:10]
        if not d:
            continue
        ym = d[:7]
        type_key = (act.get("activityType") or {}).get("typeKey", "")
        sport = "other"
        for grp, keys in SPORT_GROUPS.items():
            if type_key in keys:
                sport = grp
                break
        from garmin_client import _compute_tss
        tss = _compute_tss(act, LTHR)
        if ym not in monthly_breakdown:
            monthly_breakdown[ym] = {}
        if sport not in monthly_breakdown[ym]:
            monthly_breakdown[ym][sport] = {"tss": 0, "count": 0}
        monthly_breakdown[ym][sport]["tss"] += tss
        monthly_breakdown[ym][sport]["count"] += 1

    monthly_sport_lines = []
    for ym in sorted(monthly_breakdown.keys()):
        parts = []
        for grp in ["cycling","running","swimming","strength","other"]:
            d = monthly_breakdown[ym].get(grp)
            if d and d["count"] > 0:
                parts.append(f"{grp}:{d['count']}x/{int(d['tss'])}TSS")
        monthly_sport_lines.append(f"{ym}: {' | '.join(parts)}")

    return f"""Athlete profile: Age 34 (DOB Aug 12 1991)

Current training metrics (as of {latest['date']}):
- CTL (Fitness): {latest['ctl']:.1f}
- ATL (Fatigue): {latest['atl']:.1f}
- TSB (Form):    {latest['tsb']:.1f}  {'(fresh/rested)' if latest['tsb'] > 5 else '(fatigued)' if latest['tsb'] < -10 else '(neutral)'}
- TSS last 7 days: {weekly_tss:.0f}
- Peak CTL ever: {peak['ctl']:.1f} on {peak['date']}

Annual TSS totals:
{yearly_summary}

Monthly CTL history (first of each month):
{chr(10).join(monthly_ctl)}

Monthly sport breakdown (sessions x TSS per sport):
{chr(10).join(monthly_sport_lines)}

Recent workouts:
{chr(10).join(recent_workouts) if recent_workouts else 'No workouts in last 7 days.'}
{_build_sleep_context()}"""


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """
    Streaming training coach chat powered by Claude Opus 4.6.
    Expects JSON body: { "messages": [{"role": "user"|"assistant", "content": "..."}] }
    Returns Server-Sent Events stream.
    """
    body = request.get_json(force=True, silent=True) or {}
    messages = body.get("messages", [])

    if not messages:
        return jsonify({"error": "No messages provided."}), 400

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env"}), 500

    pmc_context = _build_pmc_context()

    system_prompt = f"""You are an expert triathlon coach and sports scientist specializing in training load management.
You have access to the athlete's live Performance Manager Chart (PMC) data.

{pmc_context}

PMC metric guide:
- CTL (Chronic Training Load) = fitness, built over ~6 weeks. Higher = more fit.
- ATL (Acute Training Load) = fatigue, built over ~1 week. Higher = more tired.
- TSB (Training Stress Balance) = CTL - ATL = form. Positive = fresh, negative = fatigued.
- TSS (Training Stress Score) = stress from a single workout.

Typical TSB interpretation:
  > +25: Very fresh — good for racing, risk of detraining
  +5 to +25: Race-ready form
  -10 to +5: Neutral — normal training
  -20 to -10: Moderate fatigue — hard training block
  < -30: Heavy fatigue — risk of overtraining

Keep responses concise and practical. Use the athlete's actual numbers when giving advice."""

    def generate():
        claude = anthropic.Anthropic(api_key=api_key)
        with claude.messages.stream(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'text': text})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting Triathlon PMC server on http://localhost:5000")
    log.info(
        "LTHR=%s  SYNC_DAYS=%s  GARMIN_EMAIL=%s",
        LTHR,
        SYNC_DAYS,
        os.getenv("GARMIN_EMAIL", "(not set)"),
    )
    app.run(debug=True, port=5000, use_reloader=False)

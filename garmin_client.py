"""
Garmin Connect data layer.

Responsibilities:
 - Authenticate once (singleton) — handles MFA prompt on first login.
 - Fetch activities with pagination (max 100/call, 0.5 s sleep between pages).
 - Extract per-activity TSS:
     Cycling with power → trainingStressScore field
     Everything else   → hrTSS = (duration_hrs) * (avg_HR / LTHR)^2 * 100
 - Aggregate to daily TSS dict.
 - Persist/load cache.json so the Flask server never calls Garmin on page load.
"""

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import garminconnect
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "cache.json"

# Activity type keys that indicate cycling (power TSS available)
CYCLING_TYPES = {
    "road_biking",
    "cycling",
    "indoor_cycling",
    "virtual_ride",
    "gravel_cycling",
    "mountain_biking",
    "bmx",
    "track_cycling",
}


def _get_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


GARTH_TOKEN_DIR = Path.home() / ".garth"


def build_client() -> garminconnect.Garmin:
    """
    Create and authenticate a Garmin client.

    Strategy (avoids rate-limit 429 errors):
      1. Try loading saved OAuth tokens from ~/.garth/  — silent, no HTTP login.
      2. Only fall back to username/password if tokens are missing or expired.
         On first-ever login, Garmin may send an MFA code to your email —
         enter it in the terminal. garth then saves tokens for all future runs.
    """
    email = _get_env("GARMIN_EMAIL")
    password = _get_env("GARMIN_PASSWORD")

    client = garminconnect.Garmin(email, password)

    if GARTH_TOKEN_DIR.exists():
        try:
            client.login(tokenstore=str(GARTH_TOKEN_DIR))
            log.info("Garmin auth OK (token) for %s", email)
            return client
        except Exception as e:
            log.warning("Saved tokens invalid/expired (%s) — falling back to password login.", e)

    # Fresh login — triggers MFA on very first run
    client.login()
    log.info("Garmin auth OK (password) for %s", email)
    return client


def _activity_date(activity: dict) -> Optional[str]:
    """Return ISO date string from startTimeLocal, or None if missing."""
    raw = activity.get("startTimeLocal")
    if not raw:
        return None
    # Garmin returns "YYYY-MM-DD HH:MM:SS" or ISO 8601
    try:
        return raw[:10]
    except Exception:
        return None


def _compute_tss(activity: dict, lthr: float) -> float:
    """
    Return TSS for a single activity.

    Priority:
      1. trainingStressScore (field set by Garmin for power-meter rides)
      2. hrTSS formula
      3. 0 (no HR data)
    """
    # 1. Power-based TSS (cycling only, only if field is populated and > 0)
    activity_type = (activity.get("activityType") or {}).get("typeKey", "")
    if activity_type in CYCLING_TYPES:
        tss_field = activity.get("trainingStressScore")
        if tss_field and tss_field > 0:
            return min(float(tss_field), 600.0)

    # 2. hrTSS
    duration_secs = activity.get("duration", 0) or 0
    avg_hr = activity.get("averageHR", 0) or 0

    if duration_secs <= 0 or avg_hr <= 0 or lthr <= 0:
        return 0.0

    duration_hrs = duration_secs / 3600.0
    hr_ratio = avg_hr / lthr
    tss = duration_hrs * (hr_ratio ** 2) * 100.0
    return min(tss, 600.0)  # cap at 600 — eliminates corrupt/forgotten recordings


def fetch_activities(
    client: garminconnect.Garmin,
    days: int = 365,
) -> List[dict]:
    """
    Pull activities from Garmin going back `days` days.
    Paginates in chunks of 100 with 0.5 s sleep between requests.
    Returns raw activity dicts.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    all_activities: List[dict] = []
    start = 0
    limit = 100

    while True:
        log.info("Fetching activities %d–%d …", start, start + limit)
        batch = client.get_activities(start, limit)
        if not batch:
            break

        for act in batch:
            act_date = _activity_date(act)
            if act_date and act_date < cutoff:
                log.info("Reached cutoff date %s — stopping pagination.", cutoff)
                return all_activities
            all_activities.append(act)

        if len(batch) < limit:
            break  # last page

        start += limit
        time.sleep(0.5)

    log.info("Fetched %d total activities.", len(all_activities))
    return all_activities


SPORT_EMOJI = {
    "running":           "🏃",
    "road_biking":       "🚴",
    "cycling":           "🚴",
    "indoor_cycling":    "🚴",
    "virtual_ride":      "🚴",
    "gravel_cycling":    "🚴",
    "mountain_biking":   "🚵",
    "lap_swimming":      "🏊",
    "open_water_swimming": "🏊",
    "swimming":          "🏊",
    "trail_running":     "🏃",
    "treadmill_running": "🏃",
    "strength_training": "🏋️",
    "yoga":              "🧘",
}


def _fmt_duration(secs: float) -> str:
    secs = int(secs or 0)
    h, m = divmod(secs // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def activities_to_daily_tss(
    activities: List[dict],
    lthr: float,
) -> Dict[str, float]:
    """
    Convert raw activity list to {iso_date: total_tss} dict.
    Multiple workouts on the same day are summed.
    Also returns a dict of {iso_date: [rich_label, ...]} for tooltips.
    Each label format:  "🏃 Long Run · 1h15m · TSS 62"
    """
    daily_tss: Dict[str, float] = {}
    daily_names: Dict[str, List[str]] = {}

    for act in activities:
        d = _activity_date(act)
        if not d:
            continue
        tss = _compute_tss(act, lthr)
        daily_tss[d] = daily_tss.get(d, 0.0) + tss

        type_key = (act.get("activityType") or {}).get("typeKey", "")
        emoji = SPORT_EMOJI.get(type_key, "🏋️")
        name = act.get("activityName") or type_key.replace("_", " ").title() or "Workout"
        duration = _fmt_duration(act.get("duration", 0))
        label = f"{emoji} {name} · {duration} · TSS {tss:.0f}"
        daily_names.setdefault(d, []).append(label)

    return daily_tss, daily_names


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def save_cache(activities: List[dict]) -> None:
    data = {
        "synced_at": datetime.utcnow().isoformat() + "Z",
        "count": len(activities),
        "activities": activities,
    }
    CACHE_FILE.write_text(json.dumps(data, indent=2))
    log.info("Saved %d activities to %s", len(activities), CACHE_FILE)


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {"synced_at": None, "count": 0, "activities": []}
    try:
        return json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        log.warning("cache.json is corrupt — returning empty cache.")
        return {"synced_at": None, "count": 0, "activities": []}


# ---------------------------------------------------------------------------
# Quick auth test: python garmin_client.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = build_client()
    activities = fetch_activities(client, days=30)
    print(f"Fetched {len(activities)} activities in last 30 days.")

    # Print unique activity type keys seen — useful for mapping
    type_keys = {(a.get("activityType") or {}).get("typeKey") for a in activities}
    print("Activity type keys found:", sorted(k for k in type_keys if k))

    lthr = float(os.getenv("LTHR", "155"))
    daily_tss, daily_names = activities_to_daily_tss(activities, lthr)
    print("\nDaily TSS (last 30 days):")
    for d in sorted(daily_tss):
        print(f"  {d}: {daily_tss[d]:.1f}  — {', '.join(daily_names[d])}")

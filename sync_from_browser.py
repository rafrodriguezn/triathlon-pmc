"""
One-time sync using your browser's Garmin session cookies.
Reads cookie from curl_command.txt (Copy as cURL from Chrome DevTools).
Fetches both activities and sleep scores in one pass.
"""

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)

ACTIVITIES_URL   = "https://connect.garmin.com/gc-api/activitylist-service/activities/search/activities"
SLEEP_DAY_URL    = "https://connect.garmin.com/gc-api/sleep-service/sleep/dailySleepData"
HRV_DAY_URL      = "https://connect.garmin.com/gc-api/hrv-service/hrv/{date}"
SLEEP_DAYS_BACK  = 1825  # 5 years — matches activity history window
CACHE_FILE       = Path(__file__).parent / "cache.json"
SLEEP_CACHE_FILE = Path(__file__).parent / "sleep_cache.json"
CURL_FILE        = Path(__file__).parent / "curl_command.txt"
DAYS_BACK        = 1825


def extract_cookie(curl_text: str) -> str:
    match = re.search(r"-H 'Cookie: ([^']+)'", curl_text)
    if not match:
        match = re.search(r'-H "Cookie: ([^"]+)"', curl_text)
    if not match:
        raise RuntimeError("Could not find Cookie header in curl_command.txt")
    return match.group(1)


def extract_csrf_token(curl_text: str) -> str:
    match = re.search(r"-H 'Connect-Csrf-Token: ([^']+)'", curl_text)
    if not match:
        match = re.search(r'-H "Connect-Csrf-Token: ([^"]+)"', curl_text)
    return match.group(1) if match else ""


def debug_hrv(cookie_str: str, csrf_token: str = "", check_date: str = "2025-06-15") -> None:
    """Print raw HRV endpoint response for a given date — used to find correct field names."""
    import requests as _req
    headers = get_headers(cookie_str, csrf_token)
    url = HRV_DAY_URL.format(date=check_date)
    print(f"\n--- HRV debug: GET {url}")
    r = _req.get(url, headers=headers, timeout=15)
    print(f"    Status: {r.status_code}")
    print(f"    Body  : {r.text[:800]}")
    print()


def get_headers(cookie_str: str, csrf_token: str = "") -> dict:
    headers = {
        "Cookie": cookie_str,
        "Accept": "*/*",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.5 Safari/605.1.15"
        ),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if csrf_token:
        headers["Connect-Csrf-Token"] = csrf_token
    return headers


def fetch_activities(cookie_str: str, csrf_token: str = "") -> list:
    headers  = get_headers(cookie_str, csrf_token)
    cutoff   = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
    all_acts = []
    start    = 0
    limit    = 100

    while True:
        log.info("Fetching activities %d – %d …", start, start + limit)
        resp = requests.get(
            ACTIVITIES_URL,
            headers=headers,
            params={"start": start, "limit": limit},
            timeout=30,
        )

        if resp.status_code == 403:
            print("\n❌ Session expired. Refresh connect.garmin.com, copy a new cURL, save to curl_command.txt, and retry.")
            raise SystemExit(1)

        resp.raise_for_status()
        batch = resp.json()
        log.info("Response type: %s, first 200 chars: %s", type(batch).__name__, str(batch)[:200])

        if not batch:
            break

        for act in batch:
            act_date = (act.get("startTimeLocal") or "")[:10]
            if act_date and act_date < cutoff:
                log.info("Reached cutoff %s — done.", cutoff)
                return all_acts
            all_acts.append(act)

        if len(batch) < limit:
            break

        start += limit
        time.sleep(0.3)

    return all_acts


def _parse_sleep_entry(dto: dict) -> dict:
    """Extract score and HRV from a dailySleepDTO object."""
    result = {}

    # Sleep score — try multiple field names across Garmin API versions
    score = (
        dto.get("overallSleepScore")
        or dto.get("sleepScore")
        or dto.get("totalSleepScore")
        or (dto.get("sleepScores") or {}).get("overall", {}).get("value")
        or (dto.get("sleepScores") or {}).get("totalDuration", {}).get("value")
    )
    if score is not None:
        try:
            result["score"] = float(score)
        except (ValueError, TypeError):
            pass

    # Overnight HRV
    hrv = (
        dto.get("avgOvernightHrv")
        or dto.get("averageHRV")
        or dto.get("hrv")
        or (dto.get("hrvData") or {}).get("lastNight5MinHigh")
        or (dto.get("hrvSummary") or {}).get("lastNight")
    )
    if hrv is not None:
        try:
            result["hrv"] = float(hrv)
        except (ValueError, TypeError):
            pass

    return result


def fetch_sleep_scores(cookie_str: str, csrf_token: str = "", days: int = SLEEP_DAYS_BACK) -> dict:
    """
    Fetch daily sleep scores + HRV from Garmin per-day endpoint.

    Smart caching: skips dates already in sleep_cache.json so re-runs only
    fetch new days.  First run fetches `days` days of history (~365 by default).
    Returns merged {iso_date: {score, hrv}} dict.
    """
    # Load existing cache and migrate legacy flat format
    existing: dict = {}
    if SLEEP_CACHE_FILE.exists():
        try:
            raw = json.loads(SLEEP_CACHE_FILE.read_text())
            for k, v in raw.items():
                existing[k] = v if isinstance(v, dict) else {"score": float(v)}
        except Exception:
            pass

    headers    = get_headers(cookie_str, csrf_token)
    today      = date.today()
    start_date = today - timedelta(days=days)

    all_dates = [start_date + timedelta(days=i) for i in range(days + 1)]

    # Dates needing full sleep+HRV fetch (not cached at all, or missing score)
    dates_full = [
        d for d in all_dates
        if not existing.get(d.isoformat(), {}).get("score")
    ]
    # Dates with sleep score but no HRV — HRV-only backfill
    dates_hrv_only = [
        d for d in all_dates
        if existing.get(d.isoformat(), {}).get("score")
        and not existing.get(d.isoformat(), {}).get("hrv")
    ]

    if not dates_full and not dates_hrv_only:
        log.info("Sleep cache up to date — nothing to fetch.")
        return existing

    log.info("Full sleep+HRV fetch: %d days | HRV backfill: %d days",
             len(dates_full), len(dates_hrv_only))

    fetched = 0

    # --- Pass 1: full fetch for uncached dates ---
    for i, d in enumerate(dates_full):
        try:
            resp = requests.get(
                SLEEP_DAY_URL,
                headers=headers,
                params={"date": d.isoformat(), "nonSleepBufferMinutes": 60},
                timeout=15,
            )
            if resp.status_code == 403:
                log.warning("Sleep 403 at %s — session expired, stopping.", d)
                break
            if resp.ok:
                dto   = resp.json().get("dailySleepDTO") or {}
                entry = _parse_sleep_entry(dto) if isinstance(dto, dict) else {}
            else:
                entry = {}

            hrv_resp = requests.get(
                HRV_DAY_URL.format(date=d.isoformat()),
                headers=headers,
                timeout=15,
            )
            if hrv_resp.status_code == 403:
                log.warning("HRV 403 at %s — session expired, stopping.", d)
                break
            if hrv_resp.ok and hrv_resp.text.strip():
                hrv_summary = (hrv_resp.json().get("hrvSummary") or {})
                hrv_val = (
                    hrv_summary.get("lastNightAvg")
                    or hrv_summary.get("lastNight")
                    or hrv_summary.get("weeklyAvg")
                    or hrv_summary.get("avgOvernightHrv")
                )
                if hrv_val is not None:
                    try:
                        entry["hrv"] = float(hrv_val)
                    except (ValueError, TypeError):
                        pass

            if entry:
                existing[d.isoformat()] = entry
                fetched += 1

            time.sleep(0.15)
            if (i + 1) % 30 == 0:
                log.info("  … %d / %d days done (full pass)", i + 1, len(dates_full))

        except Exception as exc:
            log.warning("Sleep fetch error at %s: %s", d, exc)
            time.sleep(0.5)
            continue

    # --- Pass 2: HRV backfill for dates that have sleep score but no HRV ---
    if dates_hrv_only:
        log.info("Starting HRV backfill for %d days …", len(dates_hrv_only))
        hrv_filled = 0
        for i, d in enumerate(dates_hrv_only):
            try:
                hrv_resp = requests.get(
                    HRV_DAY_URL.format(date=d.isoformat()),
                    headers=headers,
                    timeout=15,
                )
                if hrv_resp.status_code == 403:
                    log.warning("HRV backfill 403 at %s — session expired, stopping.", d)
                    break
                if hrv_resp.ok and hrv_resp.text.strip():
                    hrv_summary = (hrv_resp.json().get("hrvSummary") or {})
                    hrv_val = (
                        hrv_summary.get("lastNightAvg")
                        or hrv_summary.get("lastNight")
                        or hrv_summary.get("weeklyAvg")
                        or hrv_summary.get("avgOvernightHrv")
                    )
                    if hrv_val is not None:
                        try:
                            existing[d.isoformat()]["hrv"] = float(hrv_val)
                            hrv_filled += 1
                            fetched += 1
                        except (ValueError, TypeError):
                            pass

                time.sleep(0.15)
                if (i + 1) % 30 == 0:
                    log.info("  … %d / %d days done (HRV backfill)", i + 1, len(dates_hrv_only))

            except Exception as exc:
                log.warning("HRV backfill error at %s: %s", d, exc)
                time.sleep(0.5)
                continue
        log.info("HRV backfill complete: %d days filled.", hrv_filled)

    log.info("Sleep sync complete: %d new days fetched, %d total cached.", fetched, len(existing))
    return existing


def main():
    if not CURL_FILE.exists():
        print(f"❌ {CURL_FILE} not found.")
        print("   Run:  pbpaste > ~/triathlon-pmc/curl_command.txt")
        raise SystemExit(1)

    curl_text  = CURL_FILE.read_text()
    cookie_str = extract_cookie(curl_text)
    csrf_token = extract_csrf_token(curl_text)
    log.info("Cookie extracted (%d chars), CSRF token: %s", len(cookie_str), csrf_token[:8] + "…" if csrf_token else "not found")

    # Activities
    activities = fetch_activities(cookie_str, csrf_token)
    cache = {
        "synced_at":  datetime.utcnow().isoformat() + "Z",
        "count":      len(activities),
        "activities": activities,
    }
    CACHE_FILE.write_text(json.dumps(cache, indent=2))
    print(f"\n✅  Activities: {len(activities)} synced → cache.json")

    # Sleep scores
    print(f"\n⏳  Fetching sleep data (up to {SLEEP_DAYS_BACK} days, skips cached)…")
    sleep_scores = fetch_sleep_scores(cookie_str, csrf_token)
    if sleep_scores:
        SLEEP_CACHE_FILE.write_text(json.dumps(sleep_scores, sort_keys=True, indent=2))
        has_score = sum(1 for v in sleep_scores.values() if isinstance(v, dict) and v.get("score"))
        has_hrv   = sum(1 for v in sleep_scores.values() if isinstance(v, dict) and v.get("hrv"))
        print(f"✅  Sleep: {len(sleep_scores)} days cached  ({has_score} with score, {has_hrv} with HRV) → sleep_cache.json")
    else:
        print("⚠️   Sleep: no data returned")

    print("\n    Refresh http://127.0.0.1:5000")


if __name__ == "__main__":
    main()


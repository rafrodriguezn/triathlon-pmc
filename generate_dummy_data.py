"""
Generate a realistic fake cache.json for UI preview.
Simulates a triathlon training year: base → build → peak → race → recovery cycles.
Run with:  python generate_dummy_data.py
Then:      python app.py  →  http://127.0.0.1:5000
"""

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

random.seed(42)

TODAY = date.today()
START = TODAY - timedelta(days=365)

# ---------------------------------------------------------------------------
# Training plan skeleton — weekly TSS targets by phase
# Each tuple: (phase_name, weeks, base_weekly_tss, variation)
# ---------------------------------------------------------------------------
PHASES = [
    ("Base 1",       6,  280,  40),
    ("Base 2",       6,  380,  50),
    ("Build 1",      5,  480,  60),
    ("Recovery",     1,  150,  30),
    ("Build 2",      5,  520,  60),
    ("Peak",         3,  580,  50),
    ("Race Week",    1,  200,  30),
    ("Recovery",     2,  160,  30),
    ("Base 1",       4,  300,  40),
    ("Build",        4,  440,  50),
    ("Peak",         2,  500,  40),
    ("Race Week",    1,  180,  20),
    ("Off Season",   8,  200,  60),
]

# Workout templates per sport
WORKOUTS = {
    "run": [
        ("Easy Run",         45,  0.65),
        ("Tempo Run",        55,  0.88),
        ("Long Run",        100,  0.75),
        ("Interval Run",     75,  0.95),
        ("Recovery Jog",     25,  0.55),
    ],
    "bike": [
        ("Endurance Ride",   90,  0.70),
        ("Sweet Spot Ride", 100,  0.85),
        ("Long Ride",       150,  0.72),
        ("Intervals",        85,  0.92),
        ("Recovery Spin",    40,  0.55),
    ],
    "swim": [
        ("Endurance Swim",   45,  0.72),
        ("Threshold Swim",   55,  0.88),
        ("Long Swim",        60,  0.75),
        ("Recovery Swim",    30,  0.60),
    ],
}

LTHR = 164.0

# Typical weekly schedule: (day_of_week 0=Mon, sport, workout_index)
# 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
WEEKLY_PLAN = [
    (0, "swim",  0),   # Mon: endurance swim
    (0, "run",   0),   # Mon: easy run (brick)
    (1, "bike",  0),   # Tue: endurance ride
    (2, "run",   1),   # Wed: tempo run
    (2, "swim",  1),   # Wed: threshold swim
    (3, "bike",  1),   # Thu: sweet spot ride
    (4, "run",   0),   # Fri: easy run
    (4, "swim",  3),   # Fri: recovery swim
    (5, "bike",  2),   # Sat: long ride
    (5, "run",   2),   # Sat: long run (brick)
    (6, "swim",  2),   # Sun: long swim
]


def tss_for_workout(sport: str, idx: int, intensity_multiplier: float) -> float:
    name, base_tss, hr_ratio = WORKOUTS[sport][idx]
    tss = base_tss * intensity_multiplier
    tss *= (0.85 + random.random() * 0.30)  # ±15% day-to-day variation
    return round(max(tss, 10), 1)


def make_activity(workout_date: date, sport: str, idx: int, intensity: float) -> dict:
    name, base_tss, hr_ratio = WORKOUTS[sport][idx]
    tss = tss_for_workout(sport, idx, intensity)
    duration_secs = int((tss / (hr_ratio ** 2 * 100)) * 3600)
    avg_hr = int(LTHR * hr_ratio * (0.95 + random.random() * 0.10))

    type_key_map = {
        "run":  "running",
        "bike": "road_biking",
        "swim": "lap_swimming",
    }

    activity = {
        "activityId": random.randint(10000000, 99999999),
        "activityName": name,
        "startTimeLocal": f"{workout_date.isoformat()} {random.randint(6,9):02d}:{random.randint(0,59):02d}:00",
        "duration": duration_secs,
        "averageHR": avg_hr,
        "activityType": {"typeKey": type_key_map[sport]},
        "trainingStressScore": tss if sport == "bike" and random.random() > 0.3 else None,
    }
    return activity


def build_activities() -> list:
    activities = []
    current = START
    phase_iter = iter(PHASES)
    phase_name, weeks_left, weekly_tss, variation = next(phase_iter)
    week_num = 0

    while current <= TODAY:
        # Advance phase
        if week_num >= weeks_left:
            week_num = 0
            try:
                phase_name, weeks_left, weekly_tss, variation = next(phase_iter)
            except StopIteration:
                phase_name, weeks_left, weekly_tss, variation = ("Base", 52, 280, 40)

        dow = current.weekday()

        # 10% chance of unplanned rest day
        if random.random() < 0.10:
            current += timedelta(days=1)
            if dow == 6:
                week_num += 1
            continue

        # Scale intensity based on phase TSS target vs base
        intensity = (weekly_tss / 500.0) * (0.9 + random.random() * 0.2)
        intensity = max(0.5, min(1.3, intensity))

        for plan_dow, sport, workout_idx in WEEKLY_PLAN:
            if plan_dow == dow:
                # Skip some workouts for realism
                if random.random() < 0.15:
                    continue
                act = make_activity(current, sport, workout_idx, intensity)
                activities.append(act)

        current += timedelta(days=1)
        if dow == 6:
            week_num += 1

    # Sort newest first (matches Garmin API order)
    activities.sort(key=lambda a: a["startTimeLocal"], reverse=True)
    return activities


def main():
    print("Generating dummy training data…")
    activities = build_activities()

    cache = {
        "synced_at": datetime.utcnow().isoformat() + "Z",
        "count": len(activities),
        "_note": "DUMMY DATA — generated by generate_dummy_data.py",
        "activities": activities,
    }

    out = Path(__file__).parent / "cache.json"
    out.write_text(json.dumps(cache, indent=2))

    print(f"✓ Written {len(activities)} activities to cache.json")
    print(f"  Date range: {START.isoformat()} → {TODAY.isoformat()}")
    print()
    print("Now run:  python app.py")
    print("Then open: http://127.0.0.1:5000")
    print()
    print("When done previewing, click 'Sync Garmin' tomorrow to replace with real data.")


if __name__ == "__main__":
    main()

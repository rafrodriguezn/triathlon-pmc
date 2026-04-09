"""
ATL / CTL / TSB calculations using exponential smoothing.
No external dependencies — pure Python.

Formulas (standard TrainingPeaks convention):
  ATL_today = ATL_yesterday * e^(-1/7)  + TSS_today * (1 - e^(-1/7))
  CTL_today = CTL_yesterday * e^(-1/42) + TSS_today * (1 - e^(-1/42))
  TSB_today = CTL_yesterday - ATL_yesterday  (prior-day values)
"""

import math
from datetime import date, timedelta
from typing import Dict, List

# Decay constants
_ATL_TAU = 7   # acute training load time constant (days)
_CTL_TAU = 42  # chronic training load time constant (days)

_ATL_DECAY = math.exp(-1 / _ATL_TAU)
_CTL_DECAY = math.exp(-1 / _CTL_TAU)

_ATL_GAIN = 1 - _ATL_DECAY
_CTL_GAIN = 1 - _CTL_DECAY


def compute_pmc(
    tss_by_date: Dict[str, float],
    start_date: date,
    end_date: date,
    warmup_days: int = 60,
) -> List[dict]:
    """
    Compute PMC metrics for every calendar day in [start_date, end_date].

    Args:
        tss_by_date: dict mapping ISO date strings ("YYYY-MM-DD") to daily TSS.
        start_date:  first date to include in returned results.
        end_date:    last date to include in returned results.
        warmup_days: how many days before start_date to run the model
                     so ATL/CTL are correctly initialised (cold-start warmup).

    Returns:
        List of dicts, one per day:
          {date, tss, atl, ctl, tsb}
        All values rounded to 1 decimal place.
    """
    warmup_start = start_date - timedelta(days=warmup_days)

    atl = 0.0
    ctl = 0.0

    results = []
    current = warmup_start

    while current <= end_date:
        iso = current.isoformat()
        tss = tss_by_date.get(iso, 0.0)

        # TSB uses *prior* day's ATL/CTL (standard TP convention)
        tsb = ctl - atl

        # Update ATL / CTL with today's TSS
        atl = atl * _ATL_DECAY + tss * _ATL_GAIN
        ctl = ctl * _CTL_DECAY + tss * _CTL_GAIN

        # Only emit results from start_date onward
        if current >= start_date:
            results.append(
                {
                    "date": iso,
                    "tss": round(tss, 1),
                    "atl": round(atl, 1),
                    "ctl": round(ctl, 1),
                    "tsb": round(tsb, 1),
                }
            )

        current += timedelta(days=1)

    return results


# ---------------------------------------------------------------------------
# Quick smoke-test: run with  python calculations.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from datetime import date

    # Simulate a 14-day block: 100 TSS/day for 7 days, then rest
    today = date.today()
    tss = {}
    for i in range(7):
        d = today - timedelta(days=13 - i)
        tss[d.isoformat()] = 100.0

    start = today - timedelta(days=13)
    rows = compute_pmc(tss, start, today, warmup_days=0)

    print(f"{'Date':<12} {'TSS':>6} {'ATL':>6} {'CTL':>6} {'TSB':>6}")
    print("-" * 40)
    for r in rows:
        print(
            f"{r['date']:<12} {r['tss']:>6.1f} {r['atl']:>6.1f} {r['ctl']:>6.1f} {r['tsb']:>6.1f}"
        )

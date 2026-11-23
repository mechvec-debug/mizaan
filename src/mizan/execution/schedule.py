"""Tiny cron helper so the dashboard can show 'next scheduled run' (numbers, lists, ranges, '*' and '*/n'; day-of-week 0 or 7 = Sunday)."""
from __future__ import annotations

from datetime import datetime, timedelta


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, st = part.split("/")
            step = int(st)
        a, b = (lo, hi) if part in ("*", "") else ((int(x) for x in part.split("-")) if "-" in part else (int(part),) * 2)
        out |= set(range(a, b + 1, step))
    return out


def next_cron_run(cron: str, after: datetime) -> datetime | None:
    m, h, dom, mon, dow = cron.split()
    mins, hours, doms, mons, dows = _field(m, 0, 59), _field(h, 0, 23), _field(dom, 1, 31), _field(mon, 1, 12), {d % 7 for d in _field(dow, 0, 7)}
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if t.minute in mins and t.hour in hours and t.month in mons and t.day in doms and (t.isoweekday() % 7) in dows:
            return t
        t += timedelta(minutes=1)
    return None

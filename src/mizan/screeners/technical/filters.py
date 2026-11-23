"""Dashboard row filters. `original_visible` is the ORIGINAL dashboard logic, verbatim (hidden FORMING|VOID rows, latest date only);
`views` adds the new Actionable / Watch tabs on top without touching the original behaviour."""
from __future__ import annotations

import pandas as pd


def original_visible(records: list) -> pd.DataFrame:
    df = pd.DataFrame([{k: v for k, v in r.items() if k != "details"} for r in records])
    if df.empty:
        return df
    df = df[~df["action"].str.contains("FORMING|VOID", case=False, na=False)]            # verbatim from dashboard.py
    df["time_received"] = pd.to_datetime(df["time_received"])
    latest = df["time_received"].dt.date.max()
    return df[df["time_received"].dt.date == latest]


def views(records: list) -> dict:
    """actionable = grade A/B, watch = grade C, all = every record (the original table and its filters live on that tab)."""
    return {"actionable": [r for r in records if r["details"]["grade"] in ("A", "B")],
            "watch": [r for r in records if r["details"]["grade"] == "C"],
            "all": list(records)}

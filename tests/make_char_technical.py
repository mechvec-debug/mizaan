"""Technical (S1): the supplied code has no calculator, so baseline = the dashboard's filter logic
(copied verbatim from original/technical/dashboard.py lines 'Background Engine Filters') run on the golden alerts."""
import json, os, collections
import pandas as pd
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
alerts = json.load(open(os.path.join(ROOT, "golden_master/original_outputs/technical_trading_alerts.json")))
df = pd.DataFrame(alerts)
df = df[~df['action'].str.contains("FORMING|VOID", case=False, na=False)]            # verbatim from dashboard.py
df['time_received'] = pd.to_datetime(df['time_received'])
latest_date = df['time_received'].dt.date.max()
df = df[df['time_received'].dt.date == latest_date]
out = {"rows_total": len(alerts), "rows_after_hidden_filters": len(df), "latest_date": str(latest_date),
       "tickers_after_filters": sorted(df['ticker']), "action_counts_after": df['action'].value_counts().to_dict(),
       "aligned_counts_after": df['aligned'].value_counts().to_dict(), "plan_counts_after": df['plan'].value_counts().to_dict(),
       "columns_displayed": ["ticker", "action", "entry", "sl", "tp1", "tp2", "tp3", "aligned", "plan", "time_received"]}
json.dump(out, open(os.path.join(ROOT, "golden_master/characterization/technical_dashboard_filter_baseline.json"), "w"), indent=1, ensure_ascii=False)
print({k: v for k, v in out.items() if k != "tickers_after_filters"})

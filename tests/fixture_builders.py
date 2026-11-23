"""Side-effect-free builders shared by the fixture generators and the replay tests."""
import pandas as pd

NS = 'xmlns:in-capmkt="http://example.com/in" xmlns:xbrli="http://www.xbrl.org/2003/instance"'


def xbrl(tags, end="2026-03-31"):
    body = "".join(f"<in-capmkt:{k}>{val}</in-capmkt:{k}>" for k, val in tags)
    return (f'<?xml version="1.0"?><xbrli:xbrl {NS}><xbrli:context id="c"><xbrli:period><xbrli:endDate>{end}'
            f'</xbrli:endDate></xbrli:period></xbrli:context>{body}</xbrli:xbrl>')


def fin(ni=150e7, ebit=220e7, eq=900e7, assets=1500e7, cl=400e7, eps=(12.0, 10.0)):
    cols = [pd.Timestamp("2026-03-31"), pd.Timestamp("2025-03-31")]
    inc = pd.DataFrame({cols[0]: [ni, ebit, eps[0]], cols[1]: [ni * .8, ebit * .8, eps[1]]},
                       index=["Net Income", "EBIT", "Diluted EPS"])
    bal = pd.DataFrame({cols[0]: [eq, assets, cl], cols[1]: [eq * .9, assets * .9, cl]},
                       index=["Stockholders Equity", "Total Assets", "Total Current Liabilities"])
    return inc, bal

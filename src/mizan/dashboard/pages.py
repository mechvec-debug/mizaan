"""Dashboard pages. Every function is render_x(st, ctx, session, spec=None) and receives the Streamlit module as `st`,
so pages are tested with a recording fake and never import Streamlit themselves.
The three screener pages keep their ORIGINAL designs (Technical: quick filters + table; Fundamental: the two-column sections;
Shariah: dark theme, KPI cards, one expander per stock with six rule cards). Admin-only controls are guarded by permissions."""
from __future__ import annotations

import json

import pandas as pd

from ..core.auth import can
from ..execution.runner import run_modules
from . import views as V


# ---------------------------------------------------------------- helpers
def _df(st, frame, **kw):
    """Works on both old and new Streamlit versions (as the original Fundamental app did)."""
    frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
    try:
        st.dataframe(frame, width="stretch", hide_index=True, **kw)
    except Exception:
        st.dataframe(frame, use_container_width=True, hide_index=True, **kw)


def _table(st, rows, empty="Nothing here right now."):
    if not rows:
        st.caption(empty)
    else:
        _df(st, rows)


def admin_run_button(st, ctx, session, module_id: str, label: str):
    """Run one module from the page. Admin only; the outcome is shown, never raised."""
    if not can(session, "run_modules"):
        st.caption("🔒 Only the admin can run modules. The numbers below are the latest published results.")
        return None
    if st.button(label, type="primary", key=f"run_{module_id}"):
        with st.spinner(f"Running {module_id} ..."):
            o = run_modules(ctx.registry, ctx, [module_id])[0]
        {"ok": st.success, "not_configured": st.warning, "disabled": st.info}.get(o.status, st.error)(
            f"{module_id}: {o.status}" + (f" - {o.error}" if o.error else "") + (f" | warnings: {o.output_problems}" if o.output_problems else ""))
        return o
    return None


def _kpi(st, title, value, color="#f9fafb"):
    st.markdown(f'<div class="metric-card"><div class="metric-title">{title}</div><div class="metric-value" style="color: {color};">{value}</div></div>',
                unsafe_allow_html=True)


# ---------------------------------------------------------------- 1. Executive overview
def render_overview(st, ctx, session, spec=None):
    st.title("📊 Executive Overview")
    v = V.overview_view(ctx.store)
    if v["empty"]:
        st.info("No results yet. Run the screeners (Execution Monitor, admin) or wait for the scheduled run.")
        return
    st.caption("Each screener is shown on its own terms - scores and rankings are never mixed or averaged.")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.subheader("🎯 Swing Setup Radar")
        st.metric("Actionable setups (A/B)", v["technical"]["actionable"])
        st.metric("Watch (C)", v["technical"]["watch"])
    with c2:
        st.subheader("🛡️ Quality Value Rebound")
        st.metric("Execution Ready (PASS)", v["fundamental"]["pass"])
        st.metric("Watchlist", v["fundamental"]["watch"])
    with c3:
        st.subheader("⚖️ Shariah Compliance")
        k = v["shariah"]
        st.metric("Compliant", k["compliant"])
        st.metric("Non-compliant", k["non_compliant"])
        st.metric("Undetermined", k["undetermined"])
    with st.expander("🔗 Cross-screener comparison (optional extra - separate from the original outputs)"):
        s = v["cross"]["summary"]
        st.markdown(f"**{s['technical_actionable']}** actionable technical setups. By Shariah verdict: `{s['technical_actionable_by_shariah']}`")
        if s["technical_actionable_and_shariah_compliant"]:
            st.success("Actionable AND Shariah-compliant: " + ", ".join(s["technical_actionable_and_shariah_compliant"]))
        if s["technical_actionable_shariah_undetermined"]:
            st.warning("Actionable but Shariah UNDETERMINED (evidence still pending): " + ", ".join(s["technical_actionable_shariah_undetermined"]))
        st.caption(s["note"])
        rows = [{"Stock": r["ticker"], "Setup": (r["technical"] or {}).get("action", ""), "Grade": (r["technical"] or {}).get("grade", ""),
                 "Fundamental": (r["fundamental"] or {}).get("status", ""), "Shariah": (r["shariah"] or {}).get("verdict", "not screened"),
                 "Breached": ", ".join(x.split(":")[0] for x in (r["shariah"] or {}).get("breached", [])),
                 "Pending": ", ".join(x.split(":")[0] for x in (r["shariah"] or {}).get("pending", []))} for r in v["cross"]["rows"]]
        _df(st, rows)


# ---------------------------------------------------------------- 2. Screener 1 - Swing Setup Radar (original design kept)
def render_technical(st, ctx, session, spec=None):
    st.title("🎯 High-Probability Setup Dashboard")
    st.caption("Swing Setup Radar - monitoring for EMA crossovers inside Fibonacci impulse zones (retracement entries). Prices are raw market prices.")
    admin_run_button(st, ctx, session, "technical", "🚀 Run Swing Setup Radar")
    v = V.technical_view(ctx.store)
    if v["empty"]:
        st.info("Waiting for market data... Run the screener to update targets. 📡")
        return
    t1, t2, t3 = st.tabs(["⭐ Actionable (A/B)", "👀 Watch (C)", "🗂️ All setups (original view)"])
    with t1:
        st.caption("Best setups first. 'Notify at' = the price to be alerted at (top of the zone while waiting; the current price once inside).")
        _table(st, v["actionable"], "No A/B setups today.")
    with t2:
        _table(st, v["watch"], "No watch-list setups today.")
    with t3:
        df = v["original"]
        if df is None or df.empty:
            st.info("Scan complete. No active setups found in the Buy Zone today. 📡")
            return
        st.markdown("### 🔍 Quick Filters")
        col1, col2, col3 = st.columns(3)
        with col1:
            signal = st.multiselect("Signal", options=list(df["action"].unique()), default=list(df["action"].unique()))
        with col2:
            mtf = st.multiselect("MTF Alignment", options=list(df["aligned"].unique()), default=list(df["aligned"].unique()))
        with col3:
            plan = st.multiselect("Trade Plan", options=list(df["plan"].unique()), default=list(df["plan"].unique()))
        f = df[df["action"].isin(signal) & df["aligned"].isin(mtf) & df["plan"].isin(plan)]
        st.markdown(f"Monitoring for EMA Crossovers inside Fibonacci Impulse Zones. &nbsp;&nbsp;&nbsp; "
                    f"<span style='color:#ff4b4b; font-weight:bold;'>Total scan ({v['display_date']}) is {len(f)}</span>", unsafe_allow_html=True)
        cc = st.column_config
        st.dataframe(f, column_config={"ticker": cc.TextColumn("Stock", width="medium"), "action": cc.TextColumn("Signal", width="small"),
                                       "entry": cc.NumberColumn("Entry (₹)", format="%.2f"), "sl": cc.NumberColumn("Stop Loss (₹)", format="%.2f"),
                                       "tp1": cc.NumberColumn("TP1 (₹)", format="%.2f"), "tp2": cc.NumberColumn("TP2 (₹)", format="%.2f"),
                                       "tp3": cc.NumberColumn("TP3 (₹)", format="%.2f"), "aligned": cc.TextColumn("MTF Alignment", width="medium"),
                                       "plan": cc.TextColumn("Trade Plan", width="medium"),
                                       "time_received": cc.DatetimeColumn("Trigger Time", format="D MMM YYYY, h:mm a")},
                     hide_index=True, use_container_width=True)


# ---------------------------------------------------------------- 3. Screener 2 - Quality Value Rebound (original sections kept)
def render_fundamental(st, ctx, session, spec=None):
    st.title("🛡️ Quality Value Rebound")
    admin_run_button(st, ctx, session, "fundamental", "🚀 Run Quality Value Rebound")
    v = V.fundamental_view(ctx.store)
    if v["empty"]:
        st.warning("There is no scan data yet. Run the scanner first.")
        return
    if v["last_scan"]:
        st.caption(f"Last scan: {v['last_scan']}")
    left, right = st.columns(2)
    for col, sections in ((left, v["left"]), (right, v["right"])):
        with col:
            for heading, caption, rows in sections:
                st.subheader(heading)
                if caption:
                    st.caption(caption)
                _table(st, rows)
    st.divider()
    st.info("💡 All columns are in the published data.json for cross-verification. This is a screening tool, not investment advice.")


# ---------------------------------------------------------------- 4. Screener 3 - Shariah (original dark design kept, UNDETERMINED added)
SHARIAH_CSS = """<style>
.main { background-color: #0d1117; }
.metric-card { background: linear-gradient(135deg, #1f2937, #111827); border-radius: 10px; padding: 16px 20px; border: 1px solid #374151;
  box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); text-align: center; }
.metric-title { font-size: 0.85rem; color: #9ca3af; text-transform: uppercase; font-weight: 600; }
.metric-value { font-size: 1.8rem; font-weight: 700; color: #f9fafb; margin-top: 4px; }
.badge-compliant { background-color: #065f46; color: #34d399; font-weight: 700; padding: 4px 12px; border-radius: 9999px; border: 1px solid #059669; display: inline-block; font-size: 0.85rem; }
.badge-non-compliant { background-color: #7f1d1d; color: #f87171; font-weight: 700; padding: 4px 12px; border-radius: 9999px; border: 1px solid #dc2626; display: inline-block; font-size: 0.85rem; }
.badge-undetermined { background-color: #78350f; color: #fbbf24; font-weight: 700; padding: 4px 12px; border-radius: 9999px; border: 1px solid #d97706; display: inline-block; font-size: 0.85rem; }
.rule-card { background-color: #1e293b; border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; border-left: 4px solid #3b82f6; }
</style>"""
_COLORS = {"pass": ("#10b981", "#34d399"), "fail": ("#ef4444", "#f87171"), "pending": ("#f59e0b", "#fbbf24")}


def render_shariah(st, ctx, session, spec=None):
    st.markdown(SHARIAH_CSS, unsafe_allow_html=True)
    st.title("⚖️ Shariah Screen of NSE/BSE Stocks")
    st.markdown("Automated 6-Criteria Screening Engine under **Ind-AS & AAOIFI / TASIS Standards**. "
                "A stock is *compliant* only when all six criteria are proven; missing data stays **undetermined** - nothing is assumed.")
    admin_run_button(st, ctx, session, "shariah", "🚀 Run Full Shariah Screen")
    v = V.shariah_view(ctx.store)
    if v["empty"]:
        st.info("No Shariah results yet. Run the screen (admin) or wait for the scheduled run.")
        return
    k = v["kpis"]
    c1, c2, c3, c4, c5 = st.columns(5)
    for col, (title, val, color) in zip((c1, c2, c3, c4, c5), (("Total Screened", k["total"], "#f9fafb"), ("Compliant", k["compliant"], "#34d399"),
                                                              ("Non-Compliant", k["non_compliant"], "#f87171"), ("Undetermined", k["undetermined"], "#fbbf24"),
                                                              ("Compliance Rate", f"{k['rate']}%", "#60a5fa"))):
        with col:
            _kpi(st, title, val, color)
    st.write("")
    if v["last_updated"]:
        st.caption(f"🕒 **Report Generated:** {v['last_updated']} (Persisted)")
    st.subheader("📋 Screening Summary Table")
    for it in v["items"]:
        with st.expander(f"**{it['ticker']}** | {it['sector']} | ₹{it['market_cap_cr']:,.0f} Cr | 📅 Period: **{it['period']}** | {it['tag']}"):
            st.markdown(f"#### Shariah Audit for **{it['ticker']}** (Results Period: `{it['period']}`)")
            cols = list(st.columns(3)) + list(st.columns(3))
            for card, col in zip(it["cards"], cols):
                border, text = _COLORS[card["state"]]
                note = f'<br/><span style="font-size: 0.78rem; color: #fbbf24;">{card["note"]}</span>' if card["note"] else ""
                with col:
                    st.markdown(f"""<div class="rule-card" style="border-left-color: {border};">
<strong style="color: #e2e8f0; font-size: 0.9rem;">{card['label']}</strong><br/>
<span style="font-size: 0.85rem; color: #cbd5e1;">Value: <b>{card['value']}</b></span><br/>
<span style="font-size: 0.8rem; color: #94a3b8;">Benchmark: {card['limit']}</span><br/>
<span style="font-size: 0.85rem; font-weight: bold; color: {text};">{card['badge']}</span>{note}</div>""", unsafe_allow_html=True)
            st.caption(f"Data source: {it['source'] or 'n/a'} | BSE code: {it['bse_code']}")
    if can(session, "edit_evidence"):
        _evidence_editor(st, ctx, [i["ticker"] for i in v["items"]])


def _evidence_editor(st, ctx, tickers):
    from ..screeners.shariah.evidence import FIELDS
    with st.expander("🖊️ Admin: record evidence for a pending criterion"):
        st.caption("Every figure needs its source, fiscal year and a written justification. Amounts are in ₹ crore; 0 only if the filing states nil.")
        sym = st.selectbox("Stock", tickers, key="ev_sym")
        field = st.selectbox("Evidence field", list(FIELDS), key="ev_field")
        value = st.number_input("Amount (₹ crore)", min_value=0.0, value=0.0, key="ev_val")
        fy = st.text_input("Fiscal year end (YYYY-MM-DD)", value="", key="ev_fy")
        why = st.text_area("Where in the filing does this come from?", value="", key="ev_why")
        if st.button("💾 Save evidence", key="ev_save"):
            try:
                ctx.evidence().set(sym, field, value, source="manual", fy_end=fy.strip(), evidence=why)
                st.success(f"Saved {field} for {sym}. It is used from the next Shariah run.")
            except ValueError as exc:
                st.error(str(exc))


# ---------------------------------------------------------------- 5. Annual report intelligence
def render_annual_report(st, ctx, session, spec=None):
    from ..annual_report.config import AnnualReportConfig
    st.title("📑 Annual Report Intelligence")
    cfg = AnnualReportConfig.from_config(ctx.cfg)
    if cfg.daily_token_budget in (None, 0) or cfg.requests_per_minute in (None, 0):
        st.warning("Not configured yet: set `annual_report.daily_token_budget` and `requests_per_minute` in config/app.yaml from your AI provider's "
                   "current limits. Until then the batch will not run (it never guesses a limit).")
    admin_run_button(st, ctx, session, "annual_report", "🚀 Run today's annual-report batch")
    d = V.annual_view(ctx.store)["d"]
    if not d:
        st.info("No annual-report batch has run yet.")
        return
    a, b, c, e, f = st.columns(5)
    a.metric("Eligible stocks", d["total_eligible_stocks"]); b.metric("Reports available", d["reports_available"])
    c.metric("Processed", d["reports_processed"]); e.metric("Pending", d["reports_pending"]); f.metric("Failed", d["reports_failed"])
    db, tk = d["daily_batch"], d["tokens"]
    st.subheader("Today's batch")
    st.progress(min(1.0, db["stocks_processed"] / max(1, db["stocks_limit"])), text=f"{db['stocks_processed']} of {db['stocks_limit']} stocks")
    if tk["daily_budget"]:
        st.progress(min(1.0, tk["today_total"] / tk["daily_budget"]), text=f"Tokens: {tk['today_total']:,} of {tk['daily_budget']:,}")
    st.write(f"Stopped because: **{db['stopped_reason'] or 'not stopped yet'}** | last successful processing: **{d['last_successful_processing_date'] or 'never'}** | "
             f"next scheduled batch: **{d['next_scheduled_batch'] or 'nothing pending'}**")
    for al in db["alerts"]:
        st.warning(al)
    st.caption(f"No report found: {d['no_report_found']} | not yet checked: {d['not_yet_checked']} | inconclusive: {d['inconclusive']} | "
               f"estimated days to finish (upper bound): {d['estimated_days_to_finish_upper_bound']}")
    if d["failed_detail"]:
        st.subheader("Failed reports")
        _df(st, d["failed_detail"])


# ---------------------------------------------------------------- 6. Data management
def render_data_management(st, ctx, session, spec=None):
    st.title("🗄️ Data Management")
    v = V.data_management_view(ctx.store, ctx.universe_service)
    a, b, c, d = st.columns(4)
    a.metric("Store size (MB)", v["size"]["mb"]); b.metric("Files", v["size"]["files"])
    c.metric("Stocks with price history", v["price_history"]["stocks"]); d.metric("Stale (>7 days)", v["price_history"]["stale_over_7_days"])
    u = v.get("universe", {})
    st.subheader("Stock universe (single source of truth)")
    if "error" in u:
        st.error(u["error"])
    else:
        st.write(f"**{u.get('active', 0)}** active stocks | version `{u.get('version', '')}` | file `{u.get('source', '')}`")
        for bad in u.get("invalid", []):
            st.warning(f"Line {bad['line']}: {bad['text']!r} - {bad['reason']}")
    st.subheader("Downloads today (the ledger)")
    _table(st, v["downloads_today"], "No downloads today yet.")
    if v["duplicates_today"]:
        st.error("Possible duplicate downloads today:")
        _df(st, v["duplicates_today"])
    else:
        st.success("No duplicate downloads today.")
    if v["failures_recent"]:
        st.subheader("Recent failures")
        _df(st, v["failures_recent"])
    st.caption(f"Stocks with Shariah evidence on file: {v['evidence_stocks']} | ledger rows: {v['ledger_rows']}")
    with st.expander("Recent downloads"):
        _table(st, v["recent_downloads"])


# ---------------------------------------------------------------- 7. Execution monitor
def _show_report(st, rep):
    for m, v in rep.modules.items():
        {"ok": st.success, "not_configured": st.warning, "skipped": st.info}.get(v["status"], st.error)(
            f"{m}: {v['status']}" + (f" - {v['error'].splitlines()[0]}" if v.get("error") else ""))
    for a in rep.alerts:
        {"error": st.error, "warning": st.warning}.get(a["level"], st.info)(f"{a['module']}: {a['message']}")


def render_execution_monitor(st, ctx, session, spec=None):
    from ..execution.manager import ExecutionManager
    st.title("⏱️ Execution Monitor")
    v = V.execution_view(ctx.store, ctx.registry, ctx.cfg)
    s = v["status"]
    if s:
        (st.error if s.get("exit_code") else st.success)(
            f"Last run {s['run_id']} ({s['trigger']}) finished {s['finished_at']} - exit code {s['exit_code']}" + (" - time budget reached, it continues next run" if s.get("deadline_hit") else ""))
        for a in s.get("alerts", []):
            {"error": st.error, "warning": st.warning}.get(a["level"], st.info)(f"{a['module']}: {a['message']}")
    else:
        st.info("No run has been recorded in this data store yet.")
    if v["next_run"]:
        st.caption(f"Next scheduled run: {v['next_run']} (GitHub schedule)")
    pub = v["published"]
    if pub["generated_at"]:
        st.caption(f"Published data: {pub['generated_at']} - {pub['files']} files - " + ("PROBLEMS: " + "; ".join(pub["problems"]) if pub["problems"] else "consistent")
                   + (f" - no results yet for: {', '.join(pub['missing'])}" if pub["missing"] else ""))
    if can(session, "run_modules"):
        mgr = ExecutionManager(ctx.registry, ctx)
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            run_all = st.button("▶️ Run all enabled modules (one after another)", type="primary", key="run_all")
        with c2:
            retry = st.button("🔁 Retry failed modules", key="retry_failed")
        with c3:
            resume = st.button("⏩ Resume interrupted work", key="resume")
        with c4:
            pub_now = st.button("📤 Publish now", key="publish_now")
        if run_all or retry or resume:
            with st.spinner("Running ..."):
                rep = mgr.run(trigger="dashboard", retry_failed=retry, resume=resume)
            _show_report(st, rep)
        if pub_now:
            from ..publish import PublishError, publish_all
            try:
                st.success(f"Published {len(publish_all(ctx.store, ctx.universe())['files'])} files.")
            except PublishError as exc:
                st.error(str(exc))
    else:
        st.caption("🔒 Only the admin can start runs.")
    st.subheader("Latest run of each module")
    _table(st, [{"Module": m["id"], "Enabled": m["enabled"], "Last status": (v["last"].get(m["id"]) or {}).get("status", "never run"),
                 "When": (v["last"].get(m["id"]) or {}).get("started_at", ""), "Seconds": round((v["last"].get(m["id"]) or {}).get("seconds", 0), 1)}
                for m in v["modules"]])
    for mid, r in v["reports"].items():
        with st.expander(f"Run report - {mid}"):
            st.json({k: x for k, x in r.items() if k != "failed_detail"})
            if r.get("failed_detail"):
                st.warning(f"Failed stocks: {json.dumps(r['failed_detail'])[:1500]}")
    for al in v["annual_alerts"]:
        st.warning(al)
    st.subheader("History")
    _table(st, [{k: x.get(k) for k in ("started_at", "module_id", "status", "seconds", "error")} for x in v["history"]], "No runs recorded yet.")


# ---------------------------------------------------------------- 8/9. Custom screeners and analysis (+ generic module page)
def _custom_list(st, ctx, session, category, title, hint):
    st.title(title)
    mods = [s for s in ctx.registry.all() if s.category == category]
    if not mods:
        st.info(hint)
    for s in mods:
        on = ctx.registry.is_enabled(s.id)
        st.markdown(f"**{s.name}** (`{s.id}`) - {'✅ enabled' if on else '⏸️ disabled'}")
        st.caption(f"{s.description} | needs: {', '.join(s.data_dependencies) or 'nothing extra'}")
    if ctx.registry.invalid:
        for mid, problems in ctx.registry.invalid.items():
            st.error(f"Module '{mid}' was not registered: " + "; ".join(problems))
    with st.expander("How to add one"):
        st.markdown("Write a function `run(ctx)` in `src/mizan/screeners/custom/`, add one entry to `config/modules.yaml` "
                    "(`id`, `display_name`, `category`, `entrypoint`, `results_file`, `required_columns`, `enabled`). "
                    "It appears here and in the navigation automatically. No existing engine is edited.")


def render_custom_screeners(st, ctx, session, spec=None):
    _custom_list(st, ctx, session, "custom_screener", "🧩 Custom Screeners", "No custom screeners registered yet.")


def render_custom_analysis(st, ctx, session, spec=None):
    _custom_list(st, ctx, session, "custom_analysis", "🔬 Custom Analysis", "No custom analyses registered yet.")


def render_generic_module(st, ctx, session, spec):
    st.title(f"🧩 {spec.name}")
    st.caption(spec.description)
    admin_run_button(st, ctx, session, spec.id, f"🚀 Run {spec.name}")
    v = V.generic_module_view(ctx.registry, ctx, spec.id)
    if v["missing"]:
        st.info("No results yet for this module.")
    else:
        _table(st, v["rows"])


# ---------------------------------------------------------------- 10. System configuration
def _show_change(st, rep):
    if rep.added:
        st.success(f"Added {len(rep.added)}: {', '.join(rep.added[:30])}")
    for label, items in (("Removed", rep.removed), ("Disabled", rep.disabled), ("Enabled", rep.enabled)):
        if items:
            st.success(f"{label}: {', '.join(items)}")
    if rep.already_present:
        st.info(f"Already in the list: {', '.join(rep.already_present[:30])}")
    if rep.exists_but_disabled:
        st.warning(f"Present but disabled (use Enable): {', '.join(rep.exists_but_disabled)}")
    if rep.not_found:
        st.warning(f"Not found: {', '.join(rep.not_found)}")
    for raw, why in rep.invalid:
        st.error(f"Rejected {raw!r}: {why}")


def render_system_configuration(st, ctx, session, spec=None):
    st.title("⚙️ System Configuration")
    v = V.config_view(ctx.cfg, ctx.registry, ctx.universe_service)
    st.subheader("Modules")
    for m in v["modules"]:
        label = f"{m['name']} ({m['id']}, {m['category']})"
        if can(session, "toggle_modules"):
            now = st.checkbox(label, value=m["enabled"], key=f"mod_{m['id']}")
            if now != m["enabled"]:
                ctx.registry.set_enabled(m["id"], now)
                st.info(f"{m['name']} is now {'enabled' if now else 'disabled'}.")
        else:
            st.write(f"{'✅' if m['enabled'] else '⏸️'} {label}")
    st.subheader("Stock universe")
    st.write(f"File in use: `{v['universe_file']}`")
    if can(session, "edit_universe"):
        st.caption("One NSE symbol per line (or paste/upload a CSV with a 'symbol', 'ticker' or 'code' column). New IPOs can be added here at any time.")
        text = st.text_area("Add symbols", value="", key="uni_add")
        up = st.file_uploader("...or upload a .txt / .csv file", type=["txt", "csv"], key="uni_file")
        if st.button("➕ Add to universe", key="uni_btn"):
            try:
                body = (up.getvalue().decode("utf-8-sig") if up is not None else "") + "\n" + text
                _show_change(st, ctx.universe_service.add_from_text(body))
            except ValueError as exc:
                st.error(str(exc))
        sym = st.text_input("Symbols to remove / disable / enable (space separated)", value="", key="uni_sym").split()
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("🗑️ Remove", key="uni_rm") and sym:
                _show_change(st, ctx.universe_service.remove(sym))
        with c2:
            if st.button("⏸️ Disable", key="uni_dis") and sym:
                _show_change(st, ctx.universe_service.disable(sym))
        with c3:
            if st.button("▶️ Enable", key="uni_en") and sym:
                _show_change(st, ctx.universe_service.enable(sym))
        st.caption("Edits change the universe file on THIS machine. For the GitHub-hosted setup, commit the file (or use the repo editor) so the daily run sees it.")
    else:
        st.caption("🔒 Only the admin can edit the stock universe.")
    for mid, problems in v["invalid_modules"].items():
        st.error(f"Module '{mid}' is not registered: " + "; ".join(problems))
    with st.expander("Current configuration (read-only)"):
        st.json(v["cfg"])

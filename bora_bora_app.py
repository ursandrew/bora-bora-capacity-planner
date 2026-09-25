"""
bora_bora_app.py
=================
Streamlit front-end for the Bora Bora vintage-tracked, multi-tranche
capacity-expansion tool. Builds on the reference codebase's UI patterns
(streamlit_app_with_degradation.py) but is purpose-built for the Bora Bora
problem: PV + OTEC + BESS tranches added at realistic procurement-cycle
years (default 2028/2030/2035/2040/2050), sized to a checkpoint-bounded
2030->2050 RE glide path, subject to a curtailment cap.

Run with:  streamlit run bora_bora_app.py
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from io import BytesIO

import bora_bora_data as data
import bora_bora_engine as eng

st.set_page_config(page_title="Bora Bora Net-Zero Capacity Planner", layout="wide", page_icon="🌴")

st.title("🌴 Bora Bora Net-Zero Capacity Expansion Planner")
st.caption(
    "Vintage-tracked, multi-tranche PV + OTEC + BESS sizing across the 2026-2050 "
    "horizon - each tranche degrades from its OWN commissioning year, unlike the "
    "Excel workbook's independent milestone-year snapshots."
)

# ==============================================================================
# SIDEBAR - CONFIGURATION
# ==============================================================================
with st.sidebar:
    st.header("⚙️ Configuration")

    st.subheader("📁 Data source")
    st.caption(
        "Uploads override the bundled Bora Bora defaults for this session only - "
        "nothing is written back to the repo, so you can swap in an updated "
        "carbon-team profile any time without redeploying the app."
    )
    pv_wind_upload = st.file_uploader(
        "PV + Wind hourly generation factors (CSV)", type=["csv"],
        help="Columns: hour, month, day, hour_of_day, pv_cf, wind_cf. 8,760 rows, one per hour. "
             "Leave empty to use the bundled Bora Bora PVSYST/wind-timeseries data."
    )
    demand_upload = st.file_uploader(
        "Baseline hourly demand shape (CSV)", type=["csv"],
        help="Columns: hour, month, day, hour_of_day, demand_mw_2024_shape. 8,760 rows. "
             "Leave empty to use the bundled 2024 EDT-derived baseline shape."
    )

    st.subheader("OTEC (exogenous - already decided)")
    otec_mw = st.number_input("OTEC capacity (MW, net average delivered)", value=data.OTEC_CAPACITY_MW, step=0.1)
    otec_year = st.number_input("OTEC commissioning year", value=data.OTEC_COMMISSIONING_YEAR, step=1)
    st.caption("Per manager's decision: lower bound of the 2H Offshore 1.2-2.6 MW range. "
               "CF fixed at 1.0 since this figure is already net average output, not nameplate.")

    st.subheader("Tranche years (realistic procurement cycles)")
    tranche_years_str = st.text_input("Comma-separated years", "2028, 2030, 2035, 2040, 2050")
    tranche_years = tuple(int(y.strip()) for y in tranche_years_str.split(",") if y.strip())

    st.subheader("Glide path")
    target_buffer_pct = st.slider(
        "Target buffer at commissioning (percentage points)", 0.0, 15.0, 3.0, 0.5,
        help="Extra RE% margin built in at each tranche's commissioning year, sized to absorb "
             "the degradation dip expected before the NEXT tranche comes online. This is what "
             "keeps the checkpoint-only glide path from falling too far below target between tranches."
    )

    st.subheader("Constraints")
    curtailment_cap_pct = st.slider(
        "Max curtailment (% of annual PV+Wind+OTEC generation)", 1.0, 30.0, 10.0, 1.0,
        help="Hard ceiling on wasted renewable energy. This is the constraint that directly "
             "answers the original curtailment concern - Excel's pure-cost sizing had no such cap."
    )

    st.subheader("Search grid resolution")
    pv_max = st.number_input("Max PV addition per tranche to test (MWp)", value=100, step=10)
    pv_step = st.number_input("PV step (MWp)", value=5, step=1)
    bess_max = st.number_input("Max BESS addition per tranche to test (MWh)", value=400, step=20)
    bess_step = st.number_input("BESS step (MWh)", value=20, step=10)
    bess_c_rate = st.slider("BESS power:energy ratio (C-rate)", 0.1, 1.0, 0.5, 0.05)

    st.subheader("💰 Cost assumptions (PLACEHOLDERS except OTEC)")
    st.caption("The Excel workbook has NO cost sheet - these need validated client figures "
               "before being used for an investment-grade comparison.")
    pv_capex = st.number_input("PV CAPEX (USD/MWp)", value=eng.DEFAULT_COSTS["pv_capex_per_mwp"], step=50_000)
    bess_capex = st.number_input("BESS CAPEX (USD/MWh)", value=350_000, step=25_000)
    otec_capex_per_mw = st.number_input("OTEC CAPEX (USD/MW)", value=95_000_000, step=5_000_000,
                                          help="Derived from 2H Offshore study EUR102-152M / 1.2-2.6MW range - illustrative FX only.")

    costs = dict(eng.DEFAULT_COSTS)
    costs["pv_capex_per_mwp"] = pv_capex
    costs["bess_capex_per_mwh"] = bess_capex
    costs["otec_capex_per_mw"] = otec_capex_per_mw

    run_button = st.button("🚀 Run Tranche Sizing", type="primary", use_container_width=True)

# ==============================================================================
# RUN SIZING
# ==============================================================================
if run_button:
    with st.spinner("Loading hourly generation/demand data..."):
        try:
            pv_cf, wind_cf = data.load_hourly_generation_factors(pv_wind_upload)
            baseline_demand = data.load_baseline_demand_shape_mw(demand_upload)
        except (ValueError, KeyError) as e:
            st.error(f"❌ Problem reading an uploaded file: {e}")
            st.stop()

    if pv_wind_upload is not None:
        st.info("Using uploaded PV/Wind generation profile (not the bundled default).")
    if demand_upload is not None:
        st.info("Using uploaded baseline demand shape (not the bundled default).")

    otec_tranche = eng.Tranche("otec", int(otec_year), data.OTEC_DEGRADATION_RATE, capacity_mw=otec_mw)

    pv_candidates = list(range(0, int(pv_max) + 1, int(pv_step)))
    bess_candidates = list(range(0, int(bess_max) + 1, int(bess_step)))
    n_combos = len(pv_candidates) * len(bess_candidates) * len(tranche_years)

    progress_text = st.empty()
    progress_text.info(f"Searching {len(pv_candidates)} x {len(bess_candidates)} = "
                        f"{len(pv_candidates)*len(bess_candidates)} combinations per tranche year "
                        f"x {len(tranche_years)} tranche years = {n_combos} dispatch simulations...")

    schedule, log = eng.size_tranche_schedule(
        tranche_years=tranche_years,
        otec_tranche=otec_tranche,
        pv_candidates_mwp=pv_candidates,
        bess_candidates_mwh=bess_candidates,
        bess_c_rate=bess_c_rate,
        curtailment_cap_pct=curtailment_cap_pct,
        target_buffer_pct=target_buffer_pct,
        pv_cf_hourly=pv_cf, wind_cf_hourly=wind_cf,
        baseline_demand_hourly_mw=baseline_demand,
        costs=costs,
        verbose=False,
    )
    progress_text.empty()

    years = list(range(2026, 2051))
    traj = eng.simulate_trajectory(schedule, years, pv_cf, wind_cf,
                                    edt_penetration_cap=data.EDT_PENETRATION_CAP,
                                    baseline_demand_hourly_mw=baseline_demand)
    traj_df = pd.DataFrame(traj)

    st.session_state["bb_schedule"] = schedule
    st.session_state["bb_log"] = log
    st.session_state["bb_traj_df"] = traj_df
    st.session_state["bb_done"] = True

# ==============================================================================
# RESULTS
# ==============================================================================
if st.session_state.get("bb_done"):
    schedule = st.session_state["bb_schedule"]
    log = st.session_state["bb_log"]
    traj_df = st.session_state["bb_traj_df"]

    st.markdown("---")

    infeasible = [l for l in log if l.get("status") == "INFEASIBLE within candidate grid - widen pv/bess candidate ranges"]
    if infeasible:
        st.warning(f"⚠️ Could not find a feasible combination for tranche year(s) "
                   f"{[l['year'] for l in infeasible]} within the current search grid. "
                   f"Widen the PV/BESS max or step in the sidebar and re-run.")

    st.subheader("📋 Tranche Sizing Decisions")
    log_rows = []
    for l in log:
        if l.get("status") == "OK":
            log_rows.append({
                "Tranche Year": l["year"], "PV Added (MWp)": l["pv_add"],
                "BESS Added (MWh)": l["bess_add_mwh"], "RE% at Commissioning": f"{l['re_pct']:.1f}%",
                "Target (+buffer)": f"{l['required_pct']:.1f}%", "Curtailment %": f"{l['curtailment_pct']:.1f}%",
                "Incremental NPC": f"${l['npc']/1e6:.2f}M",
            })
    if log_rows:
        st.dataframe(pd.DataFrame(log_rows), use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("📈 RE% Trajectory 2026-2050")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=traj_df["year"], y=traj_df["re_pct"], name="RE% (this schedule)",
                              mode="lines+markers", line=dict(color="#2E7D32", width=3)))
    target_line = [eng.re_target_for_year(y) * 100 for y in traj_df["year"]]
    fig.add_trace(go.Scatter(x=traj_df["year"], y=target_line, name="Glide-path reference (75%@2030 -> 100%@2050)",
                              mode="lines", line=dict(color="orange", dash="dash")))
    fig.add_hline(y=75, line_dash="dot", line_color="red", annotation_text="2030 legal checkpoint (75%)")
    fig.add_hline(y=100, line_dash="dot", line_color="red", annotation_text="2050 legal checkpoint (100%)")
    for ty in schedule.pv:
        fig.add_vline(x=ty.commissioning_year, line_color="gray", line_dash="dot", opacity=0.3)
    fig.update_layout(xaxis_title="Year", yaxis_title="RE Penetration (%)", height=450,
                       hovermode="x unified", plot_bgcolor="white", paper_bgcolor="white")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Dips between tranche years are EXPECTED - capacity degrades between procurement "
               "cycles by design (checkpoint-only glide path). The buffer % controls how deep the dip goes.")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🏗️ Capacity Build-Out")
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=traj_df["year"], y=traj_df["pv_mwp"], name="PV (MWp, degraded)",
                                   mode="lines", line=dict(color="#FDB462", width=2), fill="tozeroy"))
        fig2.add_trace(go.Scatter(x=traj_df["year"], y=traj_df["otec_mw"], name="OTEC (MW)",
                                   mode="lines", line=dict(color="#80B1D3", width=2)))
        fig2.update_layout(xaxis_title="Year", yaxis_title="Capacity (MW)", height=380,
                            plot_bgcolor="white", paper_bgcolor="white")
        st.plotly_chart(fig2, use_container_width=True)
    with col2:
        st.subheader("🗑️ Curtailment Over Time")
        fig3 = go.Figure()
        fig3.add_trace(go.Bar(x=traj_df["year"], y=traj_df["total_curtailment_mwh"],
                               marker_color="#E63946", name="Curtailment (MWh)"))
        fig3.update_layout(xaxis_title="Year", yaxis_title="Curtailment (MWh)", height=380,
                            plot_bgcolor="white", paper_bgcolor="white")
        st.plotly_chart(fig3, use_container_width=True)

    st.markdown("---")
    st.subheader("📊 Full Trajectory Table")
    display_cols = ["year", "pv_mwp", "otec_mw", "bess_energy_mwh", "re_pct", "unmet_pct",
                     "total_curtailment_mwh", "curtailment_pct_of_re_gen", "total_demand_mwh"]
    st.dataframe(traj_df[display_cols].round(2), use_container_width=True, hide_index=True)

    # Export
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        traj_df.to_excel(writer, sheet_name="Trajectory", index=False)
        pd.DataFrame(log_rows).to_excel(writer, sheet_name="Tranche_Decisions", index=False)
    st.download_button("📥 Download Results (Excel)", data=buf.getvalue(),
                        file_name="bora_bora_capacity_plan.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

else:
    st.info("👈 Configure the tranche years, glide-path buffer and curtailment cap in the sidebar, "
            "then click **Run Tranche Sizing**.")

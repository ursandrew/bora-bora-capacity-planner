"""
bora_bora_app.py
=================
Streamlit front-end for the Bora Bora vintage-tracked capacity planner.
Run with:  streamlit run bora_bora_app.py

Every assumption below is a UI input with a default value. To change a
number, use the sidebar - nothing needs to be edited in the code.
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from io import BytesIO

import bora_bora_data as data
import bora_bora_engine as eng

st.set_page_config(page_title="Bora Bora Net Zero Optimization", layout="wide")

st.markdown("""
<div style="display:flex;align-items:center;justify-content:center;gap:14px;margin-bottom:4px">
    <svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
        <rect width="52" height="52" rx="0" fill="#0047AB"/>
        <text x="18" y="38" font-family="Arial,sans-serif" font-size="32"
              font-weight="bold" fill="white" text-anchor="middle">S</text>
        <text x="36" y="37" font-family="Arial,sans-serif" font-size="18"
              font-weight="bold" fill="white" text-anchor="middle">J</text>
        <circle cx="38" cy="18" r="4" fill="#E63946"/>
    </svg>
    <p style="font-size:2.5rem;font-weight:bold;color:#1f77b4;margin:0">
        Bora Bora Net Zero Optimization
    </p>
</div>
""", unsafe_allow_html=True)

# ==============================================================================
# SIDEBAR
# ==============================================================================
with st.sidebar:

    # --- PV -------------------------------------------------------------
    with st.expander("☀️ Solar PV", expanded=True):
        pv_cf_upload = st.file_uploader("Hourly generation-factor profile (CSV) - required", type=["csv"], key="pv_upload")
        st.download_button("Download blank template", data.pv_cf_template_csv(), "pv_cf_template.csv", key="pv_tmpl")
        dc_ac_ratio = st.number_input("DC:AC ratio", value=1.3, step=0.05)
        pv_degradation_pct = st.number_input("Degradation rate (%/yr)", value=0.3, step=0.1)

    # --- Wind -------------------------------------------------------------
    with st.expander("💨 Wind", expanded=False):
        wind_enabled = st.checkbox("Include wind", value=False)
        wind_mw = st.number_input("Capacity (MW)", value=4.125, step=0.1, disabled=not wind_enabled)
        wind_commissioning_year = st.number_input("Commissioning year", value=2028, step=1, disabled=not wind_enabled)
        wind_degradation_pct = st.number_input("Degradation rate (%/yr)", value=0.5, step=0.1, disabled=not wind_enabled)
        wind_cf_upload = st.file_uploader("Hourly generation-factor profile (CSV) - required if wind is included", type=["csv"],
                                           key="wind_upload", disabled=not wind_enabled)
        st.download_button("Download blank template", data.wind_cf_template_csv(), "wind_cf_template.csv", key="wind_tmpl")

    # --- OTEC -------------------------------------------------------------
    with st.expander("🌊 OTEC", expanded=True):
        otec_enabled = st.checkbox("Include OTEC", value=True)
        otec_mw = st.number_input("Capacity (MW, net average delivered)", value=1.2, step=0.1, disabled=not otec_enabled)
        otec_cf = st.number_input("Capacity factor", value=1.0, min_value=0.0, max_value=1.0, step=0.01, disabled=not otec_enabled)
        otec_commissioning_year = st.number_input("Commissioning year", value=2032, step=1, disabled=not otec_enabled)
        otec_degradation_pct = st.number_input("Degradation rate (%/yr)", value=0.0, step=0.1, disabled=not otec_enabled)

    # --- BESS -------------------------------------------------------------
    with st.expander("🔋 BESS", expanded=False):
        bess_c_rate = st.number_input("Power:energy ratio (C-rate)", value=0.5, min_value=0.05, max_value=2.0, step=0.05)
        bess_charge_eff = st.number_input("Charge efficiency", value=0.95, min_value=0.5, max_value=1.0, step=0.01)
        bess_discharge_eff = st.number_input("Discharge efficiency", value=0.95, min_value=0.5, max_value=1.0, step=0.01)
        bess_degradation_pct = st.number_input("Degradation rate (%/yr)", value=1.5, step=0.1)

    # --- Demand: baseline ---------------------------------------------
    with st.expander("🏝️ Demand — existing development (baseline)", expanded=False):
        st.caption("Shape = one full 8,760-hour reference year (the pattern). "
                   "Annual table = each year's total MWh (the multiplier applied to the shape).")
        baseline_shape_upload = st.file_uploader("Hourly demand shape, one calendar year (CSV) - required", type=["csv"], key="baseline_upload")
        st.download_button("Download blank template", data.baseline_demand_template_csv(), "baseline_demand_template.csv", key="baseline_tmpl")
        annual_table_upload = st.file_uploader("Annual demand by year (CSV) - optional, falls back to the bundled Forecast_Annual figures below", type=["csv"], key="annual_upload")
        st.download_button("Download current table (bundled default)", data.annual_table_default_csv(), "annual_demand.csv", key="annual_tmpl")

    # --- Demand: EV chargers (land, excl. bus) ---------------------------------------------
    with st.expander("🔌 Demand — EV chargers (land, excl. bus)", expanded=False):
        st.caption("Hour (0-23) as rows, one column per year - same layout as Combined_Hourly_Load. "
                   "One day's pattern is repeated for every day of that year.")
        ev_land_upload = st.file_uploader("Day profile by year (CSV)", type=["csv"], key="ev_land_upload")
        st.download_button("Download template", data.day_profile_by_year_template_csv(), "ev_land_template.csv", key="ev_land_tmpl")

    # --- Demand: Marine transport ---------------------------------------------
    with st.expander("⛴️ Demand — Marine transport (shore power)", expanded=False):
        st.caption("Same layout as EV chargers above: hour (0-23) as rows, one column per year.")
        marine_upload = st.file_uploader("Day profile by year (CSV)", type=["csv"], key="marine_upload")
        st.download_button("Download template", data.day_profile_by_year_template_csv(), "marine_template.csv", key="marine_tmpl")

    # --- Demand: Grande Vaitape ---------------------------------------------
    with st.expander("🏗️ Demand — Grande Vaitape", expanded=False):
        gv_commissioning_year = st.number_input("Commissioning year", value=2027, step=1, key="gv_year")
        gv_annual_mwh = st.number_input("Annual demand (MWh/yr)", value=4992.32, step=10.0, key="gv_annual")
        gv_shape_upload = st.file_uploader("Hourly day shape (CSV: hour, mw) - optional, else flat", type=["csv"], key="gv_upload")
        st.download_button("Download template", data.single_day_shape_template_csv(), "gv_shape_template.csv", key="gv_tmpl")

    # --- Grid / technical ---------------------------------------------
    with st.expander("⚡ Grid", expanded=False):
        edt_penetration_cap_pct = st.number_input("EDT intermittent penetration cap (% of demand)", value=100.0, min_value=0.0, max_value=200.0, step=5.0)

    # --- RE targets & glide path ---------------------------------------------
    with st.expander("🎯 RE targets & glide path", expanded=True):
        re_target_2030_pct = st.number_input("RE target, 2030 (%)", value=75.0, step=1.0)
        re_target_2050_pct = st.number_input("RE target, 2050 (%)", value=100.0, step=1.0)
        tranche_years_str = st.text_input("Tranche years (comma-separated)", "2028, 2030, 2035, 2040, 2050")
        target_buffer_pct = st.slider("Buffer at commissioning (percentage points)", 0.0, 15.0, 3.0, 0.5)
        curtailment_cap_pct = st.slider("Max curtailment (% of PV+Wind+OTEC generation)", 1.0, 30.0, 10.0, 1.0)

    # --- Search grid ---------------------------------------------
    with st.expander("🔍 Sizing search grid", expanded=False):
        pv_max = st.number_input("Max PV addition per tranche (MWp)", value=100, step=10)
        pv_step = st.number_input("PV step (MWp)", value=5, step=1)
        bess_max = st.number_input("Max BESS addition per tranche (MWh)", value=400, step=20)
        bess_step = st.number_input("BESS step (MWh)", value=20, step=10)

    # --- Economics ---------------------------------------------
    with st.expander("💰 Economics", expanded=False):
        analysis_end_year = st.number_input("Analysis horizon end year", value=2050, step=1)
        nominal_discount_rate_pct = st.number_input("Nominal discount rate (%)", value=8.0, step=0.5)
        inflation_rate_pct = st.number_input("Inflation rate (%)", value=2.0, step=0.5)

        st.markdown("**PV**")
        c1, c2 = st.columns(2)
        pv_capex_per_mwp = c1.number_input("CAPEX ($/MWp)", value=900_000, step=50_000, key="pv_capex")
        pv_om_per_mwp_yr = c2.number_input("O&M ($/MWp/yr)", value=12_000, step=1_000, key="pv_om")
        pv_lifetime = st.number_input("Lifetime (years)", value=25, step=1, key="pv_life")

        st.markdown("**Wind**")
        c1, c2 = st.columns(2)
        wind_capex_per_mw = c1.number_input("CAPEX ($/MW)", value=1_800_000, step=100_000, key="wind_capex")
        wind_om_per_mw_yr = c2.number_input("O&M ($/MW/yr)", value=40_000, step=5_000, key="wind_om")
        wind_lifetime = st.number_input("Lifetime (years)", value=20, step=1, key="wind_life")

        st.markdown("**OTEC**")
        c1, c2 = st.columns(2)
        otec_capex_per_mw = c1.number_input("CAPEX ($/MW)", value=95_000_000, step=1_000_000, key="otec_capex")
        otec_om_per_mw_yr = c2.number_input("O&M ($/MW/yr)", value=1_500_000, step=100_000, key="otec_om")
        otec_lifetime = st.number_input("Lifetime (years)", value=30, step=1, key="otec_life")

        st.markdown("**BESS**")
        c1, c2 = st.columns(2)
        bess_capex_per_mwh = c1.number_input("CAPEX ($/MWh)", value=350_000, step=25_000, key="bess_capex")
        bess_om_per_mwh_yr = c2.number_input("O&M ($/MWh/yr)", value=7_000, step=500, key="bess_om")
        bess_lifetime = st.number_input("Lifetime (years)", value=15, step=1, key="bess_life")

    run_button = st.button("🚀 Run", type="primary", use_container_width=True)

# ==============================================================================
# RUN
# ==============================================================================
if run_button:
    try:
        with st.spinner("Loading data..."):
            pv_cf = data.load_pv_cf(pv_cf_upload)
            wind_cf = data.load_wind_cf(wind_cf_upload) if (wind_enabled and wind_cf_upload is not None) else (
                data.load_wind_cf(None) if wind_enabled else None)
            baseline_demand = data.load_baseline_demand_shape_mw(baseline_shape_upload)
            underlying_table = data.load_annual_table(annual_table_upload, data.DEFAULT_UNDERLYING_DEMAND_MWH)
            ev_land_profiles = data.load_day_profiles_by_year(ev_land_upload)
            marine_profiles = data.load_day_profiles_by_year(marine_upload)
            combined_years = sorted(set(ev_land_profiles) | set(marine_profiles))
            ev_marine_profiles = {
                year: [a + b for a, b in zip(data.get_day_profile_for_year(ev_land_profiles, year),
                                              data.get_day_profile_for_year(marine_profiles, year))]
                for year in combined_years
            }
            gv_day_shape = data.load_single_day_shape(gv_shape_upload)
    except (ValueError, KeyError) as e:
        st.error(f"Problem reading an uploaded file: {e}")
        st.stop()

    tranche_years = tuple(int(y.strip()) for y in tranche_years_str.split(",") if y.strip())

    exogenous_tranches = []
    if otec_enabled:
        exogenous_tranches.append(eng.Tranche("otec", int(otec_commissioning_year), otec_degradation_pct / 100,
                                               capacity_mw=otec_mw))
    if wind_enabled:
        exogenous_tranches.append(eng.Tranche("wind", int(wind_commissioning_year), wind_degradation_pct / 100,
                                               capacity_mw=wind_mw))

    sim_kwargs = dict(
        pv_cf_hourly=pv_cf, wind_cf_hourly=wind_cf,
        dc_ac_ratio=dc_ac_ratio,
        bess_charge_eff=bess_charge_eff, bess_discharge_eff=bess_discharge_eff,
        otec_cf=otec_cf,
        edt_penetration_cap=edt_penetration_cap_pct / 100,
        baseline_demand_hourly_mw=baseline_demand,
        underlying_annual_table=underlying_table,
        ev_marine_day_profiles=ev_marine_profiles,
        gv_annual_mwh=gv_annual_mwh, gv_commissioning_year=int(gv_commissioning_year), gv_day_shape=gv_day_shape,
    )

    pv_candidates = list(range(0, int(pv_max) + 1, int(pv_step)))
    bess_candidates = list(range(0, int(bess_max) + 1, int(bess_step)))

    with st.spinner(f"Sizing {len(pv_candidates)}x{len(bess_candidates)} combinations x {len(tranche_years)} tranche years..."):
        schedule, log = eng.size_tranche_schedule(
            tranche_years=tranche_years,
            pv_degradation_rate=pv_degradation_pct / 100,
            bess_degradation_rate=bess_degradation_pct / 100,
            pv_candidates_mwp=pv_candidates, bess_candidates_mwh=bess_candidates, bess_c_rate=bess_c_rate,
            curtailment_cap_pct=curtailment_cap_pct, target_buffer_pct=target_buffer_pct,
            re_target_2030=re_target_2030_pct / 100, re_target_2050=re_target_2050_pct / 100,
            sim_kwargs=sim_kwargs,
            pv_capex_per_mwp=pv_capex_per_mwp, bess_capex_per_mwh=bess_capex_per_mwh,
            exogenous_tranches=exogenous_tranches,
        )

        years = list(range(2026, int(analysis_end_year) + 1))
        traj = eng.simulate_trajectory(schedule, years, **sim_kwargs)
        traj_df = pd.DataFrame(traj)

        unit_costs = dict(
            pv_capex_per_mwp=pv_capex_per_mwp, pv_om_per_mwp_yr=pv_om_per_mwp_yr,
            wind_capex_per_mw=wind_capex_per_mw, wind_om_per_mw_yr=wind_om_per_mw_yr,
            otec_capex_per_mw=otec_capex_per_mw, otec_om_per_mw_yr=otec_om_per_mw_yr,
            bess_capex_per_mwh=bess_capex_per_mwh, bess_om_per_mwh_yr=bess_om_per_mwh_yr,
        )
        lifetimes = dict(pv=pv_lifetime, wind=wind_lifetime, otec=otec_lifetime, bess=bess_lifetime)

        econ_cashflow = eng.compute_lifecycle_economics(
            schedule, traj_df, unit_costs, lifetimes,
            nominal_discount_rate_pct / 100, inflation_rate_pct / 100,
            2026, int(analysis_end_year), method="cashflow",
        )
        econ_homer = eng.compute_lifecycle_economics(
            schedule, traj_df, unit_costs, lifetimes,
            nominal_discount_rate_pct / 100, inflation_rate_pct / 100,
            2026, int(analysis_end_year), method="homer",
        )

    st.session_state.update(bb_schedule=schedule, bb_log=log, bb_traj_df=traj_df,
                             bb_econ_cashflow=econ_cashflow, bb_econ_homer=econ_homer,
                             bb_re_target_2030=re_target_2030_pct / 100, bb_re_target_2050=re_target_2050_pct / 100,
                             bb_sim_kwargs=sim_kwargs, bb_years=years,
                             bb_hourly_csv=None,  # cleared on every new Run - stale hourly export otherwise
                             bb_done=True)

# ==============================================================================
# RESULTS
# ==============================================================================
if st.session_state.get("bb_done"):
    schedule = st.session_state["bb_schedule"]
    log = st.session_state["bb_log"]
    traj_df = st.session_state["bb_traj_df"]
    econ_cf = st.session_state["bb_econ_cashflow"]
    econ_hm = st.session_state["bb_econ_homer"]
    re_2030 = st.session_state["bb_re_target_2030"]
    re_2050 = st.session_state["bb_re_target_2050"]

    st.markdown("---")

    infeasible = [l for l in log if l.get("status") == "INFEASIBLE"]
    if infeasible:
        st.warning(f"No feasible combination found for tranche year(s) {[l['year'] for l in infeasible]} "
                   f"within the current search grid. Widen the PV/BESS max or step.")

    st.subheader("Tranche sizing")
    log_rows = [{
        "Tranche Year": l["year"], "PV Added (MWp)": l["pv_add"], "BESS Added (MWh)": l["bess_add_mwh"],
        "RE% at Commissioning": f"{l['re_pct']:.1f}%", "Target (+buffer)": f"{l['required_pct']:.1f}%",
        "Curtailment %": f"{l['curtailment_pct']:.1f}%",
    } for l in log if l.get("status") == "OK"]
    if log_rows:
        st.dataframe(pd.DataFrame(log_rows), use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("RE% trajectory")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=traj_df["year"], y=traj_df["re_pct"], name="RE%",
                              mode="lines+markers", line=dict(color="#2E7D32", width=3)))
    target_line = [eng.re_target_for_year(y, re_2030, re_2050) * 100 for y in traj_df["year"]]
    fig.add_trace(go.Scatter(x=traj_df["year"], y=target_line, name="Glide-path reference",
                              mode="lines", line=dict(color="orange", dash="dash")))
    fig.add_hline(y=re_2030 * 100, line_dash="dot", line_color="red")
    fig.add_hline(y=re_2050 * 100, line_dash="dot", line_color="red")
    fig.update_layout(xaxis_title="Year", yaxis_title="RE Penetration (%)", height=420,
                       hovermode="x unified", plot_bgcolor="white", paper_bgcolor="white")
    st.plotly_chart(fig, use_container_width=True)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Capacity build-out")
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=traj_df["year"], y=traj_df["pv_mwp"], name="PV (MWp)",
                                   mode="lines", line=dict(color="#FDB462", width=2), fill="tozeroy"))
        fig2.add_trace(go.Scatter(x=traj_df["year"], y=traj_df["otec_mw"], name="OTEC (MW)",
                                   mode="lines", line=dict(color="#80B1D3", width=2)))
        if traj_df["wind_mw"].max() > 0:
            fig2.add_trace(go.Scatter(x=traj_df["year"], y=traj_df["wind_mw"], name="Wind (MW)",
                                       mode="lines", line=dict(color="#8DD3C7", width=2)))
        fig2.update_layout(xaxis_title="Year", yaxis_title="Capacity (MW)", height=380,
                            plot_bgcolor="white", paper_bgcolor="white")
        st.plotly_chart(fig2, use_container_width=True)
    with col2:
        st.subheader("Curtailment")
        fig3 = go.Figure()
        fig3.add_trace(go.Bar(x=traj_df["year"], y=traj_df["total_curtailment_mwh"], marker_color="#E63946"))
        fig3.update_layout(xaxis_title="Year", yaxis_title="Curtailment (MWh)", height=380,
                            plot_bgcolor="white", paper_bgcolor="white")
        st.plotly_chart(fig3, use_container_width=True)

    st.markdown("---")
    st.subheader("Economics")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("NPC — HOMER method", f"${econ_hm['npv_cost']/1e6:.2f}M")
    col2.metric("LCOE — HOMER method", f"${econ_hm['lcoe_per_mwh']:.2f}/MWh")
    col3.metric("NPC — Cash-flow method", f"${econ_cf['npv_cost']/1e6:.2f}M")
    col4.metric("LCOE — Cash-flow method", f"${econ_cf['lcoe_per_mwh']:.2f}/MWh")

    comp_rows = [
        ["Discount rate", "Real", "Nominal"],
        ["O&M treatment", "Constant (real terms)", "Escalates with inflation"],
        ["Replacement", "Modeled at each tranche's own lifetime", "Not modeled"],
        ["Salvage", "Included (prorated remaining life)", "Not modeled"],
        ["NPC ($M)", f"{econ_hm['npv_cost']/1e6:.2f}", f"{econ_cf['npv_cost']/1e6:.2f}"],
        ["LCOE ($/MWh)", f"{econ_hm['lcoe_per_mwh']:.2f}", f"{econ_cf['lcoe_per_mwh']:.2f}"],
    ]
    st.dataframe(pd.DataFrame(comp_rows, columns=["", "HOMER method", "Cash-flow method"]),
                 use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("Full trajectory")
    display_cols = ["year", "pv_mwp", "wind_mw", "otec_mw", "bess_energy_mwh", "re_pct", "unmet_pct",
                     "total_curtailment_mwh", "curtailment_pct_of_re_gen", "total_demand_mwh"]
    st.dataframe(traj_df[display_cols].round(2), use_container_width=True, hide_index=True)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        traj_df.to_excel(writer, sheet_name="Trajectory", index=False)
        pd.DataFrame(log_rows).to_excel(writer, sheet_name="Tranche_Decisions", index=False)
        pd.DataFrame(comp_rows, columns=["Metric", "HOMER", "Cash-flow"]).to_excel(writer, sheet_name="Economics", index=False)
    st.download_button("Download results (Excel)", data=buf.getvalue(), file_name="bora_bora_capacity_plan.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.markdown("---")
    st.subheader("Full hourly dispatch (every year, 8,760 hours each)")
    st.caption("One row per hour per year - demand, PV/wind/OTEC generation, BESS charge/discharge/state of "
               "charge, served, unmet and curtailment, all in MW (MWh for SOC). Matches this run's locked-in "
               "tranche schedule. This is a large file (years × 8,760 rows) and takes a few seconds to build.")
    if st.button("Generate hourly dispatch CSV"):
        with st.spinner("Simulating hourly dispatch for every year..."):
            hourly_rows = eng.simulate_trajectory_hourly(
                schedule, st.session_state["bb_years"], **st.session_state["bb_sim_kwargs"]
            )
            hourly_df = pd.DataFrame(hourly_rows)
            st.session_state["bb_hourly_csv"] = hourly_df.to_csv(index=False)
    if st.session_state.get("bb_hourly_csv"):
        st.download_button("Download hourly dispatch (CSV)", data=st.session_state["bb_hourly_csv"],
                            file_name="bora_bora_hourly_dispatch_all_years.csv", mime="text/csv")

else:
    st.info("Configure inputs in the sidebar, then click Run.")

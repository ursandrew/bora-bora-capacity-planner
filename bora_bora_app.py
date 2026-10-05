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
import bora_bora_export

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
    with st.expander("☀️ Solar PV (Agri)", expanded=True):
        pv_cf_upload = st.file_uploader("Hourly generation-factor profile (CSV) - required", type=["csv"], key="pv_upload")
        st.download_button("Download blank template", data.pv_cf_template_csv(), "pv_cf_template.csv", key="pv_tmpl")
        dc_ac_ratio = st.number_input("DC:AC ratio", value=1.3, step=0.05)
        pv_degradation_pct = st.number_input("Degradation rate (%/yr)", value=0.3, step=0.1)

    # --- Rooftop ----------------------------------------------------------
    with st.expander("🏠 Rooftop PV", expanded=False):
        rooftop_enabled = st.checkbox("Include rooftop (exported-to-grid share only)", value=True)
        st.caption("The self-consumed share is already netted out of your Underlying annual demand table "
                   "(Forecast_Annual's 'net of rooftop solar' row) - adding it again here would double-count it. "
                   "Only the exported share below is added as generation. Uses the same hourly CF shape as Agri PV.")
        rooftop_existing_mwp = st.number_input("Existing capacity (MWp)", value=2.357, step=0.1, disabled=not rooftop_enabled)
        rooftop_ceiling_mwp = st.number_input("Ceiling capacity (MWp)", value=4.5, step=0.1, disabled=not rooftop_enabled)
        rooftop_ramp_start_year = st.number_input(
            "Last year at existing capacity (ramp begins the following year)", value=2025, step=1,
            disabled=not rooftop_enabled,
            help="Matches Forecast_Annual: capacity is flat at 'Existing capacity' through this year, then "
                 "grows by 'New capacity added (MWp/yr)' every year after it, capped at 'Ceiling capacity'.")
        rooftop_ramp_mwp_per_year = st.number_input("New capacity added (MWp/yr)", value=0.143, step=0.01, format="%.3f",
                                                     disabled=not rooftop_enabled)
        rooftop_self_consumption_pct = st.number_input("Self-consumption offset (%)", value=93.0, step=1.0,
                                                         disabled=not rooftop_enabled) / 100

    # --- Wind -------------------------------------------------------------
    with st.expander("💨 Wind", expanded=False):
        wind_enabled = st.checkbox("Include wind", value=False)
        wind_sizing_mode = st.radio("Wind sizing", ["Fixed capacity", "Optimize capacity"],
                                     disabled=not wind_enabled, horizontal=True,
                                     help="Fixed: you set the MW and commissioning year directly (a single one-time "
                                          "build). Optimize: wind is searched at EVERY tranche year, exactly like "
                                          "PV+BESS - it can be added at more than one tranche year if that's "
                                          "cheapest, each addition its own vintage-tracked block.")
        wind_degradation_pct = st.number_input("Degradation rate (%/yr)", value=0.5, step=0.1, disabled=not wind_enabled)
        if wind_sizing_mode == "Fixed capacity":
            wind_commissioning_year = st.number_input("Commissioning year", value=2028, step=1, disabled=not wind_enabled)
            wind_mw = st.number_input("Capacity (MW)", value=4.125, step=0.1, disabled=not wind_enabled)
            wind_min_mw, wind_max_mw, wind_step_mw = None, None, None
        else:
            wind_commissioning_year, wind_mw = None, None
            c1, c2, c3 = st.columns(3)
            wind_min_mw = c1.number_input("Min per tranche year (MW)", value=0.0, step=0.5, disabled=not wind_enabled)
            wind_max_mw = c2.number_input("Max per tranche year (MW)", value=5.0, step=0.5, disabled=not wind_enabled)
            wind_step_mw = c3.number_input("Step (MW)", value=1.0, step=0.5, disabled=not wind_enabled)
            st.caption("Adds a 3rd search dimension (PV x BESS x Wind) at EVERY tranche year - materially slower "
                       "than PV+BESS alone, and slower again the more tranche years you list.")
        wind_cf_upload = st.file_uploader("Hourly generation-factor profile (CSV) - required if wind is included", type=["csv"],
                                           key="wind_upload", disabled=not wind_enabled)
        st.download_button("Download blank template", data.wind_cf_template_csv(), "wind_cf_template.csv", key="wind_tmpl")

    # --- OTEC -------------------------------------------------------------
    with st.expander("🌊 OTEC", expanded=True):
        otec_enabled = st.checkbox("Include OTEC", value=True)
        otec_schedule_str = st.text_input(
            "OTEC schedule (format 'year:mw', comma-separated - each entry is its own vintage tranche)",
            "2032:1.2", disabled=not otec_enabled)
        st.caption("Each 'year:mw' entry commissions that much ADDITIONAL OTEC capacity in that year, stacking on "
                   "top of whatever OTEC is already online (own vintage/degradation clock, same as PV/BESS "
                   "tranches). Example: '2032:1.2, 2037:1.4, 2042:1.2, 2047:1.2' stages OTEC across four "
                   "milestones instead of one fixed build - independent of the PV/BESS tranche years below, so "
                   "you can lean on staged OTEC without re-sizing PV/BESS at those same years.")
        otec_cf = st.number_input("Capacity factor (applies to every OTEC tranche)", value=1.0, min_value=0.0, max_value=1.0, step=0.01, disabled=not otec_enabled)
        otec_degradation_pct = st.number_input("Degradation rate (%/yr, applies to every OTEC tranche)", value=0.0, step=0.1, disabled=not otec_enabled)

    # --- BESS -------------------------------------------------------------
    with st.expander("🔋 BESS", expanded=False):
        bess_c_rate = st.number_input("Power:energy ratio (C-rate)", value=0.5, min_value=0.05, max_value=2.0, step=0.05)
        bess_charge_eff = st.number_input("Charge efficiency", value=0.95, min_value=0.5, max_value=1.0, step=0.01)
        bess_discharge_eff = st.number_input("Discharge efficiency", value=0.95, min_value=0.5, max_value=1.0, step=0.01)
        bess_degradation_pct = st.number_input("Degradation rate (%/yr)", value=1.5, step=0.1)
        c1, c2, c3 = st.columns(3)
        bess_initial_soc_pct = c1.number_input("Initial SoC at commissioning (%)", value=50.0, min_value=0.0,
                                                max_value=100.0, step=1.0)
        bess_min_soc_pct = c2.number_input("Min SoC (%)", value=5.0, min_value=0.0, max_value=100.0, step=1.0)
        bess_max_soc_pct = c3.number_input("Max SoC (%)", value=95.0, min_value=0.0, max_value=100.0, step=1.0)
        st.caption("A new BESS tranche starts at Initial SoC (of its own added capacity) when it commissions; "
                   "any capacity already online keeps whatever charge it already had. Every hour, charge/discharge "
                   "is kept within [Min SoC, Max SoC] of that year's total BESS capacity.")
        if bess_min_soc_pct >= bess_max_soc_pct:
            st.warning("Min SoC should be less than Max SoC - fix this before running.")

    # --- Demand: baseline ---------------------------------------------
    with st.expander("🏝️ Demand — existing development (baseline)", expanded=False):
        st.caption("Shape = one full 8,760-hour reference year (the pattern). "
                   "Annual table = each year's total MWh (the multiplier applied to the shape).")
        baseline_shape_upload = st.file_uploader("Hourly demand shape, one calendar year (CSV) - required", type=["csv"], key="baseline_upload")
        st.download_button("Download blank template", data.baseline_demand_template_csv(), "baseline_demand_template.csv", key="baseline_tmpl")
        annual_table_upload = st.file_uploader("Annual demand by year (CSV) - optional, falls back to the bundled Forecast_Annual figures below", type=["csv"], key="annual_upload")
        st.download_button("Download current table (bundled default)", data.annual_table_default_csv(), "annual_demand.csv", key="annual_tmpl")
        underlying_growth_beyond_pct = st.number_input(
            "Growth beyond the last table year (%/yr)", value=0.0, step=0.1,
            help="The bundled table ends at 2040. After the last year in the table the baseline demand is "
                 "held flat (0%) or grown at this rate. EV/marine and Grande Vaitape are independent of this.")

    # --- Demand: EV chargers (land, excl. bus) ---------------------------------------------
    with st.expander("🔌 Demand — EV chargers (land, excl. bus)", expanded=False):
        st.caption("Hour (0-23) as rows, one column per year - same layout as Combined_Hourly_Load. "
                   "One day's pattern is repeated for every day of that year. Years after the last column "
                   "in the file hold that last year's profile (flat), years before the first hold the first.")
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
        enforce_curtailment_cap = st.checkbox(
            "Enforce a max-curtailment gate on the PV/BESS/wind sizing search", value=True,
            help="The RE%/unmet% target above is always the real (legally-driven) constraint. This curtailment "
                 "gate is an extra cost/efficiency guardrail on top of it, not a requirement of the project - "
                 "turn it off when a fixed OTEC schedule is already meeting the RE target on its own and you "
                 "don't want the search reporting 'INFEASIBLE' just because curtailment is high.")
        curtailment_cap_pct = st.slider("Max curtailment (% of PV+Wind+OTEC generation)", 1.0, 100.0, 30.0, 1.0,
                                         disabled=not enforce_curtailment_cap)
        if not enforce_curtailment_cap:
            curtailment_cap_pct = 100.0
        target_overrides_str = st.text_input(
            "Target overrides per tranche year (optional, format 'year:pct', comma-separated)", "")
        st.caption("Pins an exact required RE% at one specific tranche year, replacing the glide-path+buffer "
                   "calculation for that year only - every other tranche year keeps using the glide path and "
                   "buffer above. Example: '2028:78' forces the 2028 sizing search to hit 78% RE at 2028 "
                   "without changing what's required at 2030/2040/2050.")

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
        escalate_capex = st.checkbox(
            "Escalate CAPEX with inflation to the year it is spent", value=True,
            help="Unit costs below are entered in the analysis start year's (2026) dollars. When ticked, a "
                 "build in year Y costs unit CAPEX x (1+inflation)^(Y-2026) - the same escalation O&M already "
                 "gets - before being discounted at the nominal rate. Untick to treat unit CAPEX as the "
                 "expected price at the time of each build (e.g. if you have already priced in cost declines).")
        replacement_enabled = st.checkbox(
            "Model end-of-life replacement inside the horizon", value=True,
            help="An asset is in service from its commissioning year through (commissioning year + lifetime), "
                 "inclusive. If that falls before the horizon end, a like-for-like replacement is commissioned "
                 "the following year: capacity back to nameplate, degradation restarts, CAPEX is booked again. "
                 "Untick to run every asset to the horizon end with no replacement (the old behaviour).")

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
        otec_capex_per_mw = c1.number_input("CAPEX ($/MW, variable part)", value=63_138_462, step=500_000, key="otec_capex",
                                             help="Default = EUR152M x 1.08 = $164.16M for the 2.6 MW high anchor "
                                                  "(2H Offshore Aug-2024 study, per the SWEET deck) / 2.6 MW = $63.14M/MW. "
                                                  "OTEC tranche CAPEX = fixed cost per tranche + this x MW.")
        otec_capex_fixed = c1.number_input("CAPEX ($, fixed per OTEC tranche)", value=0, step=1_000_000, key="otec_capex_fixed",
                                            help="Cost that does not scale with size (cold-water pipe, platform). "
                                                 "Leave at 0 for a purely linear $/MW cost. With two quoted points "
                                                 "(MW1, $1) and (MW2, $2): variable = ($2-$1)/(MW2-MW1), "
                                                 "fixed = $2 - variable x MW2.")
        otec_om_per_mw_yr = c2.number_input("O&M ($/MW/yr)", value=1_500_000, step=100_000, key="otec_om")
        otec_lifetime = st.number_input("Lifetime (years)", value=30, step=1, key="otec_life")

        st.markdown("**BESS**")
        c1, c2 = st.columns(2)
        bess_capex_per_mwh = c1.number_input("CAPEX ($/MWh)", value=350_000, step=25_000, key="bess_capex")
        bess_om_per_mwh_yr = c2.number_input("O&M ($/MWh/yr)", value=7_000, step=500, key="bess_om")
        bess_lifetime = st.number_input("Lifetime (years)", value=20, step=1, key="bess_life")

    run_button = st.button("🚀 Run", type="primary", use_container_width=True)

# ==============================================================================
# RUN
# ==============================================================================
if run_button:
    if bess_min_soc_pct >= bess_max_soc_pct:
        st.error("BESS Min SoC must be less than Max SoC.")
        st.stop()
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

    input_warnings = data.collect_input_warnings(
        pv_cf=pv_cf, wind_cf=wind_cf, baseline_shape=baseline_demand,
        ev_marine_profiles=ev_marine_profiles, gv_day_shape=gv_day_shape, gv_annual_mwh=gv_annual_mwh,
        annual_table=underlying_table,
    )

    try:
        tranche_years = tuple(sorted({int(y.strip()) for y in tranche_years_str.split(",") if y.strip()}))
    except ValueError:
        st.error("Tranche years must be whole years separated by commas, e.g. 2028, 2030, 2035.")
        st.stop()

    wind_optimize = wind_enabled and wind_sizing_mode == "Optimize capacity"

    target_overrides = {}
    for pair in target_overrides_str.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            st.error(f"Target override '{pair}' isn't in 'year:pct' format.")
            st.stop()
        y_str, p_str = pair.split(":", 1)
        try:
            target_overrides[int(y_str.strip())] = float(p_str.strip())
        except ValueError:
            st.error(f"Target override '{pair}' isn't in 'year:pct' format.")
            st.stop()

    otec_schedule = []
    if otec_enabled:
        for pair in otec_schedule_str.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if ":" not in pair:
                st.error(f"OTEC schedule entry '{pair}' isn't in 'year:mw' format.")
                st.stop()
            y_str, mw_str = pair.split(":", 1)
            try:
                otec_schedule.append((int(y_str.strip()), float(mw_str.strip())))
            except ValueError:
                st.error(f"OTEC schedule entry '{pair}' isn't in 'year:mw' format.")
                st.stop()
        if not otec_schedule:
            st.error("OTEC is included but no schedule entries were given - add at least one 'year:mw' entry.")
            st.stop()

    # Service lives used for end-of-life replacement (None -> asset runs to the horizon end, no replacement)
    lifetimes = ({"pv": int(pv_lifetime), "wind": int(wind_lifetime), "bess": int(bess_lifetime)}
                 if replacement_enabled else {})
    otec_life = int(otec_lifetime) if replacement_enabled else None

    exogenous_tranches = []
    for otec_year, otec_mw_add in otec_schedule:
        exogenous_tranches.append(eng.Tranche("otec", otec_year, otec_degradation_pct / 100,
                                               capacity_mw=otec_mw_add, lifetime_years=otec_life))
    if wind_enabled and not wind_optimize:
        exogenous_tranches.append(eng.Tranche("wind", int(wind_commissioning_year), wind_degradation_pct / 100,
                                               capacity_mw=wind_mw, lifetime_years=lifetimes.get("wind")))

    if wind_optimize:
        wind_candidates = []
        w = float(wind_min_mw)
        while w <= wind_max_mw + 1e-9:
            wind_candidates.append(round(w, 4))
            w += wind_step_mw
    else:
        wind_candidates = []

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
        rooftop_enabled=rooftop_enabled,
        rooftop_existing_mwp=rooftop_existing_mwp, rooftop_ceiling_mwp=rooftop_ceiling_mwp,
        rooftop_ramp_start_year=int(rooftop_ramp_start_year), rooftop_ramp_mwp_per_year=rooftop_ramp_mwp_per_year,
        rooftop_self_consumption_pct=rooftop_self_consumption_pct,
        bess_initial_soc_frac=bess_initial_soc_pct / 100,
        bess_min_soc_frac=bess_min_soc_pct / 100, bess_max_soc_frac=bess_max_soc_pct / 100,
        underlying_growth_beyond_last=underlying_growth_beyond_pct / 100,
    )

    unit_costs = dict(
        pv_capex_per_mwp=pv_capex_per_mwp, pv_om_per_mwp_yr=pv_om_per_mwp_yr,
        wind_capex_per_mw=wind_capex_per_mw, wind_om_per_mw_yr=wind_om_per_mw_yr,
        otec_capex_per_mw=otec_capex_per_mw, otec_capex_fixed=otec_capex_fixed, otec_om_per_mw_yr=otec_om_per_mw_yr,
        bess_capex_per_mwh=bess_capex_per_mwh, bess_om_per_mwh_yr=bess_om_per_mwh_yr,
    )
    econ_params = dict(
        nominal_discount_rate=nominal_discount_rate_pct / 100, inflation_rate=inflation_rate_pct / 100,
        analysis_end_year=int(analysis_end_year), escalate_capex=escalate_capex,
    )
    analysis_start_year = 2026

    pv_candidates = list(range(0, int(pv_max) + 1, int(pv_step)))
    bess_candidates = list(range(0, int(bess_max) + 1, int(bess_step)))

    wind_search_note = f" x {len(wind_candidates)} wind sizes (every tranche year)" if wind_candidates else ""
    with st.spinner(f"Sizing {len(pv_candidates)}x{len(bess_candidates)} combinations{wind_search_note} "
                     f"x {len(tranche_years)} tranche years..."):
        schedule, log, trials = eng.size_tranche_schedule(
            tranche_years=tranche_years,
            pv_degradation_rate=pv_degradation_pct / 100,
            bess_degradation_rate=bess_degradation_pct / 100,
            pv_candidates_mwp=pv_candidates, bess_candidates_mwh=bess_candidates, bess_c_rate=bess_c_rate,
            curtailment_cap_pct=curtailment_cap_pct, target_buffer_pct=target_buffer_pct,
            re_target_2030=re_target_2030_pct / 100, re_target_2050=re_target_2050_pct / 100,
            sim_kwargs=sim_kwargs,
            unit_costs=unit_costs, econ=econ_params,
            exogenous_tranches=exogenous_tranches,
            target_overrides=target_overrides,
            wind_candidates_mw=wind_candidates,
            wind_degradation_rate=wind_degradation_pct / 100 if wind_enabled else 0.0,
            lifetimes=lifetimes,
            analysis_start_year=analysis_start_year,
        )

        years = list(range(analysis_start_year, int(analysis_end_year) + 1))
        traj = eng.simulate_trajectory(schedule, years, **sim_kwargs)
        traj_df = pd.DataFrame(traj)

        econ = eng.compute_lifecycle_economics(
            schedule, traj_df, unit_costs,
            nominal_discount_rate_pct / 100, inflation_rate_pct / 100,
            analysis_start_year, int(analysis_end_year), escalate_capex=escalate_capex,
        )
        compliance_df = pd.DataFrame(eng.compliance_table(traj_df, re_target_2030_pct / 100, re_target_2050_pct / 100))

    st.session_state.update(bb_schedule=schedule, bb_log=log, bb_traj_df=traj_df, bb_trials=trials,
                             bb_econ=econ, bb_compliance_df=compliance_df, bb_input_warnings=input_warnings,
                             bb_export_inputs=dict(
                                 unit_costs=unit_costs, nominal_discount_rate=nominal_discount_rate_pct / 100,
                                 inflation_rate=inflation_rate_pct / 100, start_year=analysis_start_year,
                                 end_year=int(analysis_end_year), escalate_capex=escalate_capex,
                                 lifetimes=dict(lifetimes, otec=otec_life),
                                 degradation=dict(pv=pv_degradation_pct / 100, bess=bess_degradation_pct / 100,
                                                  wind=wind_degradation_pct / 100, otec=otec_degradation_pct / 100)),
                             bb_econ_flags=dict(escalate_capex=escalate_capex, replacement_enabled=replacement_enabled,
                                                inflation_pct=inflation_rate_pct, discount_pct=nominal_discount_rate_pct,
                                                lifetimes=dict(lifetimes, otec=otec_life)),
                             bb_re_target_2030=re_target_2030_pct / 100, bb_re_target_2050=re_target_2050_pct / 100,
                             bb_sim_kwargs=sim_kwargs, bb_years=years,
                             bb_hourly_xlsx=None,  # cleared on every new Run - stale hourly export otherwise
                             bb_done=True)

# ==============================================================================
# RESULTS
# ==============================================================================
if st.session_state.get("bb_done"):
    schedule = st.session_state["bb_schedule"]
    log = st.session_state["bb_log"]
    traj_df = st.session_state["bb_traj_df"]
    trials = st.session_state.get("bb_trials", [])
    trials_df = pd.DataFrame(trials) if trials else pd.DataFrame()
    econ = st.session_state["bb_econ"]
    compliance_df = st.session_state["bb_compliance_df"]
    econ_flags = st.session_state["bb_econ_flags"]
    re_2030 = st.session_state["bb_re_target_2030"]
    re_2050 = st.session_state["bb_re_target_2050"]

    st.markdown("---")

    for w in st.session_state.get("bb_input_warnings", []):
        st.warning(w)

    infeasible = [l for l in log if l.get("status") == "INFEASIBLE"]
    if infeasible:
        st.warning(f"No feasible combination found for tranche year(s) {[l['year'] for l in infeasible]} "
                   f"within the current search grid. Widen the PV/BESS max or step.")

    st.subheader("Tranche sizing")
    st.caption("Each tranche year picks the feasible PV/BESS(/wind) addition with the lowest lifecycle cost "
               "(NPC of that addition: escalated CAPEX + replacements + escalated O&M, discounted to 2026), "
               "given the tranches already locked in at earlier years. It is a year-by-year (greedy) search, "
               "not a joint optimisation across all tranche years.")
    log_rows = [{
        "Tranche Year": l["year"], "PV Added (MWp)": l["pv_add"], "BESS Added (MWh)": l["bess_add_mwh"],
        "Wind Added (MW)": l.get("wind_add", 0),
        "RE% at Commissioning": f"{l['re_pct']:.1f}%",
        "Target": f"{l['required_pct']:.1f}%" + (" (override)" if l.get("override") else " (glide+buffer)"),
        "Curtailment %": f"{l['curtailment_pct']:.1f}%",
        "Addition NPC ($M)": round(l["cost_proxy"] / 1e6, 2),
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

    st.subheader("Legal compliance check")
    st.caption("French Polynesia fixes two milestones: 75% RE by 2030 and 100% by 2050. The 'legal floor' column "
               "holds the 2030 target as a minimum from 2030 through 2049 (a conservative reading - the law itself "
               "only names the two milestone years). The glide-path column is the straight line between the "
               "milestones and is INFORMATIONAL only, not a legal requirement.")
    breaches = compliance_df[~compliance_df["meets_legal_floor"]]
    interim = compliance_df[(compliance_df["year"] >= 2030) & (compliance_df["year"] < 2050)]
    if breaches.empty:
        st.success(f"RE% meets the legal floor in all {len(compliance_df)} simulated years "
                   f"({int(compliance_df['year'].min())}-{int(compliance_df['year'].max())})."
                   + (f" Tightest margin between the milestones (2030-2049): "
                      f"{interim['margin_to_floor_pp'].min():.2f} pp in {int(interim.loc[interim['margin_to_floor_pp'].idxmin(), 'year'])}."
                      if not interim.empty else ""))
    else:
        st.error(f"RE% falls BELOW the legal floor in {len(breaches)} year(s): "
                 f"{', '.join(str(int(y)) for y in breaches['year'])}. Add a tranche year before the first breach, "
                 f"raise the commissioning buffer, or add capacity (e.g. a staged OTEC tranche).")
    below_glide = compliance_df[~compliance_df["meets_glide_ref"]]
    if not below_glide.empty:
        st.caption(f"Informational: below the straight-line glide path in {len(below_glide)} year(s) "
                   f"({', '.join(str(int(y)) for y in below_glide['year'])}).")
    comp_view = compliance_df.rename(columns={
        "year": "Year", "re_pct": "RE %", "legal_floor_pct": "Legal floor %", "meets_legal_floor": "Meets legal floor?",
        "margin_to_floor_pp": "Margin to floor (pp)", "glide_ref_pct": "Glide-path ref %",
        "meets_glide_ref": "At/above glide path?",
    }).round(2)
    with st.expander("Year-by-year compliance table", expanded=not breaches.empty):
        st.dataframe(comp_view, use_container_width=True, hide_index=True)

    st.subheader("Model integrity checks")
    c1, c2, c3 = st.columns(3)
    sim_hours = int(traj_df["bess_simultaneous_hours"].sum())
    c1.metric("Hours with BESS charging AND discharging", sim_hours,
              help="Must be 0 in every year. The dispatch allows one direction per hour.")
    c2.metric("Max hourly energy-balance error (MW)", f"{traj_df['max_energy_balance_error_mw'].max():.1e}",
              help="Generation - charge + discharge - curtailment + unmet vs demand, worst hour of any year.")
    c3.metric("Max BESS SoC bookkeeping error (MWh)", f"{traj_df['soc_balance_error_mwh'].abs().max():.1e}",
              help="End SoC - start SoC vs energy actually charged minus discharged, worst year.")
    if sim_hours > 0:
        st.error("Simultaneous BESS charge/discharge detected - this is a dispatch bug, do not use these results.")

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
    life_txt = ", ".join(f"{k.upper()} {v} yr" for k, v in econ_flags["lifetimes"].items() if v) or "none modelled"
    st.caption(
        f"NPV(costs) / NPV(project-served energy), both discounted at the nominal rate "
        f"({econ_flags['discount_pct']:g}%) to 2026 (year 0, factor 1.0). "
        f"CAPEX is {'escalated at ' + format(econ_flags['inflation_pct'], 'g') + '%/yr to the year it is spent' if econ_flags['escalate_capex'] else 'held at flat 2026 prices (NOT escalated)'}; "
        f"O&M escalates at {econ_flags['inflation_pct']:g}%/yr. "
        f"End-of-life replacement: {'ON (' + life_txt + ')' if econ_flags['replacement_enabled'] else 'OFF - every asset runs to the horizon end'}. "
        f"No salvage credit at the horizon end. The energy denominator EXCLUDES the rooftop-exported share "
        f"(privately owned, not in project CAPEX), allocated pro-rata from the hourly dispatch.")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("NPC (lifecycle)", f"${econ['npv_cost']/1e6:.2f}M")
    col2.metric("LCOE", f"${econ['lcoe_per_mwh']:.2f}/MWh")
    col3.metric("NPC: CAPEX incl. replacements", f"${econ['npv_capex']/1e6:.2f}M")
    col4.metric("NPC: O&M", f"${econ['npv_om']/1e6:.2f}M")

    tech_labels = {"pv": "PV", "wind": "Wind", "otec": "OTEC", "bess": "BESS"}
    contrib_rows = [
        {"Technology": tech_labels[tech], "Lifecycle cost ($M)": econ["cost_by_technology"][tech] / 1e6,
         "LCOE contribution ($/MWh)": econ["lcoe_contribution_by_technology"][tech]}
        for tech in ["pv", "wind", "otec", "bess"]
        if econ["cost_by_technology"][tech] > 0
    ]
    contrib_rows.append({
        "Technology": "Total", "Lifecycle cost ($M)": econ["npv_cost"] / 1e6,
        "LCOE contribution ($/MWh)": econ["lcoe_per_mwh"],
    })
    st.caption("Per-technology LCOE contribution — each technology's own discounted lifecycle cost "
               "(CAPEX + replacements + discounted O&M) divided by the system's total discounted energy, so the "
               "rows sum to the total LCOE above.")
    st.dataframe(pd.DataFrame(contrib_rows).round(2), use_container_width=True, hide_index=True)

    cashflow_df = pd.DataFrame(econ["cashflow_rows"])
    with st.expander("Year-by-year cash flow (for checking against a spreadsheet)", expanded=False):
        st.caption("Nominal $ per year by technology, the discount factor, and the discounted cost and energy "
                   "that make up NPC and LCOE. LCOE = sum of 'PV of cost' / sum of 'PV of energy'.")
        st.dataframe(cashflow_df.round(2), use_container_width=True, hide_index=True)

    exp_in = st.session_state["bb_export_inputs"]
    try:
        lcoe_xlsx = bora_bora_export.build_lcoe_workbook(
            schedule, traj_df, exp_in["unit_costs"], exp_in["nominal_discount_rate"], exp_in["inflation_rate"],
            exp_in["start_year"], exp_in["end_year"], exp_in["escalate_capex"], exp_in["lifetimes"],
            exp_in["degradation"], econ["lcoe_per_mwh"], econ["npv_cost"])
        st.download_button(
            "Download LCOE verification workbook (Excel, live formulas)", data=lcoe_xlsx,
            file_name="bora_bora_lcoe_verification.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            help="Inputs, Tranche_Schedule, Trajectory and Hybrid_LCOE sheets for THIS run, with working Excel "
                 "formulas. The sheet's LCOE equals the app's; change an input there to test a sensitivity.")
    except Exception as e:  # never let the export break the results page
        st.warning(f"LCOE verification workbook could not be built: {e}")

    st.markdown("---")
    st.subheader("Grid search transparency")
    st.caption("Every PV/BESS/Wind combination the sizing search actually evaluated at every tranche year - "
               "not just the one it picked. Use this to answer 'would X MWp / Y MWh also have worked?' directly "
               "from the table: filter to the year and look up that row's RE%/unmet%/curtailment% and whether "
               "each constraint was met. The search ranks feasible rows by 'NPC of addition' (that row's own "
               "lifecycle cost: CAPEX escalated to the tranche year, replacements, escalated O&M, discounted to "
               "2026). 'LCOE proxy' spreads that NPC over the years remaining to the horizon and divides by that "
               "year's project-served energy - a same-year, per-candidate comparison, not the whole-system "
               "lifecycle LCOE shown above. CAPEX/OPEX columns are in tranche-year dollars. The BESS in each "
               "trial starts from the charge the already-built fleet actually carries into that year, plus the "
               "new tranche at its commissioning SoC.")
    if trials_df.empty:
        st.info("No grid-search detail available for this run.")
    else:
        year_options = ["All tranche years"] + sorted(trials_df["tranche_year"].unique().tolist())
        col_a, col_b = st.columns([1, 2])
        with col_a:
            year_filter = st.selectbox("Filter by tranche year", year_options)
        with col_b:
            feasible_only = st.checkbox("Show only feasible combinations", value=False)

        view_df = trials_df.copy()
        if year_filter != "All tranche years":
            view_df = view_df[view_df["tranche_year"] == year_filter]
        if feasible_only:
            view_df = view_df[view_df["feasible"]]

        display_trial_cols = {
            "iteration": "Iteration", "tranche_year": "Tranche Year",
            "pv_add_mwp": "PV Added (MWp)", "bess_add_mwh": "BESS Added (MWh)", "wind_add_mw": "Wind Added (MW)",
            "capex_add": "CAPEX ($, tranche-yr $)", "opex_add_per_yr": "OPEX ($/yr, tranche-yr $)",
            "npc_add": "NPC of addition ($)", "lcoe_proxy_per_mwh": "LCOE proxy ($/MWh)",
            "re_pct": "RE% Achieved", "unmet_pct": "Unmet %", "curtailment_pct": "Curtailment %",
            "required_pct": "Required RE% (target+buffer/override)",
            "re_target_met": "RE Target Met?", "unmet_ceiling_met": "Unmet Ceiling Met?",
            "curtailment_cap_met": "Curtailment Cap Met?", "feasible": "Feasible?", "selected": "Selected (chosen)?",
        }
        pretty = view_df.rename(columns=display_trial_cols)[list(display_trial_cols.values())].round(2)
        st.caption(f"{len(pretty):,} of {len(trials_df):,} total combinations shown.")
        st.dataframe(pretty, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("Full trajectory")
    display_cols = ["year", "pv_mwp", "wind_mw", "otec_mw", "bess_energy_mwh", "re_pct", "unmet_pct",
                     "total_curtailment_mwh", "curtailment_pct_of_re_gen", "total_demand_mwh",
                     "total_served_mwh", "rooftop_served_mwh", "project_served_mwh"]
    st.dataframe(traj_df[display_cols].round(2), use_container_width=True, hide_index=True)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        traj_df.to_excel(writer, sheet_name="Trajectory", index=False)
        pd.DataFrame(log_rows).to_excel(writer, sheet_name="Tranche_Decisions", index=False)
        pd.DataFrame(contrib_rows).round(2).to_excel(writer, sheet_name="Economics", index=False)
        cashflow_df.to_excel(writer, sheet_name="LCOE_Cashflow", index=False)
        comp_view.to_excel(writer, sheet_name="Compliance", index=False)
        if not trials_df.empty:
            trials_df.rename(columns=display_trial_cols)[list(display_trial_cols.values())].to_excel(
                writer, sheet_name="Grid_Search_Detail", index=False)
    st.download_button("Download results (Excel)", data=buf.getvalue(), file_name="bora_bora_capacity_plan.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.markdown("---")
    st.subheader("Full hourly dispatch (every year, one sheet per year)")
    st.caption("One workbook, one sheet per year (Dispatch_2028, Dispatch_2030, ...), each with all 8,760 hours - "
               "demand split into baseline + extra (EV/marine/GV), rooftop (exported share) / Agri PV / wind / OTEC "
               "generation, the intermittent penetration-cap mechanics (deficit before BESS, available for BESS "
               "charge), BESS charge/discharge split before vs. after round-trip efficiency, state of charge (MWh "
               "and %), residual diesel, curtailment, a per-hour energy balance check column (should read ~0 every "
               "hour) and a simultaneous charge+discharge flag (should be 0 every hour). Matches this run's "
               "locked-in tranche schedule. This is a large file (years × 8,760 rows) and takes a few seconds "
               "to build.")
    if st.button("Generate hourly dispatch workbook"):
        with st.spinner("Simulating hourly dispatch for every year..."):
            hourly_by_year = eng.simulate_trajectory_hourly(
                schedule, st.session_state["bb_years"], **st.session_state["bb_sim_kwargs"]
            )
            hbuf = BytesIO()
            with pd.ExcelWriter(hbuf, engine="openpyxl") as writer:
                for year, rows in hourly_by_year.items():
                    pd.DataFrame(rows).to_excel(writer, sheet_name=f"Dispatch_{year}", index=False)
            st.session_state["bb_hourly_xlsx"] = hbuf.getvalue()
    if st.session_state.get("bb_hourly_xlsx"):
        st.download_button("Download hourly dispatch (Excel, one sheet per year)",
                            data=st.session_state["bb_hourly_xlsx"],
                            file_name="bora_bora_hourly_dispatch_by_year.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

else:
    st.info("Configure inputs in the sidebar, then click Run.")

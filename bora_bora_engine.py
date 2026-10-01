"""
bora_bora_engine.py
====================
Vintage-tracked, multi-tranche, multi-technology (PV / Wind / OTEC / BESS)
capacity-expansion engine. Every technical and economic assumption is a
function parameter with a default value - nothing is hardcoded that the
app's UI can't override.
"""

from dataclasses import dataclass, field
import numpy as np

import bora_bora_data as data

HOURS = data.HOURS_PER_YEAR


# ============================================================================
# TRANCHES - vintage-tracked capacity additions
# ============================================================================

@dataclass
class Tranche:
    technology: str          # 'pv', 'wind', 'otec', 'bess'
    commissioning_year: int
    degradation_rate: float
    capacity_mw: float = 0.0     # PV: MWp (DC); Wind/OTEC: MW
    power_mw: float = 0.0        # BESS only
    energy_mwh: float = 0.0      # BESS only
    capex_total: float = 0.0     # $ - stored on the tranche so cost tracking survives sizing
    om_per_year: float = 0.0     # $/yr - at nameplate, before degradation

    def effective_factor(self, year):
        if year < self.commissioning_year:
            return 0.0
        age = year - self.commissioning_year
        return (1 - self.degradation_rate) ** age


@dataclass
class TrancheSchedule:
    pv: list = field(default_factory=list)
    wind: list = field(default_factory=list)
    otec: list = field(default_factory=list)
    bess: list = field(default_factory=list)

    def clone(self):
        return TrancheSchedule(pv=list(self.pv), wind=list(self.wind),
                                otec=list(self.otec), bess=list(self.bess))

    def all_tranches(self):
        return self.pv + self.wind + self.otec + self.bess

    def effective_pv_mwp(self, year):
        return sum(t.capacity_mw * t.effective_factor(year) for t in self.pv)

    def effective_wind_mw(self, year):
        return sum(t.capacity_mw * t.effective_factor(year) for t in self.wind)

    def effective_otec_mw(self, year):
        return sum(t.capacity_mw * t.effective_factor(year) for t in self.otec)

    def effective_bess_power_mw(self, year):
        return sum(t.power_mw * t.effective_factor(year) for t in self.bess)

    def effective_bess_energy_mwh(self, year):
        return sum(t.energy_mwh * t.effective_factor(year) for t in self.bess)


# ============================================================================
# HOURLY DISPATCH SIMULATION FOR ONE CALENDAR YEAR
# ============================================================================

def rooftop_capacity_mwp(year, existing_mwp, ceiling_mwp, ramp_start_year, ramp_mwp_per_year):
    """Rooftop capacity is flat at `existing_mwp` through `ramp_start_year`, then
    grows by `ramp_mwp_per_year` every year after that, capped at `ceiling_mwp`.
    Matches the workbook's Forecast_Annual Total rooftop capacity row exactly
    with the defaults (existing=2.357, ceiling=4.5, ramp_start_year=2025,
    ramp_mwp_per_year=0.143): 2026->2.50, 2027->2.64, 2030->3.07, 2036->3.93,
    reaching the 4.5 MWp ceiling by 2040."""
    if year <= ramp_start_year:
        return existing_mwp
    return min(ceiling_mwp, existing_mwp + (year - ramp_start_year) * ramp_mwp_per_year)


def simulate_year(
    year,
    schedule: TrancheSchedule,
    pv_cf_hourly, wind_cf_hourly,
    dc_ac_ratio,
    bess_charge_eff, bess_discharge_eff,
    otec_cf,
    edt_penetration_cap,
    baseline_demand_hourly_mw,
    underlying_annual_table,
    ev_marine_day_profiles,
    gv_annual_mwh, gv_commissioning_year, gv_day_shape,
    initial_soc_mwh=None,
    return_hourly=False,
    rooftop_enabled=False,
    rooftop_existing_mwp=0.0, rooftop_ceiling_mwp=0.0,
    rooftop_ramp_start_year=2025, rooftop_ramp_mwp_per_year=0.0,
    rooftop_self_consumption_pct=0.93,
    bess_initial_soc_frac=0.5, bess_min_soc_frac=0.05, bess_max_soc_frac=0.95,
):
    """Merit-order dispatch: PV -> Wind -> OTEC -> BESS discharge -> unmet
    (unmet = demand not covered by RE - implicitly the diesel-served
    fraction, since no diesel technology is modeled). Excess RE charges
    BESS up to its limits; anything left over is curtailed.

    Rooftop (if enabled) contributes ONLY its exported-to-grid share
    (1 - rooftop_self_consumption_pct) of its generation, using the same
    hourly CF shape as pv_cf_hourly - the self-consumed share is already
    netted out of `underlying_annual_table` upstream (Forecast_Annual's
    "net of rooftop solar" row), so adding it again here would double-count
    it. This mirrors the workbook's own Row 12/14 "exported to grid" split.

    `return_hourly=True` adds the full 8,760-hour arrays (demand, each
    technology's generation, BESS charge/discharge/SOC, unmet, curtailment)
    to the returned dict under an "hourly" key - off by default since the
    sizing search calls this thousands of times and only needs the annual
    totals.

    BESS SOC: the fleet is tracked as one pooled scalar (not per-tranche), so
    "a new tranche starts at bess_initial_soc_frac" is modeled as: whenever
    this year's effective BESS energy capacity is larger than last year's
    (schedule.effective_bess_energy_mwh(year) > ...(year-1)), that INCREMENT
    is topped up to bess_initial_soc_frac and added to whatever SOC the
    existing fleet already had - the pre-existing capacity's charge level is
    left untouched. Every hour, charge/discharge is bounded so SOC never
    leaves [bess_min_soc_frac, bess_max_soc_frac] of that year's total
    capacity.
    """
    pv_mwp = schedule.effective_pv_mwp(year)
    pv_mwac = pv_mwp / dc_ac_ratio if dc_ac_ratio else pv_mwp
    wind_mw = schedule.effective_wind_mw(year)
    otec_mw = schedule.effective_otec_mw(year)
    bess_power_mw = schedule.effective_bess_power_mw(year)
    bess_energy_mwh = schedule.effective_bess_energy_mwh(year)

    pv_cf = np.array(pv_cf_hourly)
    wind_cf = np.array(wind_cf_hourly) if (wind_mw > 0 and wind_cf_hourly is not None) else np.zeros(HOURS)

    pv_gen = pv_mwac * pv_cf
    wind_gen = wind_mw * wind_cf
    otec_gen = np.full(HOURS, otec_mw * otec_cf)

    # --- Rooftop: exported-to-grid share only (see docstring) ---
    if rooftop_enabled:
        rooftop_mwp = rooftop_capacity_mwp(year, rooftop_existing_mwp, rooftop_ceiling_mwp,
                                            rooftop_ramp_start_year, rooftop_ramp_mwp_per_year)
        rooftop_gen_raw = rooftop_mwp * pv_cf  # same solar CF shape as Agri PV
        rooftop_gen_exported = rooftop_gen_raw * (1 - rooftop_self_consumption_pct)
    else:
        rooftop_mwp = 0.0
        rooftop_gen_exported = np.zeros(HOURS)

    # --- demand: underlying (existing-development) + EV/marine + Grande Vaitape ---
    baseline_shape = np.array(baseline_demand_hourly_mw)
    underlying_mwh = data.lookup_annual(underlying_annual_table, year)
    scale = underlying_mwh / data.BASELINE_ANNUAL_DEMAND_MWH
    underlying_hourly_mw = baseline_shape * scale

    ev_marine_day = data.get_day_profile_for_year(ev_marine_day_profiles, year)
    ev_marine_hourly_mw = np.array(data.tile_day_profile_to_year(ev_marine_day))

    if year >= gv_commissioning_year:
        if gv_day_shape is not None:
            gv_hourly_mw = np.array(data.tile_day_profile_to_year(gv_day_shape))
        else:
            gv_hourly_mw = np.full(HOURS, gv_annual_mwh / HOURS)
    else:
        gv_hourly_mw = np.zeros(HOURS)

    demand = underlying_hourly_mw + ev_marine_hourly_mw + gv_hourly_mw

    # --- EDT intermittent penetration cap (solar+wind only, not OTEC) ---
    # Limits how much of INSTANTANEOUS DEMAND may be served directly by
    # solar+wind - not a ceiling on total generation. Anything above that
    # limit goes to charge the BESS first, and is only curtailed if the BESS
    # is already full.
    intermittent_gen = pv_gen + wind_gen + rooftop_gen_exported
    cap_limit = edt_penetration_cap * demand
    intermittent_to_grid = np.minimum(intermittent_gen, cap_limit)
    intermittent_excess_from_cap = intermittent_gen - intermittent_to_grid

    remaining_demand_after_intermittent = demand - intermittent_to_grid
    otec_to_grid = np.minimum(otec_gen, np.maximum(remaining_demand_after_intermittent, 0))
    otec_excess = otec_gen - otec_to_grid

    total_served_by_gen = intermittent_to_grid + otec_to_grid
    net_load = demand - total_served_by_gen
    total_excess_for_bess = intermittent_excess_from_cap + otec_excess

    min_soc_mwh = bess_min_soc_frac * bess_energy_mwh
    max_soc_mwh = bess_max_soc_frac * bess_energy_mwh

    prior_bess_energy_mwh = schedule.effective_bess_energy_mwh(year - 1)
    newly_commissioned_mwh = max(0.0, bess_energy_mwh - prior_bess_energy_mwh)
    if initial_soc_mwh is not None:
        # continuing from a previous simulated year: keep the carried-over SOC, and top up
        # ONLY the newly-commissioned increment (if any) to the initial-SOC fraction
        soc = initial_soc_mwh + bess_initial_soc_frac * newly_commissioned_mwh
    else:
        # first simulated year (or a sizing-search trial, which never carries SOC forward):
        # the whole effective fleet at this point starts at the initial-SOC fraction
        soc = bess_initial_soc_frac * bess_energy_mwh
    soc = max(min_soc_mwh, min(soc, max_soc_mwh))

    unmet = np.zeros(HOURS)
    curtailed_after_bess = np.zeros(HOURS)
    bess_charge = np.zeros(HOURS)              # AC-side power drawn to charge, BEFORE charging-efficiency loss
    bess_charge_after_eff = np.zeros(HOURS)     # energy actually added to stored SOC, AFTER charging-efficiency loss
    bess_discharge = np.zeros(HOURS)            # AC-side power delivered to grid, AFTER discharge-efficiency loss
    bess_discharge_before_eff = np.zeros(HOURS)  # energy actually drawn out of stored SOC, BEFORE discharge-efficiency loss
    soc_hourly = np.zeros(HOURS)

    for h in range(HOURS):
        shortfall = net_load[h]
        excess = total_excess_for_bess[h]

        if shortfall > 0:
            max_discharge = min(bess_power_mw, max(0.0, soc - min_soc_mwh) * bess_discharge_eff)
            d = min(shortfall, max_discharge)
            d_from_soc = d / bess_discharge_eff if bess_discharge_eff > 0 else d
            soc -= d_from_soc
            bess_discharge[h] = d
            bess_discharge_before_eff[h] = d_from_soc
            unmet[h] = shortfall - d

        if excess > 0:
            room = max(0.0, max_soc_mwh - soc)
            max_charge = min(bess_power_mw, room / bess_charge_eff if bess_charge_eff > 0 else room)
            c = min(excess, max_charge)
            c_to_soc = c * bess_charge_eff
            soc += c_to_soc
            bess_charge[h] = c
            bess_charge_after_eff[h] = c_to_soc
            curtailed_after_bess[h] = excess - c

        soc = max(min_soc_mwh, min(soc, max_soc_mwh))
        soc_hourly[h] = soc

    total_demand = demand.sum()
    total_unmet = unmet.sum()
    total_served = total_demand - total_unmet
    total_curtailment = curtailed_after_bess.sum()
    re_pct = (total_served / total_demand * 100) if total_demand > 0 else 0
    unmet_pct = (total_unmet / total_demand * 100) if total_demand > 0 else 0
    total_re_gen = pv_gen.sum() + wind_gen.sum() + otec_gen.sum() + rooftop_gen_exported.sum()
    curtailment_pct_of_re_gen = (total_curtailment / total_re_gen * 100) if total_re_gen > 0 else 0

    result = {
        "year": year,
        "pv_mwp": pv_mwp, "pv_mwac": pv_mwac, "wind_mw": wind_mw,
        "otec_mw": otec_mw, "bess_power_mw": bess_power_mw, "bess_energy_mwh": bess_energy_mwh,
        "rooftop_mwp": rooftop_mwp,
        "total_demand_mwh": total_demand, "total_served_mwh": total_served,
        "total_unmet_mwh": total_unmet, "unmet_pct": unmet_pct,
        "total_curtailment_mwh": total_curtailment,
        "curtailment_pct_of_re_gen": curtailment_pct_of_re_gen,
        "re_pct": re_pct,
        "pv_gen_mwh": pv_gen.sum(), "wind_gen_mwh": wind_gen.sum(), "otec_gen_mwh": otec_gen.sum(),
        "rooftop_gen_exported_mwh": rooftop_gen_exported.sum(),
        "bess_discharge_mwh": bess_discharge.sum(), "bess_charge_mwh": bess_charge.sum(),
        "ending_soc_mwh": soc,
    }

    if return_hourly:
        soc_pct = (soc_hourly / bess_energy_mwh * 100) if bess_energy_mwh > 0 else np.zeros(HOURS)
        deficit_before_bess = np.maximum(net_load, 0.0)
        # Energy balance check: total generation, minus whatever left the balance (BESS charge and
        # curtailment), plus whatever entered it (BESS discharge and unmet/diesel), should equal demand
        # exactly every hour - this should compute to ~0 (floating-point only) throughout; a persistent
        # non-zero value would mean the dispatch logic has an energy-conservation bug.
        balance_check = demand - (
            pv_gen + wind_gen + otec_gen + rooftop_gen_exported
            - bess_charge + bess_discharge - curtailed_after_bess + unmet
        )
        result["hourly"] = {
            "hour": np.arange(HOURS),
            "underlying_demand_mw": underlying_hourly_mw,
            "extra_demand_mw": ev_marine_hourly_mw + gv_hourly_mw,
            "demand_mw": demand,
            "pv_gen_mw": pv_gen,
            "wind_gen_mw": wind_gen,
            "otec_gen_mw": otec_gen,
            "rooftop_gen_mw": rooftop_gen_exported,
            "intermittent_raw_mw": intermittent_gen,
            "deficit_before_bess_mw": deficit_before_bess,
            "available_for_bess_charge_mw": total_excess_for_bess,
            "bess_charge_before_eff_mw": bess_charge,
            "bess_charge_after_eff_mw": bess_charge_after_eff,
            "bess_discharge_after_eff_mw": bess_discharge,
            "bess_discharge_before_eff_mw": bess_discharge_before_eff,
            "soc_mwh": soc_hourly,
            "soc_pct": soc_pct,
            "unmet_mw": unmet,
            "curtailment_mw": curtailed_after_bess,
            "served_mw": demand - unmet,
            "balance_check_mw": balance_check,
        }

    return result


def simulate_trajectory(schedule: TrancheSchedule, years, **sim_kwargs):
    """Runs simulate_year across a range of years, carrying BESS SOC forward."""
    results = []
    soc = None
    for year in years:
        r = simulate_year(year, schedule, initial_soc_mwh=soc, **sim_kwargs)
        soc = r["ending_soc_mwh"]
        results.append(r)
    return results


_MONTH_DAY_COUNTS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]  # non-leap 365-day calendar


def _month_day_hour_for_hour_of_year(hour_of_year):
    """Maps a 0-based hour-of-year (0..8759, non-leap 365-day calendar) to
    (month 1-12, day 1-31, hour_of_day 0-23), for the Month/Day/Hour of Day
    columns in the per-year dispatch sheets."""
    day_of_year = hour_of_year // 24  # 0-based
    hour_of_day = hour_of_year % 24
    remaining = day_of_year
    for month_index, days_in_month in enumerate(_MONTH_DAY_COUNTS, start=1):
        if remaining < days_in_month:
            return month_index, remaining + 1, hour_of_day
        remaining -= days_in_month
    return 12, 31, hour_of_day  # unreachable given exactly 365 days, kept as a safe fallback


def simulate_trajectory_hourly(schedule: TrancheSchedule, years, **sim_kwargs):
    """Like simulate_trajectory, but returns the full 8,760-hour dispatch
    detail for every year, keyed by year: {year: [row_dict, ...]}. Each
    year's rows are meant for their own sheet (Dispatch_<year>), mirroring
    the source Excel workbook's Dispatch_2028/2030/2035/... sheet-per-year
    layout, with matching column names. Use for a detailed year-by-year
    dispatch review; this is ~8,760 rows per year, so only call it once per
    schedule (not inside a sizing search)."""
    by_year = {}
    soc = None
    for year in years:
        r = simulate_year(year, schedule, initial_soc_mwh=soc, return_hourly=True, **sim_kwargs)
        soc = r["ending_soc_mwh"]
        h = r["hourly"]
        n = len(h["hour"])
        rows = []
        for i in range(n):
            month, day, hour_of_day = _month_day_hour_for_hour_of_year(i)
            rows.append({
                "Hour": i + 1,
                "Month": month,
                "Day": day,
                "Hour of day": hour_of_day,
                "Baseline demand scaled (MW)": h["underlying_demand_mw"][i],
                "Extra demand: EV+Bus+Marine+GV (MW)": h["extra_demand_mw"][i],
                "Total demand (MW)": h["demand_mw"][i],
                "Rooftop gen (MW)": h["rooftop_gen_mw"][i],
                "Agri gen (MW)": h["pv_gen_mw"][i],
                "Wind gen (MW)": h["wind_gen_mw"][i],
                "OTEC gen (MW)": h["otec_gen_mw"][i],
                "Intermittent raw (MW)": h["intermittent_raw_mw"][i],
                "Deficit before BESS (MW)": h["deficit_before_bess_mw"][i],
                "Available for BESS charge (MW)": h["available_for_bess_charge_mw"][i],
                "BESS charge before eff (MWh)": h["bess_charge_before_eff_mw"][i],
                "BESS charge after eff (MWh)": h["bess_charge_after_eff_mw"][i],
                "BESS discharge after eff (MWh)": h["bess_discharge_after_eff_mw"][i],
                "BESS discharge before eff (MWh)": h["bess_discharge_before_eff_mw"][i],
                "SoC (MWh)": h["soc_mwh"][i],
                "SoC (%)": h["soc_pct"][i],
                "Residual diesel (MW)": h["unmet_mw"][i],
                "Curtailment (MW)": h["curtailment_mw"][i],
                "Energy balance check (MW)": h["balance_check_mw"][i],
            })
        by_year[year] = rows
    return by_year


# ============================================================================
# ECONOMICS - HOMER-style and cash-flow-style NPC/LCOE
# ============================================================================

def crf(rate, lifetime_yr):
    if rate == 0:
        return 1 / lifetime_yr
    return (rate * (1 + rate) ** lifetime_yr) / ((1 + rate) ** lifetime_yr - 1)


def _tranche_capex(t: Tranche, unit_costs):
    if t.technology == "pv":
        return t.capacity_mw * unit_costs["pv_capex_per_mwp"]
    if t.technology == "wind":
        return t.capacity_mw * unit_costs["wind_capex_per_mw"]
    if t.technology == "otec":
        return t.capacity_mw * unit_costs["otec_capex_per_mw"]
    if t.technology == "bess":
        return t.energy_mwh * unit_costs["bess_capex_per_mwh"]
    return 0.0


def _tranche_om_per_year(t: Tranche, unit_costs):
    if t.technology == "pv":
        return t.capacity_mw * unit_costs["pv_om_per_mwp_yr"]
    if t.technology == "wind":
        return t.capacity_mw * unit_costs["wind_om_per_mw_yr"]
    if t.technology == "otec":
        return t.capacity_mw * unit_costs["otec_om_per_mw_yr"]
    if t.technology == "bess":
        return t.energy_mwh * unit_costs["bess_om_per_mwh_yr"]
    return 0.0


def compute_lifecycle_economics(
    schedule: TrancheSchedule, traj_df, unit_costs,
    nominal_discount_rate, inflation_rate,
    analysis_start_year, analysis_end_year,
):
    """Builds a year-by-year system cash flow from analysis_start_year to
    analysis_end_year (inclusive) across ALL tranches, discounts it at the
    nominal discount rate, and returns total NPC and LCOE.

    CAPEX is booked once, in each tranche's own commissioning year only (no
    replacement modeled regardless of tranche lifetime); O&M escalates with
    inflation from analysis_start_year; no salvage credit at the horizon end.
    LCOE = NPV(costs) / NPV(energy-served), both discounted at the nominal
    rate - i.e. standard NPV(costs)/NPV(energy).

    This is the project's single LCOE methodology (the former "cash-flow"
    method), verified against the FDDA reference model (Oct 2026) to be the
    same formula: CAPEX-only-at-build, inflation-escalated O&M, nominal
    discounting throughout, discounted-energy denominator. The former
    "HOMER-style" alternative (real discount rate + per-tranche replacement
    + prorated salvage) has been removed; this is the sole method now.
    """
    rate = nominal_discount_rate

    energy_by_year = {int(row["year"]): row["total_served_mwh"] for _, row in traj_df.iterrows()}

    years = list(range(analysis_start_year, analysis_end_year + 1))
    npv_cost = 0.0
    npv_energy = 0.0
    cost_by_tech = {"pv": 0.0, "wind": 0.0, "otec": 0.0, "bess": 0.0}

    for year in years:
        t_idx = year - analysis_start_year
        discount_factor = (1 + rate) ** t_idx
        year_cost = 0.0

        for t in schedule.all_tranches():
            if year < t.commissioning_year:
                continue
            capex = _tranche_capex(t, unit_costs)
            om_base = _tranche_om_per_year(t, unit_costs)

            tech_cost = 0.0
            if year == t.commissioning_year:
                tech_cost += capex
            tech_cost += om_base * (1 + inflation_rate) ** t_idx

            year_cost += tech_cost
            cost_by_tech[t.technology] += tech_cost / discount_factor

        npv_cost += year_cost / discount_factor
        npv_energy += energy_by_year.get(year, 0.0) / discount_factor

    lcoe_per_mwh = (npv_cost / npv_energy) if npv_energy > 0 else 0.0
    # Per-technology LCOE contribution: each tech's own discounted lifecycle
    # cost (CAPEX + discounted O&M) divided by the SYSTEM's total discounted
    # energy, so the contributions are additive and sum to lcoe_per_mwh.
    lcoe_contribution_by_technology = {
        tech: (cost / npv_energy if npv_energy > 0 else 0.0)
        for tech, cost in cost_by_tech.items()
    }

    return {
        "npv_cost": npv_cost,
        "npv_energy_mwh": npv_energy,
        "lcoe_per_mwh": lcoe_per_mwh,
        "cost_by_technology": cost_by_tech,
        "lcoe_contribution_by_technology": lcoe_contribution_by_technology,
    }


# ============================================================================
# SEQUENTIAL TRANCHE SIZING
# ============================================================================

def re_target_for_year(year, target_2030, target_2050):
    """Straight-line reference between the two checkpoints, used only to
    compute the buffer at each tranche year - the actual constraint enforced
    is 'checkpoints-only, bounded by buffer', see size_tranche_schedule()."""
    if year <= 2030:
        return target_2030 * min(1.0, max(0.0, (year - 2024) / (2030 - 2024)))
    if year >= 2050:
        return target_2050
    frac = (year - 2030) / (2050 - 2030)
    return target_2030 + frac * (target_2050 - target_2030)


def size_tranche_schedule(
    tranche_years,
    pv_degradation_rate, bess_degradation_rate,
    pv_candidates_mwp, bess_candidates_mwh, bess_c_rate,
    curtailment_cap_pct, target_buffer_pct,
    re_target_2030, re_target_2050,
    sim_kwargs,           # dict of everything simulate_year needs besides `year`/`schedule`/`initial_soc_mwh`
    pv_capex_per_mwp, bess_capex_per_mwh,  # used only to rank candidates within a tranche year (cheapest first)
    exogenous_tranches=None,   # list of Tranche objects to seed the schedule with (OTEC, Wind if enabled)
    unmet_load_ceiling_pct=100.0,
    target_overrides=None,   # optional {year: required_pct} - pins an exact required RE% at that
                              # tranche year, bypassing the glide-path+buffer calculation for that
                              # year only; every other tranche year is unaffected.
    wind_candidates_mw=None,      # None/[] -> wind is not optimized (any wind capacity must be
                                   # passed in via exogenous_tranches instead, as a fixed input).
                                   # Otherwise wind is searched at EVERY tranche year, exactly like
                                   # PV/BESS - each addition becomes its own vintage-tracked tranche.
    wind_degradation_rate=0.0,
    wind_capex_per_mw=0.0,
    pv_om_per_mwp_yr=0.0, bess_om_per_mwh_yr=0.0, wind_om_per_mw_yr=0.0,  # for the per-candidate
                              # CAPEX/OPEX/LCOE-proxy columns in `trials` - purely informational,
                              # do not affect which candidate is chosen (the search still ranks by
                              # cost_proxy = CAPEX only, see below).
    lcoe_discount_rate=0.08, pv_lifetime=25, bess_lifetime=15, wind_lifetime=20,  # annualizes each
                              # candidate's own CAPEX (via CRF) for the LCOE-proxy column only.
    record_trials=True,      # when True (default), every candidate combination tried - feasible or
                              # not - is recorded and returned as a third value `trials`, for full
                              # grid-search transparency (see docstring). Set False to skip this and
                              # save a little memory/time on very large grids.
    verbose=False,
):
    """Greedy sequential sizing: at each tranche year, grid-search the
    minimum-cost (PV, BESS, and - if wind_candidates_mw is given - Wind)
    addition that satisfies the RE floor (target + buffer, or an explicit
    override) at that checkpoint year while respecting the curtailment cap,
    given all previously-locked tranches (now degraded to that year). With
    wind included, the search is 3D (PV x BESS x Wind) at every tranche
    year, not just PV x BESS - materially slower.

    Returns (schedule, log, trials):
      - schedule: the final TrancheSchedule with only the winning addition
        locked in at each tranche year.
      - log: one entry per tranche year, same as before (status, winning
        combo, target used).
      - trials: (when record_trials=True) one row per (tranche_year, PV, BESS,
        Wind) combination the search actually evaluated - the full grid, not
        just the winner - each with its RE%/unmet%/curtailment%, this
        candidate's own incremental CAPEX/OPEX/annualized-LCOE-proxy, and
        three boolean columns (re_target_met, unmet_ceiling_met,
        curtailment_cap_met) plus the combined `feasible` and whether it's
        the one actually `selected`. This is what full grid-search
        transparency (e.g. "would 24 MWp/80 MWh have worked? what about
        22 MWp/90 MWh?") is answered from directly, row by row, rather than
        by re-running anything.
    """
    schedule = TrancheSchedule()
    for t in (exogenous_tranches or []):
        getattr(schedule, t.technology).append(t)

    log = []
    trials = []
    target_overrides = target_overrides or {}
    wind_candidates_mw = wind_candidates_mw or []
    iteration = 0

    for ty in tranche_years:
        if ty in target_overrides:
            required_pct = target_overrides[ty]
            target_pct = required_pct
            is_override = True
        else:
            target_pct = re_target_for_year(ty, re_target_2030, re_target_2050) * 100
            required_pct = target_pct + (target_buffer_pct if ty < 2050 else 0.0)
            is_override = False

        # Wind is searched at every tranche year, exactly like PV/BESS, when wind_candidates_mw is
        # given; each year's chosen wind_add (including 0 = "add nothing this year") becomes its own
        # vintage-tracked tranche, same as PV/BESS additions.
        wind_candidates_this_year = wind_candidates_mw if wind_candidates_mw else [0]

        best = None
        best_trial_index = None
        for pv_add in pv_candidates_mwp:
            for bess_add_mwh in bess_candidates_mwh:
                for wind_add in wind_candidates_this_year:
                    trial = schedule.clone()
                    if pv_add > 0:
                        trial.pv.append(Tranche("pv", ty, pv_degradation_rate, capacity_mw=pv_add))
                    if bess_add_mwh > 0:
                        trial.bess.append(Tranche("bess", ty, bess_degradation_rate,
                                                   power_mw=bess_add_mwh * bess_c_rate, energy_mwh=bess_add_mwh))
                    if wind_add > 0:
                        trial.wind.append(Tranche("wind", ty, wind_degradation_rate, capacity_mw=wind_add))

                    r = simulate_year(ty, trial, **sim_kwargs)

                    re_target_met = r["re_pct"] >= required_pct
                    unmet_ceiling_met = r["unmet_pct"] <= unmet_load_ceiling_pct
                    curtailment_cap_met = r["curtailment_pct_of_re_gen"] <= curtailment_cap_pct
                    feasible = re_target_met and unmet_ceiling_met and curtailment_cap_met

                    # simple capital-cost proxy for ranking candidates within one tranche year
                    # (full NPC/LCOE across the whole schedule is computed once, separately,
                    # after the schedule is locked in - see compute_lifecycle_economics)
                    capex_add = (pv_add * pv_capex_per_mwp + bess_add_mwh * bess_capex_per_mwh
                                 + wind_add * wind_capex_per_mw)
                    opex_add = (pv_add * pv_om_per_mwp_yr + bess_add_mwh * bess_om_per_mwh_yr
                                + wind_add * wind_om_per_mw_yr)
                    # Candidate LCOE PROXY: this candidate's own CAPEX annualized over its own
                    # lifetime (via CRF) plus its OPEX, divided by THIS YEAR's total served energy.
                    # This is NOT the whole-system lifecycle LCOE (that needs the full multi-year
                    # schedule and is computed once, separately, via compute_lifecycle_economics) -
                    # it's a same-year, per-candidate proxy meant only for comparing candidates
                    # against each other on a consistent $/MWh basis.
                    annualized_capex = (pv_add * pv_capex_per_mwp * crf(lcoe_discount_rate, pv_lifetime)
                                         + bess_add_mwh * bess_capex_per_mwh * crf(lcoe_discount_rate, bess_lifetime)
                                         + wind_add * wind_capex_per_mw * crf(lcoe_discount_rate, wind_lifetime))
                    lcoe_proxy = ((annualized_capex + opex_add) / r["total_served_mwh"]
                                  if r["total_served_mwh"] > 0 else 0.0)

                    if record_trials:
                        iteration += 1
                        trials.append({
                            "iteration": iteration, "tranche_year": ty,
                            "pv_add_mwp": pv_add, "bess_add_mwh": bess_add_mwh, "wind_add_mw": wind_add,
                            "capex_add": capex_add, "opex_add_per_yr": opex_add,
                            "lcoe_proxy_per_mwh": lcoe_proxy,
                            "re_pct": r["re_pct"], "unmet_pct": r["unmet_pct"],
                            "curtailment_pct": r["curtailment_pct_of_re_gen"],
                            "required_pct": required_pct,
                            "re_target_met": re_target_met,
                            "unmet_ceiling_met": unmet_ceiling_met,
                            "curtailment_cap_met": curtailment_cap_met,
                            "feasible": feasible,
                            "selected": False,   # back-filled to True on the winning row once known
                        })

                    if not feasible:
                        continue

                    if best is None or capex_add < best["cost_proxy"]:
                        best = {"pv_add": pv_add, "bess_add_mwh": bess_add_mwh, "wind_add": wind_add,
                                "cost_proxy": capex_add,
                                "re_pct": r["re_pct"], "unmet_pct": r["unmet_pct"],
                                "curtailment_pct": r["curtailment_pct_of_re_gen"]}
                        if record_trials:
                            best_trial_index = len(trials) - 1

        if record_trials and best_trial_index is not None:
            trials[best_trial_index]["selected"] = True

        if best is None:
            log.append({"year": ty, "status": "INFEASIBLE", "target_pct": target_pct,
                        "required_pct": required_pct, "override": is_override})
            if verbose:
                print(f"[{ty}] INFEASIBLE at target {required_pct:.1f}% - widen PV/BESS candidate ranges.")
            continue

        if best["pv_add"] > 0:
            schedule.pv.append(Tranche("pv", ty, pv_degradation_rate, capacity_mw=best["pv_add"]))
        if best.get("wind_add", 0) > 0:
            schedule.wind.append(Tranche("wind", ty, wind_degradation_rate, capacity_mw=best["wind_add"]))
        if best["bess_add_mwh"] > 0:
            schedule.bess.append(Tranche("bess", ty, bess_degradation_rate,
                                          power_mw=best["bess_add_mwh"] * bess_c_rate, energy_mwh=best["bess_add_mwh"]))

        log.append({"year": ty, "status": "OK", **best, "target_pct": target_pct,
                    "required_pct": required_pct, "override": is_override})

    return schedule, log, trials

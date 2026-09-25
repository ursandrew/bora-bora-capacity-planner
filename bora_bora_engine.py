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
):
    """Merit-order dispatch: PV -> Wind -> OTEC -> BESS discharge -> unmet
    (unmet = demand not covered by RE - implicitly the diesel-served
    fraction, since no diesel technology is modeled). Excess RE charges
    BESS up to its limits; anything left over is curtailed.
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
    intermittent_gen = pv_gen + wind_gen
    cap_limit = edt_penetration_cap * demand
    intermittent_to_grid = np.minimum(intermittent_gen, cap_limit)
    intermittent_excess_from_cap = intermittent_gen - intermittent_to_grid

    remaining_demand_after_intermittent = demand - intermittent_to_grid
    otec_to_grid = np.minimum(otec_gen, np.maximum(remaining_demand_after_intermittent, 0))
    otec_excess = otec_gen - otec_to_grid

    total_served_by_gen = intermittent_to_grid + otec_to_grid
    net_load = demand - total_served_by_gen
    total_excess_for_bess = intermittent_excess_from_cap + otec_excess

    soc = initial_soc_mwh if initial_soc_mwh is not None else 0.5 * bess_energy_mwh
    soc = min(soc, bess_energy_mwh)

    unmet = np.zeros(HOURS)
    curtailed_after_bess = np.zeros(HOURS)
    bess_charge = np.zeros(HOURS)
    bess_discharge = np.zeros(HOURS)

    for h in range(HOURS):
        shortfall = net_load[h]
        excess = total_excess_for_bess[h]

        if shortfall > 0:
            max_discharge = min(bess_power_mw, soc * bess_discharge_eff)
            d = min(shortfall, max_discharge)
            soc -= d / bess_discharge_eff if bess_discharge_eff > 0 else d
            bess_discharge[h] = d
            unmet[h] = shortfall - d

        if excess > 0:
            room = bess_energy_mwh - soc
            max_charge = min(bess_power_mw, room / bess_charge_eff if bess_charge_eff > 0 else room)
            c = min(excess, max_charge)
            soc += c * bess_charge_eff
            bess_charge[h] = c
            curtailed_after_bess[h] = excess - c

        soc = max(0.0, min(soc, bess_energy_mwh))

    total_demand = demand.sum()
    total_unmet = unmet.sum()
    total_served = total_demand - total_unmet
    total_curtailment = curtailed_after_bess.sum()
    re_pct = (total_served / total_demand * 100) if total_demand > 0 else 0
    unmet_pct = (total_unmet / total_demand * 100) if total_demand > 0 else 0
    total_re_gen = pv_gen.sum() + wind_gen.sum() + otec_gen.sum()
    curtailment_pct_of_re_gen = (total_curtailment / total_re_gen * 100) if total_re_gen > 0 else 0

    return {
        "year": year,
        "pv_mwp": pv_mwp, "pv_mwac": pv_mwac, "wind_mw": wind_mw,
        "otec_mw": otec_mw, "bess_power_mw": bess_power_mw, "bess_energy_mwh": bess_energy_mwh,
        "total_demand_mwh": total_demand, "total_served_mwh": total_served,
        "total_unmet_mwh": total_unmet, "unmet_pct": unmet_pct,
        "total_curtailment_mwh": total_curtailment,
        "curtailment_pct_of_re_gen": curtailment_pct_of_re_gen,
        "re_pct": re_pct,
        "pv_gen_mwh": pv_gen.sum(), "wind_gen_mwh": wind_gen.sum(), "otec_gen_mwh": otec_gen.sum(),
        "bess_discharge_mwh": bess_discharge.sum(), "bess_charge_mwh": bess_charge.sum(),
        "ending_soc_mwh": soc,
    }


def simulate_trajectory(schedule: TrancheSchedule, years, **sim_kwargs):
    """Runs simulate_year across a range of years, carrying BESS SOC forward."""
    results = []
    soc = None
    for year in years:
        r = simulate_year(year, schedule, initial_soc_mwh=soc, **sim_kwargs)
        soc = r["ending_soc_mwh"]
        results.append(r)
    return results


# ============================================================================
# ECONOMICS - HOMER-style and cash-flow-style NPC/LCOE
# ============================================================================

def crf(rate, lifetime_yr):
    if rate == 0:
        return 1 / lifetime_yr
    return (rate * (1 + rate) ** lifetime_yr) / ((1 + rate) ** lifetime_yr - 1)


def real_discount_rate(nominal, inflation):
    return (nominal - inflation) / (1 + inflation)


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


def _tranche_lifetime(t: Tranche, lifetimes):
    return lifetimes[t.technology]


def compute_lifecycle_economics(
    schedule: TrancheSchedule, traj_df, unit_costs, lifetimes,
    nominal_discount_rate, inflation_rate,
    analysis_start_year, analysis_end_year,
    method,  # "homer" or "cashflow"
):
    """Builds a year-by-year system cash flow from analysis_start_year to
    analysis_end_year (inclusive) across ALL tranches, discounts it, and
    returns total NPC and LCOE.

    method="cashflow": nominal discount rate; O&M escalates with inflation;
      no replacement modeled (CAPEX only at each tranche's own commissioning
      year); no salvage. Matches NPV(costs)/NPV(energy).
    method="homer": real discount rate; O&M held constant in real terms;
      a tranche is replaced (full CAPEX again) every time its own lifetime
      elapses within the horizon; any tranche with remaining useful life at
      analysis_end_year gets a prorated salvage credit.
    """
    rate = (real_discount_rate(nominal_discount_rate, inflation_rate)
            if method == "homer" else nominal_discount_rate)

    energy_by_year = {int(row["year"]): row["total_served_mwh"] for _, row in traj_df.iterrows()}

    years = list(range(analysis_start_year, analysis_end_year + 1))
    npv_cost = 0.0
    npv_energy = 0.0
    cost_by_tech = {"pv": 0.0, "wind": 0.0, "otec": 0.0, "bess": 0.0}

    for year in years:
        t_idx = year - analysis_start_year
        year_cost = 0.0

        for t in schedule.all_tranches():
            if year < t.commissioning_year:
                continue
            capex = _tranche_capex(t, unit_costs)
            om_base = _tranche_om_per_year(t, unit_costs)
            lifetime = _tranche_lifetime(t, lifetimes)
            age = year - t.commissioning_year

            tech_cost = 0.0
            if year == t.commissioning_year:
                tech_cost += capex
            if method == "cashflow":
                tech_cost += om_base * (1 + inflation_rate) ** t_idx
            else:  # homer
                tech_cost += om_base  # constant real terms
                if age > 0 and lifetime > 0 and age % lifetime == 0 and year != t.commissioning_year:
                    tech_cost += capex  # replacement event

            year_cost += tech_cost
            cost_by_tech[t.technology] += tech_cost / (1 + rate) ** t_idx

        # HOMER-style salvage credit at the horizon end
        if method == "homer" and year == analysis_end_year:
            for t in schedule.all_tranches():
                if year < t.commissioning_year:
                    continue
                lifetime = _tranche_lifetime(t, lifetimes)
                age = year - t.commissioning_year
                remaining_frac = 1 - (age % lifetime) / lifetime if lifetime > 0 else 0
                salvage = _tranche_capex(t, unit_costs) * remaining_frac
                year_cost -= salvage
                cost_by_tech[t.technology] -= salvage / (1 + rate) ** t_idx

        discount_factor = (1 + rate) ** t_idx
        npv_cost += year_cost / discount_factor
        npv_energy += energy_by_year.get(year, 0.0) / discount_factor

    lcoe_per_mwh = (npv_cost / npv_energy) if npv_energy > 0 else 0.0

    return {
        "method": method,
        "npv_cost": npv_cost,
        "npv_energy_mwh": npv_energy,
        "lcoe_per_mwh": lcoe_per_mwh,
        "cost_by_technology": cost_by_tech,
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
    verbose=False,
):
    """Greedy sequential sizing: at each tranche year, grid-search the
    minimum-cost (PV, BESS) addition that satisfies the RE floor (target +
    buffer) at that checkpoint year while respecting the curtailment cap,
    given all previously-locked tranches (now degraded to that year).
    """
    schedule = TrancheSchedule()
    for t in (exogenous_tranches or []):
        getattr(schedule, t.technology).append(t)

    log = []

    for ty in tranche_years:
        target_pct = re_target_for_year(ty, re_target_2030, re_target_2050) * 100
        required_pct = target_pct + (target_buffer_pct if ty < 2050 else 0.0)

        best = None
        for pv_add in pv_candidates_mwp:
            for bess_add_mwh in bess_candidates_mwh:
                trial = schedule.clone()
                if pv_add > 0:
                    trial.pv.append(Tranche("pv", ty, pv_degradation_rate, capacity_mw=pv_add))
                if bess_add_mwh > 0:
                    trial.bess.append(Tranche("bess", ty, bess_degradation_rate,
                                               power_mw=bess_add_mwh * bess_c_rate, energy_mwh=bess_add_mwh))

                r = simulate_year(ty, trial, **sim_kwargs)

                feasible = (r["re_pct"] >= required_pct
                            and r["unmet_pct"] <= unmet_load_ceiling_pct
                            and r["curtailment_pct_of_re_gen"] <= curtailment_cap_pct)
                if not feasible:
                    continue

                # simple capital-cost proxy for ranking candidates within one tranche year
                # (full NPC/LCOE across the whole schedule is computed once, separately,
                # after the schedule is locked in - see compute_lifecycle_economics)
                cost_proxy = pv_add * pv_capex_per_mwp + bess_add_mwh * bess_capex_per_mwh
                if best is None or cost_proxy < best["cost_proxy"]:
                    best = {"pv_add": pv_add, "bess_add_mwh": bess_add_mwh, "cost_proxy": cost_proxy,
                            "re_pct": r["re_pct"], "unmet_pct": r["unmet_pct"],
                            "curtailment_pct": r["curtailment_pct_of_re_gen"]}

        if best is None:
            log.append({"year": ty, "status": "INFEASIBLE", "target_pct": target_pct, "required_pct": required_pct})
            if verbose:
                print(f"[{ty}] INFEASIBLE at target {required_pct:.1f}% - widen PV/BESS candidate ranges.")
            continue

        if best["pv_add"] > 0:
            schedule.pv.append(Tranche("pv", ty, pv_degradation_rate, capacity_mw=best["pv_add"]))
        if best["bess_add_mwh"] > 0:
            schedule.bess.append(Tranche("bess", ty, bess_degradation_rate,
                                          power_mw=best["bess_add_mwh"] * bess_c_rate, energy_mwh=best["bess_add_mwh"]))

        log.append({"year": ty, "status": "OK", **best, "target_pct": target_pct, "required_pct": required_pct})

    return schedule, log

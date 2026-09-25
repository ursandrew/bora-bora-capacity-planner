"""
bora_bora_engine.py
====================
Vintage-tracked, multi-tranche, multi-technology (PV / Wind / OTEC / BESS)
capacity-expansion engine for the Bora Bora net-zero model.

This is the piece the Excel workbook cannot do: Excel's Dispatch_2028 /
2030 / 2035 / 2040 / 2050 sheets are independent, non-cumulative snapshots -
each one assumes a single fixed capacity for that year. This engine instead
tracks capacity as a list of TRANCHES per technology, each with its own
commissioning year and its own degradation clock, so it can answer:
"if I build X MWp in 2028, top up with Y MWp in 2035, and OTEC lands in
2032, what does the RE%/curtailment/unmet-load trajectory look like every
year from 2026 to 2050, accounting for each tranche degrading from the
day IT was commissioned (not from year zero)?"

Reuses the HOMER-style NPC/CRF methodology from
optimize_gridsearch_hydro_WITH_DEGRADATION.py (the confirmed-correct
reference backend), adapted to Bora Bora's technology set (PV + OTEC + BESS;
Wind is modeled but excluded by default per Assumptions!B54=0) and to
vintage/tranche tracking instead of a single fixed capacity for 25 years.
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
    # PV/Wind/OTEC:
    capacity_mw: float = 0.0     # PV: MWp (DC); Wind/OTEC: MW
    # BESS:
    power_mw: float = 0.0
    energy_mwh: float = 0.0

    def effective_factor(self, year):
        """Fraction of nameplate/energy capacity still available in `year`."""
        if year < self.commissioning_year:
            return 0.0
        age = year - self.commissioning_year
        return (1 - self.degradation_rate) ** age


@dataclass
class TrancheSchedule:
    """All tranches built up over the project so far, across technologies."""
    pv: list = field(default_factory=list)
    wind: list = field(default_factory=list)
    otec: list = field(default_factory=list)
    bess: list = field(default_factory=list)

    def clone(self):
        return TrancheSchedule(
            pv=list(self.pv), wind=list(self.wind),
            otec=list(self.otec), bess=list(self.bess),
        )

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
    dc_ac_ratio=1.3,
    bess_charge_eff=0.95, bess_discharge_eff=0.95,
    initial_soc_mwh=None,
    edt_penetration_cap=1.0,
    ev_marine_shape="flat",
    baseline_demand_hourly_mw=None,
):
    """Merit-order dispatch: PV -> Wind -> OTEC -> BESS discharge -> unmet.
    Excess after serving load charges BESS up to its limits; anything left
    over is curtailed.

    Returns a dict with hourly arrays and annual summary metrics, plus the
    ending SOC (MWh) so the caller can carry it into the next year.
    """
    pv_mwp = schedule.effective_pv_mwp(year)
    pv_mwac = pv_mwp / dc_ac_ratio if dc_ac_ratio else pv_mwp
    wind_mw = schedule.effective_wind_mw(year)
    otec_mw = schedule.effective_otec_mw(year)
    bess_power_mw = schedule.effective_bess_power_mw(year)
    bess_energy_mwh = schedule.effective_bess_energy_mwh(year)

    pv_cf = np.array(pv_cf_hourly)
    wind_cf = np.array(wind_cf_hourly) if wind_mw > 0 else np.zeros(HOURS)

    pv_gen = pv_mwac * pv_cf
    wind_gen = wind_mw * wind_cf
    otec_gen = np.full(HOURS, otec_mw * data.OTEC_CF)

    # --- demand: baseline shape scaled to this year's forecast, + EV/marine overlay ---
    # Loaded ONCE by the caller (app.py) and passed in - not re-read from disk on every
    # call, since this function runs thousands of times inside a grid search. Falls back
    # to the bundled default only if the caller doesn't supply one (e.g. quick scripts/tests).
    if baseline_demand_hourly_mw is None:
        baseline_demand_hourly_mw = data.load_baseline_demand_shape_mw()
    baseline_shape = np.array(baseline_demand_hourly_mw)  # MW, 2024 shape
    base_annual_mwh = data.BASELINE_ANNUAL_DEMAND_MWH
    # underlying (non-EV/marine) demand for this year:
    ev_marine_mwh = data.get_ev_marine_annual_mwh(year)
    underlying_mwh = data.get_annual_demand_mwh(year) - ev_marine_mwh
    scale = underlying_mwh / base_annual_mwh
    underlying_hourly_mw = baseline_shape * scale

    if ev_marine_shape == "flat":
        ev_marine_hourly_mw = np.full(HOURS, ev_marine_mwh / HOURS)
    else:
        ev_marine_hourly_mw = np.array(ev_marine_shape)  # caller-supplied real profile

    demand = underlying_hourly_mw + ev_marine_hourly_mw

    # --- EDT intermittent penetration cap (solar+wind only, not OTEC) ---
    # The cap limits how much of INSTANTANEOUS DEMAND may be served directly
    # by solar+wind - it is NOT a ceiling on total generation. Intermittent
    # output above that limit isn't lost: it's simply not allowed to inject
    # straight to the grid, so - exactly like any other excess - it goes to
    # charge the BESS first, and is only curtailed if the BESS is already full.
    intermittent_gen = pv_gen + wind_gen
    cap_limit = edt_penetration_cap * demand
    intermittent_to_grid = np.minimum(intermittent_gen, cap_limit)
    intermittent_excess_from_cap = intermittent_gen - intermittent_to_grid

    remaining_demand_after_intermittent = demand - intermittent_to_grid
    otec_to_grid = np.minimum(otec_gen, np.maximum(remaining_demand_after_intermittent, 0))
    otec_excess = otec_gen - otec_to_grid

    total_served_by_gen = intermittent_to_grid + otec_to_grid
    net_load = demand - total_served_by_gen              # >=0 always: remaining shortfall to be met by BESS/unmet
    total_excess_for_bess = intermittent_excess_from_cap + otec_excess  # generation with nowhere to go but BESS/curtailment

    soc = initial_soc_mwh if initial_soc_mwh is not None else 0.5 * bess_energy_mwh
    soc = min(soc, bess_energy_mwh)

    unmet = np.zeros(HOURS)
    curtailed_after_bess = np.zeros(HOURS)
    bess_charge = np.zeros(HOURS)
    bess_discharge = np.zeros(HOURS)
    soc_trace = np.zeros(HOURS)

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
        soc_trace[h] = soc

    total_demand = demand.sum()
    total_unmet = unmet.sum()
    total_served = total_demand - total_unmet
    total_curtailment = curtailed_after_bess.sum()
    re_delivered = total_served  # by construction, everything served is RE (no diesel modeled)
    re_pct = (re_delivered / total_demand * 100) if total_demand > 0 else 0
    unmet_pct = (total_unmet / total_demand * 100) if total_demand > 0 else 0
    curtailment_pct_of_re_gen = (
        total_curtailment / (pv_gen.sum() + wind_gen.sum() + otec_gen.sum()) * 100
        if (pv_gen.sum() + wind_gen.sum() + otec_gen.sum()) > 0 else 0
    )

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


def simulate_trajectory(schedule: TrancheSchedule, years, pv_cf_hourly, wind_cf_hourly, **kwargs):
    """Run simulate_year across a range of years, carrying BESS SOC forward."""
    results = []
    soc = None
    for year in years:
        r = simulate_year(year, schedule, pv_cf_hourly, wind_cf_hourly,
                           initial_soc_mwh=soc, **kwargs)
        soc = r["ending_soc_mwh"]
        results.append(r)
    return results


# ============================================================================
# COST MODEL (HOMER-style NPC, adapted from optimize_gridsearch_hydro_WITH_DEGRADATION.py)
# ============================================================================
# NOTE: the Bora Bora Excel workbook has NO cost/CAPEX/O&M sheet - it is a
# purely technical (RE%/curtailment/unmet-load) model. The defaults below
# are PLACEHOLDERS for PV/BESS (typical Pacific-island-scale figures) except
# OTEC, whose CAPEX range (EUR102-152M for 1.2-2.6MW) is sourced from the
# 2H Offshore Aug-2024 feasibility study and converted to USD/MW here at an
# illustrative rate - replace ALL of these with validated client figures
# before using this for an investment-grade cost comparison.

DEFAULT_COSTS = {
    "pv_capex_per_mwp": 900_000,       # USD/MWp installed (placeholder)
    "pv_om_per_mwp_yr": 12_000,        # USD/MWp/yr (placeholder)
    "pv_lifetime_yr": 25,
    "bess_capex_per_mwh": 350_000,     # USD/MWh (placeholder)
    "bess_om_per_mwh_yr": 7_000,       # USD/MWh/yr (placeholder)
    "bess_lifetime_yr": 15,
    "otec_capex_per_mw": 95_000_000,   # USD/MW - derived from 2H Offshore EUR102-152M / 1.2-2.6MW range midpoint, illustrative FX
    "otec_om_per_mw_yr": 1_500_000,    # USD/MW/yr (placeholder, OTEC O&M is typically high - refine with study data)
    "otec_lifetime_yr": 30,
    "discount_rate_nominal": 0.08,
    "inflation_rate": 0.02,
}


def crf(real_discount_rate, lifetime_yr):
    i = real_discount_rate
    n = lifetime_yr
    if i == 0:
        return 1 / n
    return (i * (1 + i) ** n) / ((1 + i) ** n - 1)


def real_discount_rate(nominal, inflation):
    return (nominal - inflation) / (1 + inflation)


def tranche_incremental_npc(pv_add_mwp, bess_add_mw, bess_add_mwh, costs=DEFAULT_COSTS):
    """Simple NPC (capital + PV-of-O&M, no replacement/salvage) for ONE new
    tranche, used to compare candidate tranche sizes against each other
    during sizing at a single checkpoint year. Real discount rate applied
    to O&M for the shorter of (technology lifetime, remaining project years)
    - simplified here to the technology's own lifetime for tranche-vs-tranche
    comparability."""
    rdr = real_discount_rate(costs["discount_rate_nominal"], costs["inflation_rate"])

    def pv_of_om(annual_om, lifetime):
        if rdr == 0:
            return annual_om * lifetime
        return annual_om * (1 - (1 + rdr) ** -lifetime) / rdr

    pv_capex = pv_add_mwp * costs["pv_capex_per_mwp"]
    pv_om_npc = pv_of_om(pv_add_mwp * costs["pv_om_per_mwp_yr"], costs["pv_lifetime_yr"])

    bess_capex = bess_add_mwh * costs["bess_capex_per_mwh"]
    bess_om_npc = pv_of_om(bess_add_mwh * costs["bess_om_per_mwh_yr"], costs["bess_lifetime_yr"])

    return {
        "pv_npc": pv_capex + pv_om_npc,
        "bess_npc": bess_capex + bess_om_npc,
        "total_npc": pv_capex + pv_om_npc + bess_capex + bess_om_npc,
    }


def otec_tranche_npc(otec_mw, costs=DEFAULT_COSTS):
    rdr = real_discount_rate(costs["discount_rate_nominal"], costs["inflation_rate"])

    def pv_of_om(annual_om, lifetime):
        if rdr == 0:
            return annual_om * lifetime
        return annual_om * (1 - (1 + rdr) ** -lifetime) / rdr

    capex = otec_mw * costs["otec_capex_per_mw"]
    om_npc = pv_of_om(otec_mw * costs["otec_om_per_mw_yr"], costs["otec_lifetime_yr"])
    return capex + om_npc


# ============================================================================
# SEQUENTIAL TRANCHE SIZING
# ============================================================================

def re_target_for_year(year):
    """Straight-line interpolation between the two LEGAL checkpoints
    (75% @ 2030, 100% @ 2050). Used only as the glide-path REFERENCE line for
    computing buffers - the actual constraint enforced at each tranche
    checkpoint is 'checkpoints-only, bounded dip', see size_tranche_schedule()."""
    if year <= 2030:
        # linear ramp from an assumed ~0% in 2024 baseline to 75% by 2030 (illustrative)
        return data.RE_TARGET_2030 * min(1.0, (year - 2024) / (2030 - 2024)) if year > 2024 else 0.0
    if year >= 2050:
        return data.RE_TARGET_2050
    frac = (year - 2030) / (2050 - 2030)
    return data.RE_TARGET_2030 + frac * (data.RE_TARGET_2050 - data.RE_TARGET_2030)


def size_tranche_schedule(
    tranche_years=(2028, 2030, 2035, 2040, 2050),
    otec_tranche=None,               # a Tranche for OTEC, added at its own commissioning year automatically
    pv_candidates_mwp=None,          # iterable of candidate ADDITIONAL MWp to try at each tranche year
    bess_candidates_mwh=None,        # iterable of candidate ADDITIONAL MWh to try (power sized at 0.5C by default)
    bess_c_rate=0.5,
    curtailment_cap_pct=10.0,        # max curtailment as % of annual PV+wind+OTEC generation
    unmet_load_ceiling_pct=100.0,    # NOTE: in this model (no diesel technology tracked), "unmet load %"
                                      # IS mathematically (100 - RE%) - the demand not met by RE is
                                      # implicitly covered by EDT's diesel backbone, not a blackout.
                                      # So this is NOT an independent reliability constraint here - it's
                                      # redundant with the RE% target and defaults to unconstrained (100%).
                                      # Only tighten this once diesel dispatch/true blackout risk is modeled
                                      # explicitly; until then, use the RE% target to control this instead.
    target_buffer_pct=3.0,           # extra RE% margin required at commissioning, to absorb degradation dip before next tranche
    pv_cf_hourly=None, wind_cf_hourly=None,
    baseline_demand_hourly_mw=None,  # load once by the caller (e.g. from an uploaded CSV) and pass in here
    costs=DEFAULT_COSTS,
    verbose=True,
):
    """Greedy sequential sizing: at each tranche year (in order), grid-search
    the minimum-NPC (PV, BESS) addition that satisfies the RE floor (target +
    buffer) at that checkpoint year while respecting the curtailment cap and
    unmet-load ceiling, given all previously-locked tranches (now degraded to
    that year). OTEC is treated as exogenous (fixed capacity/timing decision
    already made) and inserted into the schedule at its own commissioning year.
    """
    if pv_candidates_mwp is None:
        pv_candidates_mwp = list(range(0, 41, 2))     # 0..40 MWp in 2 MWp steps
    if bess_candidates_mwh is None:
        bess_candidates_mwh = list(range(0, 201, 20))  # 0..200 MWh in 20 MWh steps

    schedule = TrancheSchedule()
    if otec_tranche is not None:
        schedule.otec.append(otec_tranche)

    log = []

    for ty in tranche_years:
        target_pct = re_target_for_year(ty) * 100
        required_pct = target_pct + (target_buffer_pct if ty < 2050 else 0.0)  # no buffer needed at final checkpoint

        best = None
        for pv_add in pv_candidates_mwp:
            for bess_add_mwh in bess_candidates_mwh:
                trial = schedule.clone()
                if pv_add > 0:
                    trial.pv.append(Tranche("pv", ty, data.PV_DEGRADATION_RATE, capacity_mw=pv_add))
                if bess_add_mwh > 0:
                    trial.bess.append(Tranche(
                        "bess", ty, data.BESS_DEGRADATION_RATE,
                        power_mw=bess_add_mwh * bess_c_rate, energy_mwh=bess_add_mwh,
                    ))

                r = simulate_year(ty, trial, pv_cf_hourly, wind_cf_hourly,
                                   edt_penetration_cap=data.EDT_PENETRATION_CAP,
                                   baseline_demand_hourly_mw=baseline_demand_hourly_mw)

                feasible = (
                    r["re_pct"] >= required_pct
                    and r["unmet_pct"] <= unmet_load_ceiling_pct
                    and r["curtailment_pct_of_re_gen"] <= curtailment_cap_pct
                )
                if not feasible:
                    continue

                incr_npc = tranche_incremental_npc(pv_add, bess_add_mwh * bess_c_rate, bess_add_mwh, costs)["total_npc"]
                if best is None or incr_npc < best["npc"]:
                    best = {"pv_add": pv_add, "bess_add_mwh": bess_add_mwh, "npc": incr_npc,
                            "re_pct": r["re_pct"], "unmet_pct": r["unmet_pct"],
                            "curtailment_pct": r["curtailment_pct_of_re_gen"]}

        if best is None:
            log.append({"year": ty, "status": "INFEASIBLE within candidate grid - widen pv/bess candidate ranges"})
            if verbose:
                print(f"[{ty}] INFEASIBLE - no candidate combination met target {required_pct:.1f}% "
                      f"within curtailment/unmet constraints. Widen search grid.")
            continue

        if best["pv_add"] > 0:
            schedule.pv.append(Tranche("pv", ty, data.PV_DEGRADATION_RATE, capacity_mw=best["pv_add"]))
        if best["bess_add_mwh"] > 0:
            schedule.bess.append(Tranche(
                "bess", ty, data.BESS_DEGRADATION_RATE,
                power_mw=best["bess_add_mwh"] * bess_c_rate, energy_mwh=best["bess_add_mwh"],
            ))

        log.append({"year": ty, "status": "OK", **best, "target_pct": target_pct, "required_pct": required_pct})
        if verbose:
            print(f"[{ty}] target {target_pct:.0f}% (+{target_buffer_pct if ty<2050 else 0:.0f}% buffer) -> "
                  f"add PV +{best['pv_add']} MWp, BESS +{best['bess_add_mwh']} MWh "
                  f"=> RE {best['re_pct']:.1f}%, unmet {best['unmet_pct']:.2f}%, "
                  f"curtailment {best['curtailment_pct']:.1f}% of RE gen, incr. NPC ${best['npc']/1e6:.2f}M")

    return schedule, log

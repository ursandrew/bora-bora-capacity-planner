"""
bora_bora_engine.py
====================
Vintage-tracked, multi-tranche, multi-technology (PV / Wind / OTEC / BESS)
capacity-expansion engine. Every technical and economic assumption is a
function parameter with a default value - nothing is hardcoded that the
app's UI can't override.
"""

from dataclasses import dataclass, field
from typing import Optional
import numpy as np

import bora_bora_data as data

HOURS = data.HOURS_PER_YEAR
RE_TOL_PP = 1e-6   # percentage-point tolerance when comparing RE% to a target (float noise at 100%)


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
    # Service life in years. None / 0 -> no end-of-life modelled (asset runs to the horizon end, no
    # replacement cost). Otherwise the asset is IN SERVICE from the commissioning year through
    # (commissioning year + lifetime_years) INCLUSIVE - e.g. BESS commissioned 2028 with a 20-year
    # life is in service through 2048 - and a like-for-like replacement is commissioned the year
    # after (2049): capacity back to nameplate, degradation clock reset, CAPEX booked again.
    lifetime_years: Optional[int] = None

    def _period(self):
        return int(self.lifetime_years) + 1 if self.lifetime_years else None

    def effective_factor(self, year):
        if year < self.commissioning_year:
            return 0.0
        elapsed = year - self.commissioning_year
        period = self._period()
        if period:
            elapsed = elapsed % period      # age within the current life cycle
        return (1 - self.degradation_rate) ** elapsed

    def is_build_year(self, year):
        """True in the commissioning year and in every replacement year after it."""
        if year < self.commissioning_year:
            return False
        elapsed = year - self.commissioning_year
        period = self._period()
        return elapsed == 0 or (period is not None and elapsed % period == 0)

    def build_years(self, end_year):
        years = [self.commissioning_year]
        period = self._period()
        if period:
            y = self.commissioning_year + period
            while y <= end_year:
                years.append(y)
                y += period
        return [y for y in years if y <= end_year]


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

    def newly_commissioned_bess_mwh(self, year):
        """Nameplate BESS energy that is commissioned (new build or replacement) in `year`."""
        return sum(t.energy_mwh for t in self.bess if t.is_build_year(year))


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
    underlying_growth_beyond_last=0.0,
):
    """Merit-order dispatch: PV -> Wind -> OTEC -> BESS discharge -> unmet
    (unmet = demand not covered by RE - implicitly the diesel-served
    fraction, since no diesel technology is modeled). Excess RE charges
    BESS up to its limits; anything left over is curtailed.

    BESS operating rule (strictly ONE direction per hour - it never charges
    and discharges in the same hour, at any EDT cap): if there is surplus
    energy AND room in the battery, the battery charges and any remaining
    shortfall that hour goes to residual diesel; the battery discharges only in
    hours where it is not charging. (At a 100% EDT cap a surplus hour can never
    also have a shortfall, so the rule only bites when the cap is below 100%.)

    Demand: the baseline hourly shape is normalised by its OWN sum and then
    multiplied by the year's annual-table MWh, so the annual table always sets
    the total regardless of how the shape file was scaled. Beyond the last year
    in the table, demand grows at `underlying_growth_beyond_last` (default 0 =
    held flat).

    Rooftop (if enabled) contributes ONLY its exported-to-grid share
    (1 - rooftop_self_consumption_pct) of its generation, using the same
    hourly CF shape as pv_cf_hourly - the self-consumed share is already
    netted out of `underlying_annual_table` upstream (Forecast_Annual's
    "net of rooftop solar" row), so adding it again here would double-count
    it. This mirrors the workbook's own Row 12/14 "exported to grid" split.
    Because rooftop is privately owned (not project CAPEX), the result also
    reports `rooftop_served_mwh` / `project_served_mwh` - the energy actually
    delivered to load attributable to rooftop (direct, plus its pro-rata share
    of BESS-stored energy) and the remainder - and the LCOE denominator uses
    the project-served figure only.

    `return_hourly=True` adds the full 8,760-hour arrays to the returned dict
    under an "hourly" key - off by default since the sizing search calls this
    thousands of times and only needs the annual totals.

    BESS SOC: the fleet is tracked as one pooled scalar (not per-tranche).
    `initial_soc_mwh` is the pooled charge carried in from the previous year.
    Whatever BESS energy is COMMISSIONED this year (new tranche or a
    replacement) is topped up to bess_initial_soc_frac of its nameplate and
    added to that carried charge; capacity already online keeps the charge it
    had. Every hour, charge/discharge keeps SOC inside
    [bess_min_soc_frac, bess_max_soc_frac] of that year's total capacity.
    """
    pv_mwp = schedule.effective_pv_mwp(year)
    pv_mwac = pv_mwp / dc_ac_ratio if dc_ac_ratio else pv_mwp
    wind_mw = schedule.effective_wind_mw(year)
    otec_mw = schedule.effective_otec_mw(year)
    bess_power_mw = schedule.effective_bess_power_mw(year)
    bess_energy_mwh = schedule.effective_bess_energy_mwh(year)

    pv_cf = np.asarray(pv_cf_hourly, dtype=float)
    wind_cf = np.asarray(wind_cf_hourly, dtype=float) if (wind_mw > 0 and wind_cf_hourly is not None) else np.zeros(HOURS)

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
    baseline_shape = np.asarray(baseline_demand_hourly_mw, dtype=float)
    shape_sum = baseline_shape.sum()
    underlying_mwh = data.lookup_annual(underlying_annual_table, year, underlying_growth_beyond_last)
    scale = underlying_mwh / shape_sum if shape_sum > 0 else 0.0
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

    if initial_soc_mwh is not None:
        # continuing from a previous simulated year: keep the carried-over SOC, and top up ONLY the
        # BESS energy commissioned this year (new tranche or replacement) to the initial-SOC fraction
        soc = initial_soc_mwh + bess_initial_soc_frac * schedule.newly_commissioned_bess_mwh(year)
    else:
        # first simulated year: the whole effective fleet starts at the initial-SOC fraction
        soc = bess_initial_soc_frac * bess_energy_mwh
    soc = max(min_soc_mwh, min(soc, max_soc_mwh))
    soc_start = soc

    net_load_l = net_load.tolist()
    excess_l = total_excess_for_bess.tolist()
    unmet_l = [0.0] * HOURS
    curt_l = [0.0] * HOURS
    charge_l = [0.0] * HOURS             # AC-side power drawn to charge, BEFORE charging-efficiency loss
    charge_after_l = [0.0] * HOURS       # energy added to stored SOC, AFTER charging-efficiency loss
    discharge_l = [0.0] * HOURS          # AC-side power delivered to grid, AFTER discharge-efficiency loss
    discharge_before_l = [0.0] * HOURS   # energy drawn out of stored SOC, BEFORE discharge-efficiency loss
    soc_l = [0.0] * HOURS

    eff_c = bess_charge_eff
    eff_d = bess_discharge_eff

    for h in range(HOURS):
        shortfall = net_load_l[h]
        excess = excess_l[h]
        c = 0.0

        # 1) Charge from surplus if there is surplus AND room.
        if excess > 0:
            room = max_soc_mwh - soc
            if room < 0.0:
                room = 0.0
            max_charge = min(bess_power_mw, room / eff_c if eff_c > 0 else room)
            c = excess if excess < max_charge else max_charge
            if c > 0:
                soc += c * eff_c
                charge_l[h] = c
                charge_after_l[h] = c * eff_c
            curt_l[h] = excess - c

        # 2) Shortfall: discharge ONLY in an hour in which the battery is not charging.
        if shortfall > 0:
            if c > 0:
                unmet_l[h] = shortfall      # battery busy charging -> residual diesel
            else:
                avail = soc - min_soc_mwh
                if avail < 0.0:
                    avail = 0.0
                max_discharge = min(bess_power_mw, avail * eff_d)
                d = shortfall if shortfall < max_discharge else max_discharge
                d_from_soc = d / eff_d if eff_d > 0 else d
                soc -= d_from_soc
                discharge_l[h] = d
                discharge_before_l[h] = d_from_soc
                unmet_l[h] = shortfall - d

        if soc < min_soc_mwh:
            soc = min_soc_mwh
        elif soc > max_soc_mwh:
            soc = max_soc_mwh
        soc_l[h] = soc

    unmet = np.array(unmet_l)
    curtailed_after_bess = np.array(curt_l)
    bess_charge = np.array(charge_l)
    bess_charge_after_eff = np.array(charge_after_l)
    bess_discharge = np.array(discharge_l)
    bess_discharge_before_eff = np.array(discharge_before_l)
    soc_hourly = np.array(soc_l)

    total_demand = demand.sum()
    total_unmet = unmet.sum()
    total_served = total_demand - total_unmet
    total_curtailment = curtailed_after_bess.sum()
    re_pct = (total_served / total_demand * 100) if total_demand > 0 else 0
    unmet_pct = (total_unmet / total_demand * 100) if total_demand > 0 else 0
    total_re_gen = pv_gen.sum() + wind_gen.sum() + otec_gen.sum() + rooftop_gen_exported.sum()
    curtailment_pct_of_re_gen = (total_curtailment / total_re_gen * 100) if total_re_gen > 0 else 0

    # --- Rooftop's share of the energy actually delivered to load (pro-rata attribution) ---
    # Direct: its share of intermittent_to_grid each hour. Via BESS: its share of the surplus that
    # charged the battery, times the battery's realised discharge/charge throughput (capped at 1).
    rooftop_served = 0.0
    if rooftop_enabled and rooftop_gen_exported.sum() > 0:
        with np.errstate(divide="ignore", invalid="ignore"):
            roof_frac = np.where(intermittent_gen > 0, rooftop_gen_exported / intermittent_gen, 0.0)
            excess_share = np.where(total_excess_for_bess > 0,
                                    intermittent_excess_from_cap * roof_frac / total_excess_for_bess, 0.0)
        rooftop_direct = float((intermittent_to_grid * roof_frac).sum())
        rooftop_to_bess = float((bess_charge * excess_share).sum())
        ch_sum = bess_charge.sum()
        throughput = min(1.0, bess_discharge.sum() / ch_sum) if ch_sum > 0 else 0.0
        rooftop_served = rooftop_direct + rooftop_to_bess * throughput
    project_served = total_served - rooftop_served

    # --- Integrity checks (cheap, vectorised) ---
    simultaneous_hours = int(((bess_charge > 0) & (bess_discharge > 0)).sum())
    balance_check = demand - (
        pv_gen + wind_gen + otec_gen + rooftop_gen_exported
        - bess_charge + bess_discharge - curtailed_after_bess + unmet
    )
    soc_balance_error = soc - soc_start - (bess_charge_after_eff.sum() - bess_discharge_before_eff.sum())

    result = {
        "year": year,
        "pv_mwp": pv_mwp, "pv_mwac": pv_mwac, "wind_mw": wind_mw,
        "otec_mw": otec_mw, "bess_power_mw": bess_power_mw, "bess_energy_mwh": bess_energy_mwh,
        "rooftop_mwp": rooftop_mwp,
        "total_demand_mwh": total_demand, "total_served_mwh": total_served,
        "rooftop_served_mwh": rooftop_served, "project_served_mwh": project_served,
        "total_unmet_mwh": total_unmet, "unmet_pct": unmet_pct,
        "total_curtailment_mwh": total_curtailment,
        "curtailment_pct_of_re_gen": curtailment_pct_of_re_gen,
        "re_pct": re_pct,
        "pv_gen_mwh": pv_gen.sum(), "wind_gen_mwh": wind_gen.sum(), "otec_gen_mwh": otec_gen.sum(),
        "rooftop_gen_exported_mwh": rooftop_gen_exported.sum(),
        "bess_discharge_mwh": bess_discharge.sum(), "bess_charge_mwh": bess_charge.sum(),
        "ending_soc_mwh": soc,
        "bess_simultaneous_hours": simultaneous_hours,
        "max_energy_balance_error_mw": float(np.abs(balance_check).max()),
        "soc_balance_error_mwh": float(soc_balance_error),
    }

    if return_hourly:
        soc_pct = (soc_hourly / bess_energy_mwh * 100) if bess_energy_mwh > 0 else np.zeros(HOURS)
        deficit_before_bess = np.maximum(net_load, 0.0)
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
            "simultaneous_flag": ((bess_charge > 0) & (bess_discharge > 0)).astype(int),
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
                "Extra demand: EV+Marine+GV (MW)": h["extra_demand_mw"][i],
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
                "Simultaneous charge+discharge (1=yes)": h["simultaneous_flag"][i],
            })
        by_year[year] = rows
    return by_year


# ============================================================================
# ECONOMICS - single cash-flow NPC / LCOE method
# ============================================================================

def crf(rate, lifetime_yr):
    if rate == 0:
        return 1 / lifetime_yr
    return (rate * (1 + rate) ** lifetime_yr) / ((1 + rate) ** lifetime_yr - 1)


def _tranche_capex(t: Tranche, unit_costs):
    """Nameplate CAPEX of one build of this tranche, in analysis-start-year dollars."""
    if t.technology == "pv":
        return t.capacity_mw * unit_costs["pv_capex_per_mwp"]
    if t.technology == "wind":
        return t.capacity_mw * unit_costs["wind_capex_per_mw"]
    if t.technology == "otec":
        # fixed cost per OTEC tranche (platform / cold-water pipe) + variable per MW
        return unit_costs.get("otec_capex_fixed", 0.0) + t.capacity_mw * unit_costs["otec_capex_per_mw"]
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


def tranche_cashflows(t: Tranche, unit_costs, inflation_rate, start_year, end_year, escalate_capex=True):
    """Nominal cash flows of one tranche from max(commissioning, start_year) to end_year, as a list of
    (year, capex, om). Unit costs are taken to be in `start_year` dollars:
      - CAPEX is booked in the commissioning year and again in every replacement year within the
        horizon (see Tranche.lifetime_years), escalated by (1+inflation)^(year-start_year) when
        escalate_capex is True, otherwise held at the flat start-year price;
      - O&M is booked every year from commissioning, escalated by inflation from start_year.
    No salvage credit at the horizon end."""
    base_capex = _tranche_capex(t, unit_costs)
    base_om = _tranche_om_per_year(t, unit_costs)
    build_years = set(t.build_years(end_year))
    flows = []
    for year in range(max(t.commissioning_year, start_year), end_year + 1):
        idx = year - start_year
        capex = 0.0
        if year in build_years:
            capex = base_capex * ((1 + inflation_rate) ** idx if escalate_capex else 1.0)
        om = base_om * (1 + inflation_rate) ** idx
        flows.append((year, capex, om))
    return flows


def tranche_npv(t: Tranche, unit_costs, nominal_discount_rate, inflation_rate, start_year, end_year,
                escalate_capex=True):
    """Lifecycle NPV of cost (CAPEX + replacements + O&M) of one tranche, discounted to start_year."""
    total = 0.0
    for year, capex, om in tranche_cashflows(t, unit_costs, inflation_rate, start_year, end_year, escalate_capex):
        total += (capex + om) / (1 + nominal_discount_rate) ** (year - start_year)
    return total


def compute_lifecycle_economics(
    schedule: TrancheSchedule, traj_df, unit_costs,
    nominal_discount_rate, inflation_rate,
    analysis_start_year, analysis_end_year,
    escalate_capex=True,
):
    """Builds a year-by-year system cash flow from analysis_start_year to
    analysis_end_year (inclusive) across ALL tranches, discounts it at the
    nominal discount rate, and returns total NPC and LCOE.

    CAPEX is booked in each tranche's commissioning year and again in each
    replacement year inside the horizon (end of life + 1); unit costs are
    start-year dollars and, when escalate_capex is True, CAPEX is escalated at
    the inflation rate to the year it is spent (O&M always is); no salvage credit
    at the horizon end. LCOE = NPV(costs) / NPV(energy), both discounted at the
    nominal rate to the start year (year 0 = start year, factor 1.0).

    The energy denominator is PROJECT-SERVED energy (`project_served_mwh`):
    RE energy delivered to load excluding the rooftop-exported share, because
    rooftop is privately owned and its cost is not in the numerator.

    Returns a dict that also carries `cashflow_rows` - one row per year with CAPEX and O&M
    by technology, discount factor and PVs - so the figures can be tied to a spreadsheet.
    """
    rate = nominal_discount_rate
    energy_col = "project_served_mwh" if "project_served_mwh" in traj_df.columns else "total_served_mwh"
    energy_by_year = {int(y): e for y, e in zip(traj_df["year"], traj_df[energy_col])}

    years = list(range(analysis_start_year, analysis_end_year + 1))
    techs = ["pv", "wind", "otec", "bess"]
    capex_by_year = {y: {k: 0.0 for k in techs} for y in years}
    om_by_year = {y: {k: 0.0 for k in techs} for y in years}

    for t in schedule.all_tranches():
        for year, capex, om in tranche_cashflows(t, unit_costs, inflation_rate, analysis_start_year,
                                                  analysis_end_year, escalate_capex):
            capex_by_year[year][t.technology] += capex
            om_by_year[year][t.technology] += om

    npv_cost = 0.0
    npv_energy = 0.0
    npv_capex = 0.0
    cost_by_tech = {k: 0.0 for k in techs}
    cashflow_rows = []

    for year in years:
        df_ = (1 + rate) ** (year - analysis_start_year)
        year_capex = sum(capex_by_year[year].values())
        year_om = sum(om_by_year[year].values())
        year_cost = year_capex + year_om
        energy = energy_by_year.get(year, 0.0)

        npv_cost += year_cost / df_
        npv_capex += year_capex / df_
        npv_energy += energy / df_
        for k in techs:
            cost_by_tech[k] += (capex_by_year[year][k] + om_by_year[year][k]) / df_

        row = {"Year": year, "Discount factor": df_}
        for k in techs:
            row[f"CAPEX {k.upper()} ($)"] = capex_by_year[year][k]
        for k in techs:
            row[f"O&M {k.upper()} ($)"] = om_by_year[year][k]
        row.update({"Total cost ($)": year_cost, "PV of cost ($)": year_cost / df_,
                    "Project-served energy (MWh)": energy, "PV of energy (MWh)": energy / df_})
        cashflow_rows.append(row)

    lcoe_per_mwh = (npv_cost / npv_energy) if npv_energy > 0 else 0.0
    # Per-technology LCOE contribution: each tech's own discounted lifecycle
    # cost (CAPEX + replacements + discounted O&M) divided by the SYSTEM's total
    # discounted energy, so the contributions are additive and sum to lcoe_per_mwh.
    lcoe_contribution_by_technology = {
        tech: (cost / npv_energy if npv_energy > 0 else 0.0)
        for tech, cost in cost_by_tech.items()
    }

    return {
        "npv_cost": npv_cost,
        "npv_capex": npv_capex,
        "npv_om": npv_cost - npv_capex,
        "npv_energy_mwh": npv_energy,
        "lcoe_per_mwh": lcoe_per_mwh,
        "cost_by_technology": cost_by_tech,
        "lcoe_contribution_by_technology": lcoe_contribution_by_technology,
        "cashflow_rows": cashflow_rows,
    }


# ============================================================================
# COMPLIANCE
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


def compliance_table(traj_df, re_target_2030, re_target_2050, tol_pp=RE_TOL_PP):
    """Per-year legal-compliance view of a simulated trajectory.

    The law fixes two milestones - 75% by 2030 and 100% by 2050. This table
    reports, for every simulated year:
      - legal_floor_pct: 0 before 2030; the 2030 target from 2030 up to 2049 (i.e. the
        2030 milestone must not be lost again once reached); the 2050 target in 2050.
        The 'held until 2050' reading of the interim years is a conservative design
        assumption, not wording from the law;
      - meets_legal_floor: RE% >= that floor (tolerance `tol_pp`);
      - glide_ref_pct / meets_glide_ref: INFORMATIONAL straight-line reference between the
        milestones (not a legal requirement)."""
    rows = []
    for _, r in traj_df.iterrows():
        y = int(r["year"])
        re_pct = float(r["re_pct"])
        if y < 2030:
            floor = 0.0
        elif y < 2050:
            floor = re_target_2030 * 100
        else:
            floor = re_target_2050 * 100
        glide = re_target_for_year(y, re_target_2030, re_target_2050) * 100
        rows.append({
            "year": y, "re_pct": re_pct,
            "legal_floor_pct": floor, "meets_legal_floor": re_pct >= floor - tol_pp,
            "margin_to_floor_pp": re_pct - floor,
            "glide_ref_pct": glide, "meets_glide_ref": re_pct >= glide - tol_pp,
        })
    return rows


# ============================================================================
# SEQUENTIAL TRANCHE SIZING
# ============================================================================

def size_tranche_schedule(
    tranche_years,
    pv_degradation_rate, bess_degradation_rate,
    pv_candidates_mwp, bess_candidates_mwh, bess_c_rate,
    curtailment_cap_pct, target_buffer_pct,
    re_target_2030, re_target_2050,
    sim_kwargs,           # dict of everything simulate_year needs besides `year`/`schedule`/`initial_soc_mwh`
    unit_costs,           # per-technology CAPEX / O&M (see _tranche_capex / _tranche_om_per_year)
    econ,                 # dict: nominal_discount_rate, inflation_rate, analysis_end_year, escalate_capex
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
    lifetimes=None,          # {'pv': yrs, 'wind': yrs, 'bess': yrs} service lives for NEW tranches
                              # (missing / None -> no end-of-life replacement modelled for that tech)
    analysis_start_year=2026,
    record_trials=True,      # when True (default), every candidate combination tried - feasible or
                              # not - is recorded and returned as a third value `trials`, for full
                              # grid-search transparency (see docstring). Set False to skip this and
                              # save a little memory/time on very large grids.
    verbose=False,
):
    """Greedy sequential sizing: tranche years are processed in order. At each
    one, grid-search the lowest-LIFECYCLE-COST (PV, BESS, and - if
    wind_candidates_mw is given - Wind) addition that satisfies the RE floor
    (target + buffer, or an explicit override) at that checkpoint year while
    respecting the curtailment cap, given all previously-locked tranches.

    Ranking metric = the candidate's own NPC: its CAPEX (escalated to the
    tranche year if econ['escalate_capex']), any replacement CAPEX inside the
    horizon, and its inflation-escalated O&M through the horizon end, all
    discounted to the analysis start year. This is NOT the same as ranking by
    day-one CAPEX: it prices O&M, timing and replacements. It is still a
    sequential (greedy) search - each tranche year is optimised given the
    earlier ones, not jointly across all years.

    BESS state of charge in every trial is the SOC the already-locked fleet would
    actually carry into that year (the earlier years are simulated forward), plus
    the nameplate of the NEW tranche starting at the initial-SOC fraction -
    exactly what the final trajectory does, so the trial RE% at a tranche year
    equals the trajectory RE% for that year.

    Returns (schedule, log, trials):
      - schedule: the final TrancheSchedule with only the winning addition
        locked in at each tranche year.
      - log: one entry per tranche year (status, winning combo, target used).
      - trials: (when record_trials=True) one row per (tranche_year, PV, BESS,
        Wind) combination the search evaluated - the full grid, not just the
        winner - each with its RE%/unmet%/curtailment%, this candidate's own
        CAPEX/OPEX/NPC/LCOE-proxy, three boolean constraint columns, the
        combined `feasible` and whether it's the one actually `selected`.
    """
    lifetimes = lifetimes or {}
    rate = econ["nominal_discount_rate"]
    infl = econ["inflation_rate"]
    end_year = int(econ["analysis_end_year"])
    escalate = econ.get("escalate_capex", True)

    schedule = TrancheSchedule()
    for t in (exogenous_tranches or []):
        getattr(schedule, t.technology).append(t)

    log = []
    trials = []
    target_overrides = target_overrides or {}
    wind_candidates_mw = wind_candidates_mw or []
    iteration = 0

    for ty in sorted(set(int(y) for y in tranche_years)):
        if ty in target_overrides:
            required_pct = target_overrides[ty]
            target_pct = required_pct
            is_override = True
        else:
            target_pct = re_target_for_year(ty, re_target_2030, re_target_2050) * 100
            required_pct = target_pct + (target_buffer_pct if ty < 2050 else 0.0)
            is_override = False

        # --- SOC the locked-in fleet carries into this tranche year (earlier years simulated forward) ---
        soc_carry = None
        for y in range(analysis_start_year, ty):
            rr = simulate_year(y, schedule, initial_soc_mwh=soc_carry, **sim_kwargs)
            soc_carry = rr["ending_soc_mwh"]

        # --- lifecycle NPC per unit of each technology, built at this tranche year ---
        def _unit_npc(tech):
            if tech == "pv":
                u = Tranche("pv", ty, pv_degradation_rate, capacity_mw=1.0, lifetime_years=lifetimes.get("pv"))
            elif tech == "wind":
                u = Tranche("wind", ty, wind_degradation_rate, capacity_mw=1.0, lifetime_years=lifetimes.get("wind"))
            else:
                u = Tranche("bess", ty, bess_degradation_rate, power_mw=bess_c_rate, energy_mwh=1.0,
                            lifetime_years=lifetimes.get("bess"))
            return tranche_npv(u, unit_costs, rate, infl, analysis_start_year, end_year, escalate)

        pv_unit_npc, bess_unit_npc, wind_unit_npc = _unit_npc("pv"), _unit_npc("bess"), _unit_npc("wind")
        esc_ty = (1 + infl) ** (ty - analysis_start_year)
        capex_esc = esc_ty if escalate else 1.0
        years_remaining = max(1, end_year - ty + 1)
        annuity = crf(rate, years_remaining)

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
                        trial.pv.append(Tranche("pv", ty, pv_degradation_rate, capacity_mw=pv_add,
                                                 lifetime_years=lifetimes.get("pv")))
                    if bess_add_mwh > 0:
                        trial.bess.append(Tranche("bess", ty, bess_degradation_rate,
                                                   power_mw=bess_add_mwh * bess_c_rate, energy_mwh=bess_add_mwh,
                                                   lifetime_years=lifetimes.get("bess")))
                    if wind_add > 0:
                        trial.wind.append(Tranche("wind", ty, wind_degradation_rate, capacity_mw=wind_add,
                                                   lifetime_years=lifetimes.get("wind")))

                    r = simulate_year(ty, trial, initial_soc_mwh=soc_carry, **sim_kwargs)

                    re_target_met = r["re_pct"] >= required_pct - RE_TOL_PP
                    unmet_ceiling_met = r["unmet_pct"] <= unmet_load_ceiling_pct
                    curtailment_cap_met = r["curtailment_pct_of_re_gen"] <= curtailment_cap_pct
                    feasible = re_target_met and unmet_ceiling_met and curtailment_cap_met

                    # Candidate economics, all in tranche-year dollars except NPC (discounted to start year)
                    npc_add = pv_add * pv_unit_npc + bess_add_mwh * bess_unit_npc + wind_add * wind_unit_npc
                    capex_add = (pv_add * unit_costs["pv_capex_per_mwp"] + bess_add_mwh * unit_costs["bess_capex_per_mwh"]
                                 + wind_add * unit_costs["wind_capex_per_mw"]) * capex_esc
                    opex_add = (pv_add * unit_costs["pv_om_per_mwp_yr"] + bess_add_mwh * unit_costs["bess_om_per_mwh_yr"]
                                + wind_add * unit_costs["wind_om_per_mw_yr"]) * esc_ty
                    # LCOE PROXY: this candidate's NPC spread over the remaining horizon (capital-recovery
                    # factor) divided by THIS YEAR's project-served energy. NOT the whole-system lifecycle
                    # LCOE (computed once, separately, by compute_lifecycle_economics); it exists only to
                    # compare candidates against each other on a consistent $/MWh basis.
                    served = r["project_served_mwh"]
                    lcoe_proxy = (npc_add * annuity / served) if served > 0 else 0.0

                    if record_trials:
                        iteration += 1
                        trials.append({
                            "iteration": iteration, "tranche_year": ty,
                            "pv_add_mwp": pv_add, "bess_add_mwh": bess_add_mwh, "wind_add_mw": wind_add,
                            "capex_add": capex_add, "opex_add_per_yr": opex_add,
                            "npc_add": npc_add, "lcoe_proxy_per_mwh": lcoe_proxy,
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

                    if best is None or npc_add < best["cost_proxy"]:
                        best = {"pv_add": pv_add, "bess_add_mwh": bess_add_mwh, "wind_add": wind_add,
                                "cost_proxy": npc_add, "capex_add": capex_add,
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
            schedule.pv.append(Tranche("pv", ty, pv_degradation_rate, capacity_mw=best["pv_add"],
                                        lifetime_years=lifetimes.get("pv")))
        if best.get("wind_add", 0) > 0:
            schedule.wind.append(Tranche("wind", ty, wind_degradation_rate, capacity_mw=best["wind_add"],
                                          lifetime_years=lifetimes.get("wind")))
        if best["bess_add_mwh"] > 0:
            schedule.bess.append(Tranche("bess", ty, bess_degradation_rate,
                                          power_mw=best["bess_add_mwh"] * bess_c_rate, energy_mwh=best["bess_add_mwh"],
                                          lifetime_years=lifetimes.get("bess")))

        log.append({"year": ty, "status": "OK", **best, "target_pct": target_pct,
                    "required_pct": required_pct, "override": is_override})

    return schedule, log, trials

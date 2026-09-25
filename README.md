# Bora Bora Net-Zero Capacity Expansion Planner

Purpose-built tool for P-001341 (Bora Bora net-zero advisory), answering the
question the Excel workbook's architecture cannot: **given PV/BESS degrade
and OTEC arrives partway through, what capacity do we add and when, to trace
a legally-compliant path from 75% RE (2030) to 100% RE (2050) without
excessive curtailment?**

## Why this exists (vs. the Excel workbook)

The workbook's `Dispatch_2028/2030/2035/2040/2050` sheets are **independent,
non-cumulative snapshots** - each assumes one fixed capacity for that single
year. It cannot answer "if I build 22 MWp in 2028, does that same physical
plant (now degraded) still hit target in 2035?" This tool tracks capacity as
a list of **vintage tranches** per technology, each aging from its own
commissioning year, and simulates the full 2026-2050 hourly dispatch year by
year.

## What it does

1. Loads the real hourly PV/Wind capacity-factor profiles and the real 2024
   baseline hourly demand shape, both extracted directly from
   `BORABORA_NZO_SEPT_24_nocap.xlsx` (`RE_Hourly_Generation_Factors`,
   `Demand_Hourly_Shape_8760`) - see `bora_bora_data.py`.
2. Adds **OTEC as a technology** (missing from the reference GitHub codebase
   entirely), fixed at the manager's decided 1.2 MW / CF 1.0 / 2032
   commissioning.
3. Sequentially sizes PV + BESS tranches at each procurement-cycle
   checkpoint (default 2028/2030/2035/2040/2050 - matching the workbook's own
   milestone years) via grid search, picking the **minimum-cost** combination
   that hits the RE target (+ a configurable buffer) at that checkpoint
   without exceeding a **curtailment cap** - directly addressing the original
   "22 MWp + 120 MWh gives 16,620 MWh of curtailment" concern by making
   curtailment a hard constraint instead of an unconstrained side-effect.
4. Runs the full 25-year hourly dispatch trajectory with the locked-in
   tranche schedule and reports RE%, curtailment, and unmet-load (= diesel-
   served fraction, since no diesel technology is modeled explicitly) for
   every year.

## Known gaps / things to fix before this is investment-grade

- **No cost sheet exists in the Excel workbook.** PV and BESS CAPEX/O&M in
  `bora_bora_engine.py::DEFAULT_COSTS` are **placeholders** - only OTEC's
  CAPEX is sourced from a real document (2H Offshore Aug-2024 study,
  EUR102-152M for 1.2-2.6 MW, converted at an illustrative FX rate). Replace
  all of these with validated client/EDT figures before using this for a
  real cost comparison.
- **EV/marine hourly shape is a flat placeholder.** The workbook's own
  `Combined_Hourly_Load` hardcodes these to zero for every hour; this tool
  instead spreads each year's EV+marine annual MWh evenly across all 8,760
  hours as a documented placeholder. Swap in the carbon team's real hourly
  profile via `data.py`'s `get_ev_marine_annual_mwh()` / the `ev_marine_shape`
  parameter in `engine.py::simulate_year()` the moment it's available.
- **Demand data beyond 2040 is extrapolated**, not carbon-team-supplied
  (the workbook snapshot only carried figures through 2040).
- **Curtailment numbers will NOT exactly match Excel.** This tool's dispatch
  logic (documented in `simulate_year()`) is a from-scratch physical merit-
  order simulation - the Excel model's own curtailment formula was never
  directly inspected cell-by-cell, so the two should be cross-checked before
  either number is quoted externally.
- The workbook snapshot used to extract the hourly profiles predates the
  OTEC (1.2 MW/CF 1.0/2032) and bus-exclusion corrections - those corrections
  are applied here in code (`bora_bora_data.py`), not read from the file.

## Running it

```bash
pip install -r requirements.txt
streamlit run bora_bora_app.py
```

## Files

- `bora_bora_data.py` - real hourly PV/Wind CF + baseline demand shape
  loaders, annual demand forecast table, corrected OTEC/bus assumptions.
- `bora_bora_engine.py` - `Tranche`/`TrancheSchedule` vintage tracking,
  `simulate_year()` hourly dispatch, `simulate_trajectory()`, HOMER-style NPC
  costing (adapted from `optimize_gridsearch_hydro_WITH_DEGRADATION.py`),
  `size_tranche_schedule()` sequential grid-search sizing.
- `bora_bora_app.py` - Streamlit UI.
- `re_hourly_factors.csv`, `baseline_demand_hourly_2024_MW.csv` - extracted
  8,760-hour source data.

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

1. Loads the hourly PV/wind capacity-factor profiles, the 2024 baseline hourly
   demand shape, the annual demand table, EV/marine day profiles and Grande
   Vaitape demand - all uploaded through the sidebar (see `bora_bora_data.py`;
   uploads are sanity-checked, see "Input guards").
2. Treats OTEC as an exogenous schedule (`year:mw` entries, default 2032:1.2 MW,
   CF 1.0), each entry its own vintage tranche.
3. Sequentially sizes PV + BESS (+ optional wind) tranches at each procurement
   checkpoint (default 2028/2030/2035/2040/2050) by grid search, choosing the
   **lowest-lifecycle-cost** combination that hits the RE target (+ buffer) at
   that checkpoint, optionally under a curtailment cap.
4. Runs the full 2026-2050 hourly dispatch with the locked-in schedule and reports
   RE%, curtailment, unmet load (= diesel-served fraction), a year-by-year **legal
   compliance table**, NPC and LCOE.

## Dispatch rules

- Merit order PV -> wind -> OTEC -> BESS -> unmet (residual diesel). The EDT
  penetration cap limits solar+wind served directly to load; excess above the cap
  charges the BESS first.
- **BESS operates in one direction per hour.** If there is surplus and room it
  charges (any remaining shortfall that hour is residual diesel); it discharges
  only in hours when it is not charging. At a 100% cap a surplus hour can never
  also have a shortfall; below 100% the rule is what prevents simultaneous
  charge/discharge. A counter in the app and a flag column in the hourly export
  confirm 0 such hours.
- **BESS starts at the commissioning SoC (default 50%)** for the energy
  commissioned that year (a new tranche or a replacement); capacity already online
  keeps the charge it had. In the sizing search each trial starts from the charge
  the already-built fleet would actually carry into that year (earlier years are
  simulated forward), so the RE% shown for a tranche year in the grid-search table
  equals that year's RE% in the final trajectory.
- Demand: the baseline hourly shape is normalised by its own sum and multiplied by
  the annual-table MWh for the year. Beyond the last table year (2040) baseline
  demand is held flat unless a growth rate is set; EV/marine hold the last year's
  profile; Grande Vaitape is flat from its commissioning year.
- Rooftop adds only its exported-to-grid share (the self-consumed share is already
  netted out of the demand table).

## Economics (single cash-flow method)

NPC = sum over years of (CAPEX + O&M) / (1 + nominal rate)^(year - 2026);
LCOE = NPC / NPV of **project-served** energy (same discounting).

- Unit costs are **2026 dollars**. With "Escalate CAPEX" on (default), a build in
  year Y costs unit CAPEX x (1 + inflation)^(Y - 2026), like O&M; off = flat 2026
  prices.
- **Lifetimes / replacement.** An asset is in service from its commissioning year
  through commissioning year + lifetime (inclusive); if that is before the horizon
  end, a like-for-like replacement is commissioned the next year (nameplate
  restored, degradation restarts, CAPEX re-booked). A BESS built in 2028 with a
  20-year life is replaced in 2049; with 22 years there is no replacement inside a
  2050 horizon. Untick the option to run everything to the horizon with no
  replacement. No salvage credit at the horizon end.
- OTEC CAPEX per tranche = fixed + $/MW x MW (default fixed 0, $63.14M/MW =
  EUR152M x 1.08 / 2.6 MW, the high anchor of the 2H Offshore study). That default is exact
  for a single 2.6 MW tranche only; a staged or smaller OTEC schedule needs a two-point
  fixed + variable fit once both anchors (and their capacities) are confirmed.
- **Rooftop is excluded from the LCOE denominator** (privately owned, not in the
  numerator). Its delivered energy (direct + its pro-rata share of BESS-stored
  energy) is estimated per hour and subtracted from total served energy.
- The "Year-by-year cash flow" table and the `LCOE_Cashflow` export sheet show every
  term so the result can be reconciled against a spreadsheet.
- **"Download LCOE verification workbook"** (Economics section) generates Inputs,
  Tranche_Schedule, Trajectory and Hybrid_LCOE sheets for the run with LIVE Excel
  formulas (SUMIFS over the tranche table, replacement years, CAPEX escalation switch,
  per-technology LCOE contributions). The sheet's LCOE equals the app's; edit an input
  there to test a sensitivity. Insert new tranche rows inside the Tranche_Schedule table.
- Sizing ranks feasible candidates by their own **NPC** (escalated CAPEX +
  replacements + escalated O&M, discounted), not day-one CAPEX. It is still greedy
  year by year - each tranche year is optimised given the earlier ones, not jointly.

## Compliance check

For every simulated year: RE% vs a legal floor (0 before 2030, the 2030 target held
through 2049, the 2050 target in 2050) plus an informational straight-line glide path.
The 2030 target being held as a floor until 2050 is a conservative design reading; the
law itself names only the 2030 and 2050 milestones.

## Input guards

Hard errors: negative capacity factors, capacity factors above 1.5 (percent
by mistake), empty/zero demand shape. Warnings: CF above 1.0, baseline shape not
summing to ~44,178 MWh, EV/marine peak above 10 MW (kW vs MW), Grande Vaitape shape
disagreeing with its annual MWh, annual demand table outside 10,000-200,000 MWh.

## Known gaps / things to fix before this is investment-grade

- **PV, wind and BESS CAPEX/O&M defaults are placeholders**; replace with validated
  client/EDT figures. OTEC CAPEX and O&M need a citable source.
- Rooftop uses the same hourly profile as the agrivoltaic PV, with its MWp divided by the
  DC:AC ratio first (about 1,800 kWh/kWp, in line with the workbook's existing-rooftop
  CF 0.2033). The workbook's lower CF for NEW rooftop (0.1777) and its 0.3%/yr rooftop
  degradation are not modelled; only the 7% exported share enters, so the gap is small
  (roughly 90 MWh/yr by 2050).
- Curtailment will not exactly match Excel: this is a from-scratch physical merit-order
  simulation; cross-check before quoting either number externally.
- The 2030/2050 milestone years are hardcoded in the glide-path and compliance logic.
- At a BESS replacement the retiring fleet's stored charge is clamped to the new
  fleet's SoC window rather than removed explicitly (small, one-off).

## Running it

```bash
pip install -r requirements.txt
streamlit run bora_bora_app.py
```

## Files

- `bora_bora_data.py` - CSV loaders, input guards, annual-table lookup, templates.
- `bora_bora_engine.py` - `Tranche`/`TrancheSchedule` vintage tracking (with service life
  and replacement), `simulate_year()` hourly dispatch, `simulate_trajectory()`,
  cash-flow NPC/LCOE, `compliance_table()`, `size_tranche_schedule()` sequential
  NPC-ranked grid search.
- `bora_bora_export.py` - builds the formula-driven LCOE verification workbook.
- `bora_bora_app.py` - Streamlit UI.

"""
bora_bora_data.py
==================
Loads real Bora Bora hourly generation/demand data extracted from
BORABORA_NZO_SEPT_24_nocap.xlsx (RE_Hourly_Generation_Factors,
Demand_Hourly_Shape_8760, Forecast_Annual, Assumptions sheets).

IMPORTANT — known corrections applied here that are NOT yet reflected in the
source workbook snapshot this was extracted from (per manager's decisions
communicated in the advisory conversation, as of Sept 2026):
  - OTEC: 1.2 MW, CF = 1.0, commissioning 2032 (the 2H Offshore Aug-2024
    feasibility study's 1.2-2.6 MW range is already a NET AVERAGE delivered
    output, not a nameplate capacity requiring a further CF multiplier -
    hence CF=1.0, i.e. flat 1.2 MW baseload). The raw workbook snapshot
    still shows the old placeholder (3 MW / CF 0.9 / commissioning 2028).
  - Electric bus demand: excluded (0), per carbon team scope change.
    The raw workbook snapshot still shows non-zero bus demand from 2030.
  - Land-EV (excl. bus) and marine transport demand: annual totals are
    taken from Forecast_Annual (these are carbon-team-supplied figures),
    but NO validated hourly shape exists yet for them in the workbook
    (Combined_Hourly_Load hardcodes these to zero for all hours). Pending
    the carbon team's actual hourly profile, this module distributes each
    year's EV+marine annual energy as a FLAT (24/7 constant) hourly
    addition on top of the baseline shape. This is a clearly-flagged
    placeholder - swap in real hourly shape via
    set_ev_marine_hourly_shape() the moment it's available.
"""

import csv
import os

DATA_DIR = os.path.dirname(os.path.abspath(__file__))

HOURS_PER_YEAR = 8760

# ---------------------------------------------------------------------------
# Corrected assumptions (overriding stale workbook snapshot values)
# ---------------------------------------------------------------------------
OTEC_CAPACITY_MW = 1.2          # manager's decision: lower bound of 1.2-2.6 MW range
OTEC_CF = 1.0                   # 1.2 MW figure is already net average output
OTEC_COMMISSIONING_YEAR = 2032  # per 2H Offshore Aug-2024 feasibility study
OTEC_DEGRADATION_RATE = 0.0     # Assumptions!B42 - OTEC assumed non-degrading

PV_DEGRADATION_RATE = 0.003     # Assumptions!B41 (rooftop & agrivoltaic PV)
WIND_DEGRADATION_RATE = 0.005   # Assumptions!B43
BESS_DEGRADATION_RATE = 0.015   # Assumptions!B44 (capacity fade)

RE_TARGET_2030 = 0.75
RE_TARGET_2050 = 1.00
BASELINE_YEAR = 2024
BASELINE_ANNUAL_DEMAND_MWH = 44178.0  # Assumptions!B6

EDT_PENETRATION_CAP = 1.0  # Assumptions!B101 - currently uncapped in this scenario version

WIND_INCLUDED = False  # Assumptions!B54 = 0 in current scenario


def _open_text(source):
    """Accepts: None (-> bundled default under DATA_DIR keyed by caller),
    a filesystem path (str/Path), or a file-like object (e.g. Streamlit's
    UploadedFile from st.file_uploader, which yields bytes). Returns
    something csv.DictReader can iterate as text lines."""
    if hasattr(source, "read"):  # file-like (e.g. st.file_uploader result)
        raw = source.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")
        return raw.splitlines()
    # plain path
    with open(source, newline="", encoding="utf-8-sig") as f:
        return f.readlines()


def load_hourly_generation_factors(source=None):
    """Returns (pv_cf, wind_cf) - two 8760-length lists (per-MW normalized).
    `source`: None -> bundled default CSV shipped in this repo;
              a file path -> read from disk;
              a file-like object (e.g. from st.file_uploader) -> read from upload,
              so a new profile can be swapped in from the browser without
              touching the repo.
    Expected columns: hour, month, day, hour_of_day, pv_cf, wind_cf[, otec_cf].
    OTEC CF is NOT read from file even if present - it's always overridden to
    the flat corrected value (OTEC_CF), since the bundled default's OTEC
    column reflects the old, superseded 3MW/0.9 placeholder.
    """
    if source is None:
        source = os.path.join(DATA_DIR, "re_hourly_factors.csv")
    lines = _open_text(source)
    reader = csv.DictReader(lines)
    pv_cf, wind_cf = [], []
    for row in reader:
        pv_cf.append(float(row["pv_cf"]))
        wind_cf.append(float(row["wind_cf"]))
    if len(pv_cf) != HOURS_PER_YEAR:
        raise ValueError(f"Expected 8760 hourly rows, got {len(pv_cf)}. "
                          f"Check the uploaded file has one row per hour, no gaps.")
    return pv_cf, wind_cf


def load_baseline_demand_shape_mw(source=None):
    """Returns an 8760-length list of baseline hourly demand (MW).
    `source`: None -> bundled default; a path or file-like object (upload) ->
    read from there instead. Expected column: demand_mw_2024_shape (plus the
    same hour/month/day/hour_of_day columns as the generation-factors file)."""
    if source is None:
        source = os.path.join(DATA_DIR, "baseline_demand_hourly_2024_MW.csv")
    lines = _open_text(source)
    reader = csv.DictReader(lines)
    demand = [float(row["demand_mw_2024_shape"]) for row in reader]
    if len(demand) != HOURS_PER_YEAR:
        raise ValueError(f"Expected 8760 hourly rows, got {len(demand)}. "
                          f"Check the uploaded file has one row per hour, no gaps.")
    return demand


# ---------------------------------------------------------------------------
# Annual demand forecast (existing development only, EXCLUDING bus, matching
# the corrected/fixed Forecast_Annual logic) - MWh/yr by calendar year.
# Extracted from Forecast_Annual row 16 ("Underlying demand, net of rooftop
# solar & efficiency") + row 17 (EV excl. bus) + row 19 (marine) + row 20
# (Grande Vaitape). Bus (row 18) deliberately excluded per scope change.
# Values shown are from the Sept-24 workbook snapshot; extend/replace this
# table once later years or updated carbon-team figures are available.
# ---------------------------------------------------------------------------
ANNUAL_DEMAND_MWH = {
    2024: 44178.0,
    2025: 40260.56,
    2026: 40154.46,
    2027: 40056.81 + 21.91 + 0 + 4992.32,       # underlying + EV(excl bus) + marine(0) + GV
    2028: 39967.63 + 24.67 + 958.13 + 4992.32,
    2029: 39886.92 + 25.61 + 958.13 + 4992.32,
    2030: 39814.71 + 3228.02 + 1916.25 + 4992.32,
    2031: 39751.02 + 3228.95 + 1916.25 + 4992.32,
    2032: 39695.86 + 3120.90 + 1916.25 + 4992.32,
    2033: 39649.25 + 3120.90 + 1916.25 + 4992.32,
    2034: 39611.22 + 3123.23 + 1916.25 + 4992.32,
    2035: 39581.78 + 4666.91 + 1916.25 + 4992.32,
    2036: 39560.95 + 4669.23 + 1916.25 + 4992.32,
    2037: 39548.77 + 4670.17 + 1916.25 + 4992.32,
    2038: 39545.24 + 4507.01 + 1916.25 + 4992.32,
    2039: 39550.40 + 4509.34 + 1916.25 + 4992.32,
    2040: 39564.27 + 6000.40 + 3832.50 + 4992.32,
}


def get_annual_demand_mwh(year, growth_rate_after_2040=0.01):
    """Annual total demand (MWh). Years beyond the extracted table (2041-2050)
    are extrapolated at Assumptions!B31's demand growth rate (1%/yr) applied
    to the last known year (2040) - flagged as an extrapolation, not a
    carbon-team-supplied figure."""
    if year in ANNUAL_DEMAND_MWH:
        return ANNUAL_DEMAND_MWH[year]
    if year > max(ANNUAL_DEMAND_MWH):
        last_year = max(ANNUAL_DEMAND_MWH)
        last_val = ANNUAL_DEMAND_MWH[last_year]
        return last_val * (1 + growth_rate_after_2040) ** (year - last_year)
    if year < min(ANNUAL_DEMAND_MWH):
        return BASELINE_ANNUAL_DEMAND_MWH
    raise ValueError(f"No demand data for year {year}")


# EV + marine annual energy, isolated (for the flat-shape overlay), MWh/yr
EV_MARINE_ANNUAL_MWH = {
    2024: 0, 2025: 0, 2026: 0,
    2027: 21.91,
    2028: 24.67 + 958.13,
    2029: 25.61 + 958.13,
    2030: 3228.02 + 1916.25,
    2031: 3228.95 + 1916.25,
    2032: 3120.90 + 1916.25,
    2033: 3120.90 + 1916.25,
    2034: 3123.23 + 1916.25,
    2035: 4666.91 + 1916.25,
    2036: 4669.23 + 1916.25,
    2037: 4670.17 + 1916.25,
    2038: 4507.01 + 1916.25,
    2039: 4509.34 + 1916.25,
    2040: 6000.40 + 3832.50,
}


def get_ev_marine_annual_mwh(year):
    if year in EV_MARINE_ANNUAL_MWH:
        return EV_MARINE_ANNUAL_MWH[year]
    if year > max(EV_MARINE_ANNUAL_MWH):
        return EV_MARINE_ANNUAL_MWH[max(EV_MARINE_ANNUAL_MWH)]  # hold flat beyond 2040 (placeholder)
    return 0.0

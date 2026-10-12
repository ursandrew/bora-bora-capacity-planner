"""
bora_bora_data.py
==================
Data loading for the Bora Bora capacity planner. Every load component and
every technology profile is loadable from an uploaded CSV (via the app's
sidebar) OR falls back to a bundled default file/table if nothing is
uploaded. Nothing here is meant to be edited in code during normal use -
see bora_bora_app.py for the corresponding upload widgets.
"""

import csv
import os

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
HOURS_PER_YEAR = 8760
DAYS_PER_YEAR = 365


# ---------------------------------------------------------------------------
# Generic CSV reading (accepts a path, a file-like upload, or None -> default)
# ---------------------------------------------------------------------------

def _read_rows(source, default_path):
    if source is None:
        source = default_path
    if hasattr(source, "read"):  # file-like, e.g. st.file_uploader result
        raw = source.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")
        lines = raw.splitlines()
    else:
        with open(source, newline="", encoding="utf-8-sig") as f:
            lines = f.readlines()
    return list(csv.DictReader(lines))


def _pick_column(row, candidates):
    for c in candidates:
        if c in row:
            return c
    raise KeyError(f"None of the expected columns {candidates} found in {list(row.keys())}")


def _to_float(value):
    """Converts a CSV cell to float, tolerating formatting Excel commonly
    applies when saving/exporting numbers as text: thousands-separator
    commas ("44,178.00"), surrounding whitespace, and a trailing/leading
    currency symbol."""
    s = str(value).strip()
    if s == "":
        return 0.0
    s = s.replace(",", "").replace("$", "").strip()
    return float(s)


# ---------------------------------------------------------------------------
# Hourly (8760-row) capacity-factor / demand-shape series
# ---------------------------------------------------------------------------

def load_hourly_series(source, value_columns, label):
    """Reads an 8760-row CSV and returns a plain list of floats from the
    first matching column name in `value_columns`. No bundled default -
    a file must be uploaded through the UI; `label` names what's missing
    in the error message."""
    if source is None:
        raise ValueError(f"Please upload a {label} CSV (none uploaded).")
    rows = _read_rows(source, None)
    if not rows:
        raise ValueError("File has no data rows.")
    col = _pick_column(rows[0], value_columns)
    series = [_to_float(r[col]) for r in rows]
    if len(series) != HOURS_PER_YEAR:
        raise ValueError(f"Expected {HOURS_PER_YEAR} hourly rows, got {len(series)}.")
    return series


CF_HARD_MAX = 1.5  # above this a "capacity factor" is almost certainly in percent (0-100), not a fraction


def _check_cf(series, label):
    """Hard guard: a capacity-factor series must be non-negative fractions. Values
    in (1.0, CF_HARD_MAX] are allowed through here and only flagged as a warning
    by collect_input_warnings(); anything above CF_HARD_MAX (e.g. percent values)
    or any negative value is rejected."""
    lo, hi = min(series), max(series)
    if lo < 0:
        raise ValueError(f"{label}: contains negative values (min {lo:.4g}). A capacity factor must be >= 0.")
    if hi > CF_HARD_MAX:
        raise ValueError(f"{label}: maximum value is {hi:.4g}. Capacity factors must be fractions "
                         f"(0-1) - this looks like percent. Divide the column by 100 and re-upload.")
    return series


def load_pv_cf(source=None):
    return _check_cf(load_hourly_series(source, ["pv_cf", "cf"], "PV generation-factor profile"),
                     "PV generation-factor profile")


def load_wind_cf(source=None):
    return _check_cf(load_hourly_series(source, ["wind_cf", "cf"], "wind generation-factor profile"),
                     "Wind generation-factor profile")


def load_baseline_demand_shape_mw(source=None):
    series = load_hourly_series(
        source, ["demand_mw_2024_shape", "mw", "demand"], "baseline demand shape",
    )
    if min(series) < 0:
        raise ValueError(f"Baseline demand shape contains negative values (min {min(series):.4g}).")
    if sum(series) <= 0:
        raise ValueError("Baseline demand shape sums to zero - the file is empty or all zeros.")
    return series


# Reference annual total the 2024 baseline hourly shape is expected to sum to. It is NOT used
# to scale demand any more (the engine normalises by the uploaded shape's own sum, so the annual
# table always drives each year's total) - it only feeds the sanity-check warning below.
BASELINE_ANNUAL_DEMAND_MWH = 44178.0


# ---------------------------------------------------------------------------
# Annual demand table (underlying/existing-development demand only - NOT
# including EV/marine or Grande Vaitape, which are tracked separately below)
# ---------------------------------------------------------------------------

DEFAULT_UNDERLYING_DEMAND_MWH = {
    2024: 44178.0, 2025: 40260.56, 2026: 40154.46, 2027: 40056.81,
    2028: 39967.63, 2029: 39886.92, 2030: 39814.71, 2031: 39751.02,
    2032: 39695.86, 2033: 39649.25, 2034: 39611.22, 2035: 39581.78,
    2036: 39560.95, 2037: 39548.77, 2038: 39545.24, 2039: 39550.40,
    2040: 39564.27,
}


def load_annual_table(source, default_table, year_col="year", value_col="mwh"):
    """Loads a two-column [year, value] CSV into a {year: value} dict.
    Falls back to `default_table` if no file is provided."""
    if source is None:
        return dict(default_table)
    rows = _read_rows(source, None)
    table = {}
    for r in rows:
        table[int(_to_float(r[year_col]))] = _to_float(r[value_col])
    return table


def lookup_annual(table, year, growth_rate_beyond_last=0.0):
    """Exact match if present; hold flat below the earliest year; extrapolate
    at `growth_rate_beyond_last` beyond the latest year; linearly interpolate
    for a gap between two known years."""
    if not table:
        return 0.0
    if year in table:
        return table[year]
    years = sorted(table)
    if year < years[0]:
        return table[years[0]]
    if year > years[-1]:
        return table[years[-1]] * (1 + growth_rate_beyond_last) ** (year - years[-1])
    lo = max(y for y in years if y < year)
    hi = min(y for y in years if y > year)
    frac = (year - lo) / (hi - lo)
    return table[lo] + frac * (table[hi] - table[lo])


# ---------------------------------------------------------------------------
# EV (land, excl. bus) + marine transport - a 24-hour DAY profile per
# calendar year (tiled x365 to build that year's 8760-hour profile), since
# the carbon team supplies one representative day per year, not a full
# 8760-hour series. Magnitude and shape both vary year to year (e.g. marine
# charger ramp-up from 2030), so this is captured entirely by which years
# are present in the uploaded table - no separate "commissioning year"
# switch is needed here.
# ---------------------------------------------------------------------------

def load_day_profiles_by_year(source):
    """Wide-format CSV, same layout as the Combined_Hourly_Load sheet:
    first column is the hour of day (0-23), every other column header is a
    calendar year, and each cell is that year's MW value for that hour -
    one representative day per year, which gets repeated for every day of
    that year. Returns {year: [24 floats]}, keyed by the year column headers
    present. Returns {} if source is None (no load modeled until a profile
    is supplied)."""
    if source is None:
        return {}
    rows = _read_rows(source, None)
    if not rows:
        raise ValueError("File has no data rows.")
    fieldnames = list(rows[0].keys())
    hour_col = fieldnames[0]  # first column, whatever it's labeled ("hour", "Hr#", ...)
    year_cols = [c for c in fieldnames[1:] if c.strip()]

    by_year = {}
    for yc in year_cols:
        try:
            year = int(_to_float(yc))
        except ValueError:
            continue  # skip any non-year column (e.g. a label column)
        by_year[year] = [0.0] * 24

    for r in rows:
        hour = int(_to_float(r[hour_col]))
        if not (0 <= hour <= 23):
            raise ValueError(f"Hour value {hour} is out of range 0-23.")
        for yc in year_cols:
            try:
                year = int(_to_float(yc))
            except ValueError:
                continue
            by_year[year][hour] = _to_float(r.get(yc, ""))

    if any(len(v) != 24 for v in by_year.values()):
        raise ValueError("Every year column must have all 24 hours (0-23) populated.")
    return by_year


def get_day_profile_for_year(profiles: dict, year):
    """Exact match; hold the nearest edge outside the given range; linearly
    interpolate hour-by-hour between the two neighboring years for a gap."""
    if not profiles:
        return [0.0] * 24
    if year in profiles:
        return profiles[year]
    years = sorted(profiles)
    if year < years[0]:
        return profiles[years[0]]
    if year > years[-1]:
        return profiles[years[-1]]
    lo = max(y for y in years if y < year)
    hi = min(y for y in years if y > year)
    frac = (year - lo) / (hi - lo)
    return [profiles[lo][h] + frac * (profiles[hi][h] - profiles[lo][h]) for h in range(24)]


def tile_day_profile_to_year(day_profile_24):
    return list(day_profile_24) * DAYS_PER_YEAR  # 24 * 365 = 8760


# ---------------------------------------------------------------------------
# Grande Vaitape - single development, own commissioning year, flat annual
# demand unless an hourly day-shape is supplied.
# ---------------------------------------------------------------------------

def load_single_day_shape(source):
    """24-row CSV: columns hour, mw. Returns [24 floats], or None if no file
    given (caller should then treat the load as flat 24/7)."""
    if source is None:
        return None
    rows = _read_rows(source, None)
    profile = [0.0] * 24
    for r in rows:
        profile[int(_to_float(r["hour"]))] = _to_float(r["mw"])
    return profile


# ---------------------------------------------------------------------------
# Input sanity checks (warnings only - none of these stop a run; hard errors
# are raised in the loaders above). Meant to catch silent unit/scale mistakes
# such as an EV profile uploaded in kW instead of MW.
# ---------------------------------------------------------------------------

def collect_input_warnings(pv_cf=None, wind_cf=None, baseline_shape=None,
                           ev_marine_profiles=None, gv_day_shape=None, gv_annual_mwh=None,
                           annual_table=None, island_peak_mw=7.5, extra_peak_warn_mw=10.0):
    """Returns a list of human-readable warning strings (empty list = all clear)."""
    warns = []

    for label, series in (("PV", pv_cf), ("Wind", wind_cf)):
        if series is not None and max(series) > 1.0:
            warns.append(f"{label} capacity-factor profile has values above 1.0 (max {max(series):.3f}). "
                         f"This is fine if it is MW per 1 MWac block of a DC-oversized plant (e.g. PVsyst E_Grid for "
                         f"1.3 MWp / 1 MWac, which can exceed 1.0 on clear hours); the model divides MWp by the "
                         f"DC:AC ratio first. Otherwise check it is not a different normalisation.")

    if baseline_shape is not None:
        total = sum(baseline_shape)
        dev = abs(total - BASELINE_ANNUAL_DEMAND_MWH) / BASELINE_ANNUAL_DEMAND_MWH
        if dev > 0.001:
            warns.append(f"Baseline demand shape sums to {total:,.1f} MWh, which differs from the expected "
                         f"{BASELINE_ANNUAL_DEMAND_MWH:,.0f} MWh by {dev*100:.2f}%. Each year's total is still set "
                         f"by the annual-demand table (the shape is normalised), but check the units - a shape "
                         f"in kW instead of MW would sum to ~1000x this.")

    if ev_marine_profiles:
        peak = max(max(day) for day in ev_marine_profiles.values())
        if peak > extra_peak_warn_mw:
            warns.append(f"EV + marine demand profile peaks at {peak:,.2f} MW, above {extra_peak_warn_mw:g} MW "
                         f"(island grid peak is about {island_peak_mw:g} MW). Are the values in kW instead of MW?")

    if gv_day_shape is not None and gv_annual_mwh:
        implied = sum(gv_day_shape) * DAYS_PER_YEAR
        if gv_annual_mwh > 0 and abs(implied - gv_annual_mwh) / gv_annual_mwh > 0.05:
            warns.append(f"Grande Vaitape day shape implies {implied:,.0f} MWh/yr but the annual-demand input is "
                         f"{gv_annual_mwh:,.0f} MWh/yr. When a shape is uploaded, the SHAPE is used and the annual "
                         f"input is ignored - make sure they agree.")

    if annual_table:
        vals = list(annual_table.values())
        if min(vals) < 10_000 or max(vals) > 200_000:
            warns.append(f"Annual-demand table runs from {min(vals):,.0f} to {max(vals):,.0f} MWh. "
                         f"The island baseline is about 40,000-45,000 MWh/yr - check the units (GWh/kWh?).")
    return warns


# ---------------------------------------------------------------------------
# CSV templates for download buttons in the app.
# Every template here is generated on the fly - correctly shaped and headed
# for the matching loader, but with no data in it. Nothing is read from a
# bundled file, so nothing needs to exist in the repo: upload is the only
# source of PV/wind/baseline-demand data, same as EV/marine/GV below.
# ---------------------------------------------------------------------------

def _hourly_skeleton_csv(value_col):
    lines = [f"hour,{value_col}"]
    for h in range(HOURS_PER_YEAR):
        lines.append(f"{h},0.0")
    return "\n".join(lines)


def pv_cf_template_csv():
    return _hourly_skeleton_csv("pv_cf")


def wind_cf_template_csv():
    return _hourly_skeleton_csv("wind_cf")


def baseline_demand_template_csv():
    return _hourly_skeleton_csv("mw")


def annual_table_default_csv(value_col="mwh"):
    lines = ["year," + value_col]
    for year, val in sorted(DEFAULT_UNDERLYING_DEMAND_MWH.items()):
        lines.append(f"{year},{val}")
    return "\n".join(lines)


def day_profile_by_year_template_csv(years=range(2025, 2041)):
    """Wide format matching Combined_Hourly_Load: hour (0-23) as rows, one
    column per year. Empty/zero skeleton - no real default data exists for
    EV/marine yet."""
    header = "hour," + ",".join(str(y) for y in years)
    lines = [header]
    for h in range(24):
        lines.append(f"{h}," + ",".join("0.0" for _ in years))
    return "\n".join(lines)


def single_day_shape_template_csv():
    lines = ["hour,mw"]
    for h in range(24):
        lines.append(f"{h},0.0")
    return "\n".join(lines)

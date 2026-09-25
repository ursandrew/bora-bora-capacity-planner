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


# ---------------------------------------------------------------------------
# Hourly (8760-row) capacity-factor / demand-shape series
# ---------------------------------------------------------------------------

def load_hourly_series(source, value_columns, default_path):
    """Reads an 8760-row CSV and returns a plain list of floats from the
    first matching column name in `value_columns`."""
    rows = _read_rows(source, default_path)
    if not rows:
        raise ValueError("File has no data rows.")
    col = _pick_column(rows[0], value_columns)
    series = [float(r[col]) for r in rows]
    if len(series) != HOURS_PER_YEAR:
        raise ValueError(f"Expected {HOURS_PER_YEAR} hourly rows, got {len(series)}.")
    return series


def load_pv_cf(source=None):
    return load_hourly_series(source, ["pv_cf", "cf"], os.path.join(DATA_DIR, "re_hourly_factors.csv"))


def load_wind_cf(source=None):
    return load_hourly_series(source, ["wind_cf", "cf"], os.path.join(DATA_DIR, "re_hourly_factors.csv"))


def load_baseline_demand_shape_mw(source=None):
    return load_hourly_series(
        source, ["demand_mw_2024_shape", "mw", "demand"],
        os.path.join(DATA_DIR, "baseline_demand_hourly_2024_MW.csv"),
    )


BASELINE_ANNUAL_DEMAND_MWH = 44178.0  # annual total the baseline hourly shape sums to


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
        table[int(float(r[year_col]))] = float(r[value_col])
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
    """Long-format CSV: columns year, hour, mw (hour = 0..23).
    Returns {year: [24 floats]}. Returns {} if source is None (i.e. no
    EV/marine load modeled until a profile is supplied)."""
    if source is None:
        return {}
    rows = _read_rows(source, None)
    by_year = {}
    for r in rows:
        year = int(float(r["year"]))
        hour = int(float(r["hour"]))
        mw = float(r["mw"])
        by_year.setdefault(year, [0.0] * 24)[hour] = mw
    for year, profile in by_year.items():
        if len(profile) != 24 or any(v is None for v in profile):
            raise ValueError(f"Year {year} does not have all 24 hours (0-23) populated.")
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
        profile[int(float(r["hour"]))] = float(r["mw"])
    return profile


# ---------------------------------------------------------------------------
# CSV templates for download buttons in the app
# ---------------------------------------------------------------------------

def cf_template_csv(column_name):
    lines = ["hour,month,day,hour_of_day," + column_name]
    for h in range(1, 25):
        lines.append(f"{h},1,1,{h-1},0.0")
    return "\n".join(lines)


def baseline_demand_template_csv():
    lines = ["hour,month,day,hour_of_day,demand_mw_2024_shape"]
    for h in range(1, 25):
        lines.append(f"{h},1,1,{h-1},0.0")
    return "\n".join(lines)


def annual_table_template_csv(value_col="mwh"):
    return "year," + value_col + "\n2028,0\n2030,0\n2035,0\n2040,0\n2050,0"


def day_profile_by_year_template_csv():
    lines = ["year,hour,mw"]
    for year in (2028, 2030, 2035):
        for h in range(24):
            lines.append(f"{year},{h},0.0")
    return "\n".join(lines)


def single_day_shape_template_csv():
    lines = ["hour,mw"]
    for h in range(24):
        lines.append(f"{h},0.0")
    return "\n".join(lines)

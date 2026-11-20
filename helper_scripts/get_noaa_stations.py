#!/usr/bin/env python3
"""
Build a CSV of NOAA (NCEI GHCN-Daily) stations inside the WRIA watershed
shapefile that record precipitation.

No filtering on what else a station reports: a station that records
precipitation plus temperature, snow, wind, etc. is kept, so every network that
has a precipitation gauge inside the boundary shows up. Parameter names in the
CSV are written out in full ("Precipitation", not "PRCP"). The app's parameter
dropdown filters on these names.

Output columns include station id, name, network, lat/lon, elevation, period of
record, the parameters the station reports (full names), and the attributes of
the WRIA feature(s) the station falls in.

Usage:
    python helper_scripts/get_noaa_stations.py
    python helper_scripts/get_noaa_stations.py --list-parameters
    python helper_scripts/get_noaa_stations.py --active-only
    python helper_scripts/get_noaa_stations.py --include-no-prcp
    python helper_scripts/get_noaa_stations.py --shapefile path/to/file.shp --out-dir path/to/data

Defaults (layout: <project>/helper_scripts/this_file.py, <project>/data/):
    shapefile : searched for under the project folder by file name
    out-dir   : <project>/data   (created if missing)
    output    : <out-dir>/noaa_stations_in_wria.csv

Requires: pandas, geopandas  (internet access on first run; downloads are cached)
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd

GHCN_BASE = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/"
SHAPEFILE_NAME = "WRIA_boundaries_derived_from_terrain_data.shp"

HERE = Path(__file__).resolve().parent          # <project>/helper_scripts
PROJECT_ROOT = HERE.parent                      # <project>
DEFAULT_OUT_DIR = PROJECT_ROOT / "data"

SKIP_DIRS = {".venv", "venv", "env", "node_modules", "site-packages", ".git"}

# Delimiter between parameter names in the "elements" column
ELEMENT_SEP = "; "

# Third character of a GHCN-Daily station ID identifies the source network.
NETWORK_CODES = {
    "C": "COOP (NWS Cooperative Observer)",
    "W": "WBAN (NWS/FAA first-order, airport)",
    "1": "CoCoRaHS (volunteer)",
    "R": "RAWS",
    "S": "SNOTEL",
    "M": "WMO",
    "N": "National met service",
    "E": "ECA&D",
    "0": "Unspecified",
}

# --------------------------------------------------------------------------- #
# Parameter names (no commas, so the list can be joined with a delimiter)
# --------------------------------------------------------------------------- #
ELEMENT_NAMES = {
    "PRCP": "Precipitation",
    "SNOW": "Snowfall",
    "SNWD": "Snow depth",
    "WESD": "Water equivalent of snow on the ground",
    "WESF": "Water equivalent of snowfall",
    "MDPR": "Multiday precipitation total",
    "DAPR": "Number of days in multiday precipitation total",
    "DWPR": "Number of days with non-zero precipitation in multiday total",
    "MDSF": "Multiday snowfall total",
    "DASF": "Number of days in multiday snowfall total",
    "TMAX": "Maximum temperature",
    "TMIN": "Minimum temperature",
    "TAVG": "Average temperature",
    "TOBS": "Temperature at time of observation",
    "MDTN": "Multiday minimum temperature",
    "MDTX": "Multiday maximum temperature",
    "DATN": "Number of days in multiday minimum temperature",
    "DATX": "Number of days in multiday maximum temperature",
    "AWND": "Average daily wind speed",
    "AWDR": "Average daily wind direction",
    "WSF1": "Fastest 1-minute wind speed",
    "WSF2": "Fastest 2-minute wind speed",
    "WSF5": "Fastest 5-second wind speed",
    "WSFG": "Peak gust wind speed",
    "WSFI": "Highest instantaneous wind speed",
    "WSFM": "Fastest 5-minute wind speed",
    "WDF1": "Direction of fastest 1-minute wind",
    "WDF2": "Direction of fastest 2-minute wind",
    "WDF5": "Direction of fastest 5-second wind",
    "WDFG": "Direction of peak wind gust",
    "WDFI": "Direction of highest instantaneous wind",
    "WDMV": "24-hour wind movement",
    "MDWM": "Multiday wind movement",
    "DAWM": "Number of days in multiday wind movement",
    "PGTM": "Peak gust time",
    "FMTM": "Time of fastest mile or 1-minute wind",
    "EVAP": "Evaporation from evaporation pan",
    "MDEV": "Multiday evaporation total",
    "DAEV": "Number of days in multiday evaporation total",
    "MNPN": "Minimum temperature of water in evaporation pan",
    "MXPN": "Maximum temperature of water in evaporation pan",
    "PSUN": "Percent of possible sunshine",
    "TSUN": "Total sunshine",
    "THIC": "Thickness of ice on water",
    "GAHT": "Gauge height",
    "FRGB": "Base of frozen ground layer",
    "FRGT": "Top of frozen ground layer",
    "FRTH": "Thickness of frozen ground layer",
    "ACMC": "Average cloudiness midnight to midnight (ceilometer)",
    "ACMH": "Average cloudiness midnight to midnight (manual)",
    "ACSC": "Average cloudiness sunrise to sunset (ceilometer)",
    "ACSH": "Average cloudiness sunrise to sunset (manual)",
}

WEATHER_TYPES = {
    "01": "Fog", "02": "Heavy fog", "03": "Thunder",
    "04": "Ice pellets sleet or small hail", "05": "Hail", "06": "Glaze or rime",
    "07": "Dust ash or blowing sand", "08": "Smoke or haze",
    "09": "Blowing or drifting snow", "10": "Tornado or waterspout",
    "11": "High or damaging winds", "12": "Blowing spray", "13": "Mist",
    "14": "Drizzle", "15": "Freezing drizzle", "16": "Rain", "17": "Freezing rain",
    "18": "Snow or ice crystals", "19": "Unknown source of precipitation",
    "21": "Ground fog", "22": "Ice fog or freezing fog",
}
SOIL_COVER = {"0": "unknown cover", "1": "grass", "2": "fallow", "3": "bare ground",
              "4": "brome grass", "5": "sod", "6": "straw mulch",
              "7": "grass muck", "8": "bare muck"}
SOIL_DEPTH = {"1": "5 cm", "2": "10 cm", "3": "20 cm", "4": "50 cm",
              "5": "100 cm", "6": "150 cm", "7": "180 cm"}

UNMAPPED: set[str] = set()


def element_name(code: str) -> str:
    """Full parameter name for a NOAA element code (falls back to the code)."""
    if code in ELEMENT_NAMES:
        return ELEMENT_NAMES[code]
    if code[:2] == "WT" and code[2:] in WEATHER_TYPES:
        return f"Weather type: {WEATHER_TYPES[code[2:]]}"
    if code[:2] == "WV" and code[2:] in WEATHER_TYPES:
        return f"Weather in vicinity: {WEATHER_TYPES[code[2:]]}"
    if len(code) == 4 and code[:2] in ("SN", "SX") and code[2:].isdigit():
        kind = "Minimum" if code[:2] == "SN" else "Maximum"
        cover = SOIL_COVER.get(code[2], "cover code " + code[2])
        depth = SOIL_DEPTH.get(code[3], "depth code " + code[3])
        return f"{kind} soil temperature ({cover} at {depth})"
    UNMAPPED.add(code)
    return code


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def find_shapefile(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            sys.exit(f"Shapefile not found: {p}")
        return p
    matches = [
        p for p in PROJECT_ROOT.rglob(SHAPEFILE_NAME)
        if not any(part in SKIP_DIRS for part in p.parts)
    ]
    if not matches:
        sys.exit(
            f"Could not find {SHAPEFILE_NAME} under {PROJECT_ROOT}. "
            "Pass the path with --shapefile."
        )
    if len(matches) > 1:
        print(f"Multiple shapefiles found; using {matches[0]}", file=sys.stderr)
    return matches[0]


def fetch(name: str, cache_dir: Path, refresh: bool) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / name
    if refresh or not dest.exists():
        print(f"Downloading {name} ...")
        urllib.request.urlretrieve(GHCN_BASE + name, dest)
    return dest


def read_stations(path: Path) -> pd.DataFrame:
    return pd.read_fwf(
        path,
        colspecs=[(0, 11), (12, 20), (21, 30), (31, 37), (38, 40),
                  (41, 71), (72, 75), (76, 79), (80, 85)],
        names=["id", "lat", "lon", "elev_m", "state", "name",
               "gsn_flag", "hcn_crn_flag", "wmo_id"],
        header=None,
        dtype={"id": str, "state": str, "name": str, "gsn_flag": str,
               "hcn_crn_flag": str, "wmo_id": str},
        keep_default_na=False,
    )


def read_inventory(path: Path) -> pd.DataFrame:
    return pd.read_fwf(
        path,
        colspecs=[(0, 11), (12, 20), (21, 30), (31, 35), (36, 40), (41, 45)],
        names=["id", "lat", "lon", "element", "first_year", "last_year"],
        header=None,
        dtype={"id": str, "element": str},
        keep_default_na=False,
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shapefile", help="Path to the WRIA .shp (auto-detected if omitted)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Folder for the CSV")
    ap.add_argument("--out-name", default="noaa_stations_in_wria.csv")
    ap.add_argument("--list-parameters", action="store_true",
                    help="Print every parameter reported inside the boundary and exit")
    ap.add_argument("--active-only", action="store_true",
                    help="Keep only stations with data in the last two calendar years")
    ap.add_argument("--include-no-prcp", action="store_true",
                    help="Also keep stations that never record precipitation")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-download the GHCN-Daily station files")
    args = ap.parse_args()

    shp_path = find_shapefile(args.shapefile)
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "_ghcnd_cache"

    # --- Shapefile -> WGS84 ------------------------------------------------ #
    print(f"Reading {shp_path}")
    wria = gpd.read_file(shp_path)
    if wria.crs is None:
        sys.exit("Shapefile has no CRS defined (.prj missing?); cannot reproject.")
    wria = wria.to_crs(4326)
    wria["geometry"] = wria.geometry.buffer(0)          # repair minor invalid geometry
    wria = wria.reset_index(drop=True)
    wria.index.name = "wria_feature_idx"
    minx, miny, maxx, maxy = wria.total_bounds

    # --- GHCN-Daily metadata ---------------------------------------------- #
    stations = read_stations(fetch("ghcnd-stations.txt", cache_dir, args.refresh))
    inventory = read_inventory(fetch("ghcnd-inventory.txt", cache_dir, args.refresh))

    # Cheap bounding-box prefilter before the spatial join
    stations = stations[stations.lon.between(minx, maxx) & stations.lat.between(miny, maxy)]
    inventory = inventory[inventory.id.isin(stations.id)]

    codes = (inventory.groupby("id").element
             .apply(lambda s: sorted(s.unique()))
             .rename("element_codes"))
    span = (inventory.groupby("id")
            .agg(data_first_year=("first_year", "min"), data_last_year=("last_year", "max")))
    prcp = (inventory[inventory.element == "PRCP"]
            .set_index("id")[["first_year", "last_year"]]
            .rename(columns={"first_year": "prcp_first_year", "last_year": "prcp_last_year"}))

    stations = (stations
                .merge(codes, left_on="id", right_index=True, how="left")
                .merge(span, left_on="id", right_index=True, how="left")
                .merge(prcp, left_on="id", right_index=True, how="left"))
    stations["element_codes"] = stations["element_codes"].apply(
        lambda v: v if isinstance(v, list) else [])

    # --- Spatial join against the individual WRIA features ---------------- #
    pts = gpd.GeoDataFrame(
        stations,
        geometry=gpd.points_from_xy(stations.lon, stations.lat),
        crs="EPSG:4326",
    )
    features = wria.add_prefix("wria_").set_geometry("wria_geometry")
    # reset_index() turns the feature index into a real column ("wria_feature_idx"),
    # so we don't depend on how geopandas names the joined index column
    features = features.rename_geometry("geometry").reset_index()
    joined = gpd.sjoin(pts, features, how="inner", predicate="intersects")

    # A station on a shared border can match more than one feature
    joined["n_wria_features"] = joined.groupby("id")["id"].transform("size")
    joined = joined.sort_values(["id", "wria_feature_idx"]).drop_duplicates("id")
    joined = joined.drop(columns=["geometry", "index_right"], errors="ignore")

    # --- Parameter listing / filtering ------------------------------------ #
    counts = joined["element_codes"].explode().dropna().value_counts()
    if args.list_parameters:
        listing = pd.DataFrame({
            "code": counts.index,
            "parameter": [element_name(c) for c in counts.index],
            "stations": counts.values,
        })
        print(f"\nParameters reported by the {len(joined)} stations inside the boundary:\n")
        print(listing.to_string(index=False))
        return

    # Keep stations that record precipitation (nothing is excluded for also
    # reporting other parameters)
    if not args.include_no_prcp:
        has_prcp = joined["element_codes"].apply(lambda c: "PRCP" in c)
        print(f"{len(joined)} stations inside the boundary; "
              f"{int(has_prcp.sum())} record precipitation")
        joined = joined[has_prcp].copy()
    else:
        print(f"{len(joined)} stations inside the boundary")
    if joined.empty:
        sys.exit("No stations matched. Try --include-no-prcp or check the shapefile.")

    # --- Derived columns --------------------------------------------------- #
    joined["elements"] = joined["element_codes"].apply(
        lambda c: ELEMENT_SEP.join(sorted(element_name(x) for x in c)))
    joined["n_parameters"] = joined["element_codes"].apply(len)
    joined = joined.drop(columns=["element_codes"])

    code = joined["id"].str[2]
    joined["network_code"] = code
    joined["network"] = code.map(NETWORK_CODES).fillna("Other")
    joined["country"] = joined["id"].str[:2]
    this_year = date.today().year
    joined["active"] = joined["data_last_year"] >= this_year - 1
    joined["data_span_years"] = joined["data_last_year"] - joined["data_first_year"] + 1
    joined["source"] = "NCEI GHCN-Daily"

    if args.active_only:
        joined = joined[joined.active]

    lead = ["id", "name", "network", "network_code", "lat", "lon", "elev_m", "state",
            "wmo_id", "gsn_flag", "hcn_crn_flag", "elements", "n_parameters",
            "data_first_year", "data_last_year", "data_span_years", "active",
            "prcp_first_year", "prcp_last_year", "n_wria_features"]
    rest = [c for c in joined.columns if c not in lead + ["country", "source"]]
    out = joined[lead + rest + ["country", "source"]].sort_values(["network", "id"])

    out_path = out_dir / args.out_name
    out.to_csv(out_path, index=False)

    print(f"\nWrote {len(out)} stations -> {out_path}")
    print(out.groupby("network").size().rename("stations").to_string())
    if UNMAPPED:
        print(f"\nNote: no full name on file for {sorted(UNMAPPED)}; codes left as-is.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
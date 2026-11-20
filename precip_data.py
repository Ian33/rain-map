"""
Daily precipitation from NOAA NCEI (GHCN-Daily) with on-disk caching.

Source: NCEI Access Data Service, dataset "daily-summaries", no API key needed
    https://www.ncei.noaa.gov/access/services/data/v1

Why daily values: GHCN-Daily is a daily archive (COOP and CoCoRaHS gauges report
a 24-hour total for the day ending at the morning observation, mostly ~7 am).
We always keep the *daily* rows, so totals, and later day-by-day cumulative
graphs, come from the same cached data.

Periods
    last24 / last48 / last72   the 1 / 2 / 3 most recent calendar days ending
                               yesterday (today's reports are not in the archive
                               yet, and recent days can still arrive late)
    wytd                       current water year to date (Oct 1 .. yesterday)
    wy2026, wy2025, ...        the last 10 completed water years (Oct 1 .. Sep 30)

Caching (folder: <project>/data/precip_cache/)
    recent.csv.gz             last 10 days, one fetch serves the 24/48/72 h views
    wyYYYY.csv.gz             one file per water year
    *.json                    which stations were fetched, when, and the window
Completed water years are kept until you delete the file (or pass force=True);
the recent window and the current water year are re-fetched after a short TTL.

Typical use
    store = PrecipStore(Path("data/precip_cache"))
    period = build_periods(date.today())["wytd"]
    daily = store.get(period, station_ids)             # long: id, date, prcp_mm
    totals = summarize(daily, period, station_ids)     # id -> total_mm, days_reported, ...
    matrix = cumulative(daily, period)                 # date x station running total (graphing)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

API_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
BATCH_SIZE = 40                      # stations per request (keeps URLs short)
RECENT_DAYS = 10                     # window held in recent.csv.gz
RECENT_TTL = timedelta(hours=1)
CURRENT_WY_TTL = timedelta(hours=6)
LATE_DATA_GRACE = timedelta(days=7)  # a finished water year is "final" after this
N_PAST_WATER_YEARS = 10
MM_PER_INCH = 25.4

ProgressFn = Callable[[int, int], None]


class PrecipFetchError(RuntimeError):
    """The NCEI service could not be reached or returned an error."""


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Period:
    key: str            # "last24", "wytd", "wy2025"
    label: str          # for the UI
    start: date         # first day (inclusive)
    end: date           # last day (inclusive); may be < start when no days exist yet
    cache_name: str     # "recent" or "wyYYYY"
    fetch_start: date   # window actually fetched/cached for this period
    fetch_end: date
    final: bool         # True when the data should no longer change

    @property
    def expected_days(self) -> int:
        return max((self.end - self.start).days + 1, 0)


def water_year_of(d: date) -> int:
    """Water year runs Oct 1 .. Sep 30 and is named for the year it ends in."""
    return d.year + 1 if d.month >= 10 else d.year


def water_year_bounds(wy: int) -> tuple[date, date]:
    return date(wy - 1, 10, 1), date(wy, 9, 30)


def _fmt(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def build_periods(today: date | None = None) -> dict[str, Period]:
    """Ordered mapping key -> Period for every query the app offers."""
    today = today or date.today()
    yesterday = today - timedelta(days=1)
    periods: dict[str, Period] = {}

    recent_start = yesterday - timedelta(days=RECENT_DAYS - 1)
    for hours in (24, 48, 72):
        n = hours // 24
        start = yesterday - timedelta(days=n - 1)
        span = _fmt(start) if n == 1 else f"{_fmt(start)} to {_fmt(yesterday)}"
        periods[f"last{hours}"] = Period(
            key=f"last{hours}",
            label=f"Last {hours} hours (daily reports {span})",
            start=start, end=yesterday,
            cache_name="recent", fetch_start=recent_start, fetch_end=yesterday,
            final=False,
        )

    cur_wy = water_year_of(today)
    wy_start, wy_end = water_year_bounds(cur_wy)
    periods["wytd"] = Period(
        key="wytd",
        label=f"Water year {cur_wy} to date ({_fmt(wy_start)} to {_fmt(yesterday)})",
        start=wy_start, end=yesterday,
        cache_name=f"wy{cur_wy}", fetch_start=wy_start, fetch_end=yesterday,
        final=False,
    )

    for wy in range(cur_wy - 1, cur_wy - 1 - N_PAST_WATER_YEARS, -1):
        s, e = water_year_bounds(wy)
        periods[f"wy{wy}"] = Period(
            key=f"wy{wy}",
            label=f"Water year {wy} ({_fmt(s)} to {_fmt(e)})",
            start=s, end=e,
            cache_name=f"wy{wy}", fetch_start=s, fetch_end=e,
            final=today > e + LATE_DATA_GRACE,
        )
    return periods


# --------------------------------------------------------------------------- #
# NCEI requests
# --------------------------------------------------------------------------- #
def _request(stations: list[str], start: date, end: date) -> list[dict]:
    params = {
        "dataset": "daily-summaries",
        "dataTypes": "PRCP",
        "stations": ",".join(stations),
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "units": "metric",              # PRCP in mm
        "includeAttributes": "true",    # adds PRCP_ATTRIBUTES: "measurement,quality,source,obs-time"
        "format": "json",
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params, safe=',')}"
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rain-map/1.0"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read().decode("utf-8").strip()
            rows = json.loads(body) if body else []
            if not rows:
                print(f"[precip] NCEI returned no rows. Try this URL in a browser:\n  {url}")
            return rows
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504):
                last_err = e
                time.sleep(2 * (attempt + 1))
                continue
            raise PrecipFetchError(f"NCEI returned HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise PrecipFetchError(f"Could not reach NCEI after 3 attempts: {last_err}")


def _rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    """Raw API rows -> tidy frame [id, date, prcp_mm]; QC-failed values are dropped."""
    if not rows:
        return pd.DataFrame(columns=["id", "date", "prcp_mm"])
    df = pd.DataFrame(rows)
    if "PRCP" not in df.columns:
        return pd.DataFrame(columns=["id", "date", "prcp_mm"])
    out = pd.DataFrame({
        "id": df["STATION"].astype(str).str.replace("GHCND:", "", regex=False),
        "date": pd.to_datetime(df["DATE"]).dt.date,
        "prcp_mm": pd.to_numeric(df["PRCP"], errors="coerce"),
    })
    if "PRCP_ATTRIBUTES" in df.columns:
        # attributes: measurement flag, quality flag, source flag, observation time
        qflag = df["PRCP_ATTRIBUTES"].fillna("").astype(str).str.split(",").str[1].fillna("")
        out.loc[qflag.str.strip() != "", "prcp_mm"] = float("nan")
    return out.dropna(subset=["prcp_mm"]).reset_index(drop=True)


def fetch_daily(station_ids: Iterable[str], start: date, end: date,
                progress: ProgressFn | None = None) -> tuple[pd.DataFrame, set[str]]:
    """Returns (daily rows, ids that were answered).

    A batch that comes back completely empty is treated as "no answer" rather than
    "these stations have no data", so it is never cached as a success.
    """
    empty = pd.DataFrame(columns=["id", "date", "prcp_mm"])
    ids = sorted(set(station_ids))
    if not ids or start > end:
        return empty, set()
    batches = [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]
    frames, answered = [], set()
    for n, batch in enumerate(batches, 1):
        rows = _request(batch, start, end)
        frames.append(_rows_to_frame(rows))
        if rows:
            answered.update(batch)
        if progress:
            progress(n, len(batches))
    out = pd.concat(frames, ignore_index=True) if frames else empty
    return out.drop_duplicates(["id", "date"]), answered


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
class PrecipStore:
    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- file helpers --
    def _paths(self, name: str) -> tuple[Path, Path]:
        return self.dir / f"{name}.csv.gz", self.dir / f"{name}.json"

    def _load(self, name: str) -> tuple[pd.DataFrame, dict]:
        data_path, meta_path = self._paths(name)
        if data_path.exists() and meta_path.exists():
            try:
                df = pd.read_csv(data_path, parse_dates=["date"])
                df["date"] = df["date"].dt.date
                meta = json.loads(meta_path.read_text())
                return df, meta
            except Exception:
                pass                      # corrupt cache -> start over
        return pd.DataFrame(columns=["id", "date", "prcp_mm"]), {}

    def _save(self, name: str, df: pd.DataFrame, meta: dict) -> None:
        data_path, meta_path = self._paths(name)
        df.to_csv(data_path, index=False, compression="gzip")
        meta_path.write_text(json.dumps(meta))

    # -- main entry --
    def get(self, period: Period, station_ids: Iterable[str], *,
            force: bool = False, progress: ProgressFn | None = None) -> pd.DataFrame:
        """Daily rows [id, date, prcp_mm] for the stations, fetching only what is missing."""
        ids = sorted(set(station_ids))
        df, meta = self._load(period.cache_name)

        window = [period.fetch_start.isoformat(), period.fetch_end.isoformat()]
        ttl = RECENT_TTL if period.cache_name == "recent" else CURRENT_WY_TTL
        fetched_at = (datetime.fromisoformat(meta["fetched_at"])
                      if meta.get("fetched_at") else None)
        age_expired = (not period.final and fetched_at is not None
                       and datetime.now(timezone.utc) - fetched_at > ttl)
        if force or meta.get("window") != window or age_expired:
            df = pd.DataFrame(columns=["id", "date", "prcp_mm"])
            meta = {}

        fetched = set(meta.get("fetched", []))
        missing = [s for s in ids if s not in fetched]
        if missing:
            new, answered = fetch_daily(missing, period.fetch_start, period.fetch_end, progress)
            if answered:                      # never cache a reply that was entirely empty
                df = pd.concat([df, new], ignore_index=True).drop_duplicates(["id", "date"])
                fetched.update(answered)
                meta = {
                    "window": window,
                    "fetched": sorted(fetched),
                    "fetched_at": meta.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
                }
                self._save(period.cache_name, df, meta)

        return df[df["id"].isin(ids)].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
def _in_period(daily: pd.DataFrame, period: Period) -> pd.DataFrame:
    return daily[(daily["date"] >= period.start) & (daily["date"] <= period.end)]


def summarize(daily: pd.DataFrame, period: Period, station_ids: Iterable[str]) -> pd.DataFrame:
    """One row per station: total_mm (NaN when nothing reported), days_reported, days_expected."""
    ids = list(station_ids)
    d = _in_period(daily, period)
    g = d.groupby("id")["prcp_mm"].agg(total_mm="sum", days_reported="count")
    out = g.reindex(ids)
    out["days_reported"] = out["days_reported"].fillna(0).astype(int)
    out.loc[out["days_reported"] == 0, "total_mm"] = float("nan")
    out["days_expected"] = period.expected_days
    out.index.name = "id"
    return out


def daily_matrix(daily: pd.DataFrame, period: Period) -> pd.DataFrame:
    """date x station table of daily precipitation (mm); days with no report are NaN."""
    d = _in_period(daily, period)
    wide = d.pivot(index="date", columns="id", values="prcp_mm")
    full = pd.date_range(period.start, period.end, freq="D").date
    return wide.reindex(full)


def cumulative(daily: pd.DataFrame, period: Period) -> pd.DataFrame:
    """Running total per station for graphing day-by-day accumulation.

    Missing days add nothing (they are not guessed), so a station with gaps will
    under-count; compare with daily_matrix(...).notna() to see where the gaps are.
    """
    return daily_matrix(daily, period).fillna(0).cumsum()


def mm_to_inches(mm):
    return mm / MM_PER_INCH
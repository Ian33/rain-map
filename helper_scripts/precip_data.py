"""
Daily precipitation from NOAA NCEI GHCN-Daily.

No caching.
Every request hits NOAA directly.

Returned columns:
    id
    date
    prcp_mm
    measurement_flag
    quality_flag
    source_flag
    obs_time
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterable

import pandas as pd


API_URL = "https://www.ncei.noaa.gov/access/services/data/v1"

BATCH_SIZE = 40
RECENT_DAYS = 10
N_PAST_WATER_YEARS = 10
MM_PER_INCH = 25.4

ProgressFn = Callable[[int, int], None]


class PrecipFetchError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: date
    end: date

    fetch_start: date
    fetch_end: date

    @property
    def expected_days(self) -> int:
        return max((self.end - self.start).days + 1, 0)


def water_year_of(d: date) -> int:
    return d.year + 1 if d.month >= 10 else d.year


def water_year_bounds(wy: int) -> tuple[date, date]:
    return date(wy - 1, 10, 1), date(wy, 9, 30)


def _fmt(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def build_periods(today: date | None = None) -> dict[str, Period]:

    today = today or date.today()
    yesterday = today - timedelta(days=1)

    periods = {}

    recent_start = yesterday - timedelta(days=RECENT_DAYS - 1)

    for hours in (24, 48, 72):

        n = hours // 24
        start = yesterday - timedelta(days=n - 1)

        span = (
            _fmt(start)
            if n == 1
            else f"{_fmt(start)} to {_fmt(yesterday)}"
        )

        periods[f"last{hours}"] = Period(
            key=f"last{hours}",
            label=f"Last {hours} hours (daily reports {span})",
            start=start,
            end=yesterday,
            fetch_start=recent_start,
            fetch_end=yesterday,
        )

    current_wy = water_year_of(today)

    wy_start, _ = water_year_bounds(current_wy)

    periods["wytd"] = Period(
        key="wytd",
        label=f"Water Year {current_wy} To Date",
        start=wy_start,
        end=yesterday,
        fetch_start=wy_start,
        fetch_end=yesterday,
    )

    for wy in range(
        current_wy - 1,
        current_wy - 1 - N_PAST_WATER_YEARS,
        -1,
    ):
        start, end = water_year_bounds(wy)

        periods[f"wy{wy}"] = Period(
            key=f"wy{wy}",
            label=f"Water Year {wy}",
            start=start,
            end=end,
            fetch_start=start,
            fetch_end=end,
        )

    return periods


# --------------------------------------------------------------------------- #
# NOAA Request
# --------------------------------------------------------------------------- #

def _request(
    stations: list[str],
    start: date,
    end: date,
) -> list[dict]:
    # "hourly-summaries"
    # "daily-summaries"
    params = {
        "dataset": "daily-summaries",
        "dataTypes": "PRCP",
        "stations": ",".join(stations),
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "units": "metric",
        "includeAttributes": "true",
        "format": "json",
    }

    url = (
        f"{API_URL}?"
        f"{urllib.parse.urlencode(params, safe=',')}"
    )

    last_err = None

    for attempt in range(3):

        try:

            req = urllib.request.Request(
                url,
                headers={"User-Agent": "rain-map/1.0"},
            )

            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read().decode("utf-8").strip()

            rows = json.loads(body) if body else []

            if not rows:
                print(
                    f"\n[NOAA] No rows returned:\n{url}\n"
                )

            return rows

        except urllib.error.HTTPError as e:

            if e.code in (429, 500, 502, 503, 504):
                last_err = e
                time.sleep(2 * (attempt + 1))
                continue

            raise PrecipFetchError(
                f"HTTP {e.code}"
            ) from e

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as e:

            last_err = e
            time.sleep(2 * (attempt + 1))

    raise PrecipFetchError(
        f"Failed after 3 attempts: {last_err}"
    )


# --------------------------------------------------------------------------- #
# NOAA Rows -> DataFrame
# --------------------------------------------------------------------------- #

def _rows_to_frame(rows: list[dict]) -> pd.DataFrame:

    empty = pd.DataFrame(
        columns=[
            "id",
            "date",
            "prcp_mm",
            "measurement_flag",
            "quality_flag",
            "source_flag",
            "obs_time",
        ]
    )

    if not rows:
        return empty

    df = pd.DataFrame(rows)

    if "PRCP" not in df.columns:
        return empty

    out = pd.DataFrame(
        {
            "id": (
                df["STATION"]
                .astype(str)
                .str.replace(
                    "GHCND:",
                    "",
                    regex=False,
                )
            ),
            "date": pd.to_datetime(df["DATE"]).dt.date,
            "prcp_mm": pd.to_numeric(
                df["PRCP"],
                errors="coerce",
            ),
        }
    )

    if "PRCP_ATTRIBUTES" in df.columns:

        attrs = (
            df["PRCP_ATTRIBUTES"]
            .fillna("")
            .astype(str)
            .str.split(",")
        )

        out["measurement_flag"] = attrs.str[0]
        out["quality_flag"] = attrs.str[1]
        out["source_flag"] = attrs.str[2]
        out["obs_time"] = attrs.str[3]

    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #

def fetch_daily(
    station_ids: Iterable[str],
    start: date,
    end: date,
    progress: ProgressFn | None = None,
    export_raw_csv: bool = True,
) -> pd.DataFrame:

    empty = pd.DataFrame(
        columns=[
            "id",
            "date",
            "prcp_mm",
            "measurement_flag",
            "quality_flag",
            "source_flag",
            "obs_time",
        ]
    )

    ids = sorted(set(station_ids))

    if not ids or start > end:
        return empty

    batches = [
        ids[i : i + BATCH_SIZE]
        for i in range(0, len(ids), BATCH_SIZE)
    ]

    frames = []

    for n, batch in enumerate(batches, start=1):

        rows = _request(
            batch,
            start,
            end,
        )
        
        if rows:
            pd.DataFrame(rows).to_csv(
                "debug/hourly_debug.csv",
                index=False
            )

        
        # debug    
       
        #if rows:
        #    pd.DataFrame(rows).to_csv(
        #        f"debug_batch_{n}.csv",
        #        index=False,
        #    )


        #if export_raw_csv and rows:

        #    pd.DataFrame(rows).to_csv(
        #        f"noaa_batch_{n}.csv",
        #        index=False,
        #    )
        raw = pd.DataFrame(rows)
        if not raw.empty and "STATION" in raw.columns:
                raw[
                    raw["STATION"] == "USW00004205"
                ].to_csv(
                    "debug/USW00004205_raw.csv",
                    index=False
                )


        frames.append(
            _rows_to_frame(rows)
        )
        


        if progress:
            progress(n, len(batches))

    if not frames:
        return empty

    return pd.concat(
        frames,
        ignore_index=True,
    )


def get_precip(
    period: Period,
    station_ids: Iterable[str],
    progress: ProgressFn | None = None,
) -> pd.DataFrame:

    return fetch_daily(
        station_ids,
        period.fetch_start,
        period.fetch_end,
        progress=progress,
    )


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #

def _in_period(
    daily: pd.DataFrame,
    period: Period,
) -> pd.DataFrame:

    return daily[
        (daily["date"] >= period.start)
        & (daily["date"] <= period.end)
    ]


def summarize(
    daily: pd.DataFrame,
    period: Period,
    station_ids: Iterable[str],
) -> pd.DataFrame:

    ids = list(station_ids)

    d = _in_period(daily, period)

    g = d.groupby("id")["prcp_mm"].agg(
        total_mm="sum",
        days_reported="count",
    )

    out = g.reindex(ids)

    out["days_reported"] = (
        out["days_reported"]
        .fillna(0)
        .astype(int)
    )

    out.loc[
        out["days_reported"] == 0,
        "total_mm",
    ] = float("nan")

    out["days_expected"] = period.expected_days

    return out


def daily_matrix(
    daily: pd.DataFrame,
    period: Period,
) -> pd.DataFrame:

    d = _in_period(daily, period)

    wide = d.pivot(
        index="date",
        columns="id",
        values="prcp_mm",
    )

    full = pd.date_range(
        period.start,
        period.end,
        freq="D",
    ).date

    return wide.reindex(full)


def cumulative(
    daily: pd.DataFrame,
    period: Period,
) -> pd.DataFrame:

    return (
        daily_matrix(daily, period)
        .fillna(0)
        .cumsum()
    )


def station_coverage(
    daily: pd.DataFrame,
    period: Period,
) -> pd.DataFrame:

    return (
        daily.groupby("id")
        .agg(
            first_date=("date", "min"),
            last_date=("date", "max"),
            days_reported=("date", "count"),
        )
        .assign(
            pct_reported=lambda x:
            100 * x["days_reported"] /
            period.expected_days
        )
        .sort_values("pct_reported")
    )


def mm_to_inches(mm):
    return mm / MM_PER_INCH
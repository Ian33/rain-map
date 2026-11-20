"""
Rain Map: Dash app.

Pages
    /         map of NOAA stations inside the WRIA boundary (side panel on the left)
    /second   placeholder page

Side panel
    Network        dropdown (All or one network)
    Parameters     match-all filter on the parameters a station reports, with an
                   option to show non-matching stations as small grey markers
    Precipitation  choose a period, press "Get precipitation" to query NOAA
                   (nothing is queried at start-up), choose units, and optionally
                   print the total next to each station

When precipitation has been queried, marker fill = precipitation total on a color
scale (outline keeps the network color) and the total appears in the hover box.
The map is never rebuilt: filters and queries only swap the station markers, so
your zoom and pan stay put.

Expects (relative to this file):
    data/noaa_stations_in_wria.csv                  from helper_scripts/get_noaa_stations.py
    precip_data.py                                  next to this file
    WRIA_boundaries_derived_from_terrain_data.shp   searched for under this folder


Needs:  pip install dash dash-leaflet geopandas pandas
Run:    python rain_map_app.py     then open http://127.0.0.1:8050
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import dash_leaflet as dl
from dash import Dash, Input, Output, State, dcc, html, no_update

from helper_scripts import precip_data as pdata

# --------------------------------------------------------------------------- #
# Paths (PyInstaller-friendly)
# --------------------------------------------------------------------------- #
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = BASE_DIR / "data"
CSV_PATH = DATA_DIR / "noaa_stations_in_wria.csv"
SHAPEFILE_NAME = "WRIA_boundaries_derived_from_terrain_data.shp"
# Under PyInstaller _MEIPASS is temporary, so cache next to the exe instead

ELEMENT_SEP = "; "
SIDEBAR_WIDTH = "350px"
OUTLINE_SIMPLIFY_DEG = 0.0003            # ~30 m; keeps the browser fast

# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def load_outline() -> gpd.GeoDataFrame:
    shp = next(BASE_DIR.rglob(SHAPEFILE_NAME), None)
    if shp is None:
        raise FileNotFoundError(f"Could not find {SHAPEFILE_NAME} under {BASE_DIR}")
    g = gpd.read_file(shp).to_crs(4326)
    g["geometry"] = g.geometry.buffer(0)
    out = gpd.GeoDataFrame(geometry=g.dissolve().geometry, crs=g.crs)
    out["geometry"] = out.geometry.simplify(OUTLINE_SIMPLIFY_DEG, preserve_topology=True)
    return out


def load_stations() -> pd.DataFrame:
    if not CSV_PATH.exists():
        raise FileNotFoundError(
            f"{CSV_PATH} not found. Run helper_scripts/get_noaa_stations.py first.")
    df = pd.read_csv(CSV_PATH, dtype={"id": str, "network_code": str, "wmo_id": str,
                                      "gsn_flag": str, "hcn_crn_flag": str})
    df["elements"] = df["elements"].fillna("")
    df["element_set"] = df["elements"].apply(
        lambda s: set(filter(None, s.split(ELEMENT_SEP))))
    return df


OUTLINE = load_outline()
OUTLINE_GEOJSON = json.loads(OUTLINE.to_json())
minx, miny, maxx, maxy = OUTLINE.total_bounds
BOUNDS = [[miny, minx], [maxy, maxx]]

STATIONS = load_stations()
WRIA_COLS = [c for c in STATIONS.columns if c.startswith("wria_") and c != "wria_feature_idx"]

PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
           "#008080", "#9a6324", "#800000", "#808000", "#000075"]
NETWORKS = sorted(STATIONS["network"].dropna().unique())
NETWORK_COLORS = {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(NETWORKS)}

_counts: dict[str, int] = {}
for _s in STATIONS["element_set"]:
    for _e in _s:
        _counts[_e] = _counts.get(_e, 0) + 1
PARAM_OPTIONS = [{"label": f"{name} ({n})", "value": name}
                 for name, n in sorted(_counts.items(), key=lambda kv: (-kv[1], kv[0]))]

# --------------------------------------------------------------------------- #
# Precipitation color scale (light -> dark)
# --------------------------------------------------------------------------- #
SCALE = ["#ffffcc", "#a1dab4", "#41b6c4", "#2c7fb8", "#253494"]


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def scale_color(t: float) -> str:
    """Color for t in [0, 1] along SCALE (linear blend between stops)."""
    t = min(max(t, 0.0), 1.0)
    pos = t * (len(SCALE) - 1)
    i = min(int(pos), len(SCALE) - 2)
    frac = pos - i
    a, b = _hex_to_rgb(SCALE[i]), _hex_to_rgb(SCALE[i + 1])
    return "#%02x%02x%02x" % tuple(round(a[k] + (b[k] - a[k]) * frac) for k in range(3))


# --------------------------------------------------------------------------- #
# Logic (kept out of the callbacks so it can be tested on its own)
# --------------------------------------------------------------------------- #
def filter_stations(network: str | None, params: list[str] | None,
                    match_params: bool = True) -> pd.DataFrame:
    df = STATIONS
    if network and network != "All":
        df = df[df["network"] == network]
    if params and match_params:
        want = set(params)
        df = df[df["element_set"].apply(lambda s: want <= s)]      # match ALL
    return df


def run_precip_query(period_key: str, network: str | None, params: list[str] | None) -> dict:
    periods = pdata.build_periods(date.today())
    period = periods[period_key]

    ids = filter_stations(network, params)["id"].tolist()

    daily = pdata.get_precip(period, ids)
    # DEBUG 

    
    
    daily[
        daily["id"] == "USW00004205"
    ].to_csv(
        "debug/USW00004205_processed.csv",
        index=False
    )

    summary = pdata.summarize(daily, period, ids)

    totals = {
        sid: {
            "mm": None if pd.isna(r.total_mm) else float(r.total_mm),
            "days": int(r.days_reported),
            "expected": int(r.days_expected),
        }
        for sid, r in summary.iterrows()
    }

    return {
        "period": period_key,
        "label": period.label,
        "queried_at": datetime.now().strftime("%b %d %H:%M"),
        "n_requested": len(ids),
        "totals": totals,
    }

def fmt_precip(mm: float, unit: str) -> str:
    return f"{pdata.mm_to_inches(mm):.2f} in" if unit == "in" else f"{mm:.1f} mm"


def fmt_precip_bare(mm: float, unit: str) -> str:
    return f"{pdata.mm_to_inches(mm):.2f}" if unit == "in" else f"{mm:.1f}"


def tooltip_children(row: pd.Series, precip: dict | None, unit: str, queried: bool):
    def fmt(v):
        if pd.isna(v) or v == "":
            return None
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v)

    rows: list[tuple[str, str | None]] = []
    if queried:
        if precip and precip["mm"] is not None:
            rows.append(("Precipitation",
                         f"{fmt_precip(precip['mm'], unit)} "
                         f"({precip['days']} of {precip['expected']} days reported)"))
        else:
            rows.append(("Precipitation", "no data reported for this period"))
    rows += [
        ("Station ID", fmt(row["id"])),
        ("Name", fmt(row["name"])),
        ("Network", fmt(row["network"])),
        ("Latitude", f"{row['lat']:.5f}"),
        ("Longitude", f"{row['lon']:.5f}"),
        ("Elevation (m)", fmt(row.get("elev_m"))),
        ("State", fmt(row.get("state"))),
        ("WMO ID", fmt(row.get("wmo_id"))),
        ("Record", f"{fmt(row.get('data_first_year'))} to {fmt(row.get('data_last_year'))}"
         if fmt(row.get("data_first_year")) else None),
        ("Active", "Yes" if str(row.get("active")) == "True" else "No"),
        ("Parameters", fmt(row.get("elements"))),
    ]
    for col in WRIA_COLS:
        rows.append((col.removeprefix("wria_"), fmt(row.get(col))))

    cell = {"padding": "1px 8px 1px 0", "verticalAlign": "top", "fontSize": "12px"}
    body = [html.Tr([html.Td(html.B(k), style={**cell, "color": "#555"}),
                     html.Td(v, style={**cell, "whiteSpace": "normal",
                                       "wordBreak": "break-word"})])
            for k, v in rows if v is not None]
    return html.Div(html.Table(html.Tbody(body), style={"width": "100%"}),
                    style={"width": "320px", "whiteSpace": "normal"})


def build_station_layers(network, params, show_grey, store, unit, show_labels):
    """Return (station markers, label markers, count text, colorbar children)."""
    matching = filter_stations(network, params)
    marker_children: list = []
    label_children: list = []

    queried = bool(store and store.get("totals"))
    totals = store["totals"] if queried else {}

    # color range from the stations that are on screen with data
    vals = [totals[i]["mm"] for i in matching["id"]
            if i in totals and totals[i]["mm"] is not None]
    vmax_mm = max(vals) if vals else 0.0

    # grey, non-matching stations (drawn first so they sit underneath)
    n_grey = 0
    if params and show_grey:
        in_network = filter_stations(network, params, match_params=False)
        grey = in_network[~in_network["id"].isin(matching["id"])]
        n_grey = len(grey)
        for _, r in grey.iterrows():
            marker_children.append(dl.CircleMarker(
                center=[r["lat"], r["lon"]], radius=3, weight=1, color="#8a8a8a",
                fillColor="#b5b5b5", fillOpacity=0.8,
                children=dl.Tooltip(
                    f"{r['name']} ({r['id']}): does not report all selected parameters",
                    sticky=True)))

    for _, r in matching.iterrows():
        p = totals.get(r["id"])
        has_val = bool(p and p["mm"] is not None)
        if queried and has_val:
            t = p["mm"] / vmax_mm if vmax_mm > 0 else 0.0
            fill, fill_op, dash = scale_color(t), 0.95, None
        elif queried:                                   # queried, nothing reported
            fill, fill_op, dash = "#ffffff", 0.9, "2 3"
        else:                                           # not queried yet
            fill, fill_op, dash = NETWORK_COLORS.get(r["network"], "#888888"), 0.9, None
        outline = NETWORK_COLORS.get(r["network"], "#222222") if queried else "#222222"
        marker_children.append(dl.CircleMarker(
            center=[r["lat"], r["lon"]], radius=7, weight=2 if queried else 1,
            color=outline, fillColor=fill, fillOpacity=fill_op, dashArray=dash,
            children=dl.Tooltip(tooltip_children(r, p, unit, queried), sticky=True,
                                className="station-tip")))

        if show_labels and has_val:
            label_children.append(dl.CircleMarker(
                center=[r["lat"], r["lon"]], radius=1, stroke=False, fill=False,
                interactive=False,
                children=dl.Tooltip(fmt_precip_bare(p["mm"], unit),
                                    permanent=True, direction="right", offset=[9, 0],
                                    className="precip-label", interactive=False)))

    count = f"Showing {len(matching)} of {len(STATIONS)} stations"
    if n_grey:
        count += f" (+{n_grey} grey)"

    colorbar = []
    if queried and vals:
        top = pdata.mm_to_inches(vmax_mm) if unit == "in" else vmax_mm
        dec = 2 if unit == "in" else 1
        gradient = "linear-gradient(to right, " + ", ".join(SCALE) + ")"
        ticks = [f"{top * f:.{dec}f}" for f in (0, 0.25, 0.5, 0.75, 1.0)]
        colorbar = [
            html.Div(store["label"], style={"fontWeight": "600", "fontSize": "11px",
                                            "maxWidth": "240px", "marginBottom": "4px"}),
            html.Div(style={"height": "12px", "width": "240px", "background": gradient,
                            "border": "1px solid #999"}),
            html.Div([html.Span(t) for t in ticks],
                     style={"display": "flex", "justifyContent": "space-between",
                            "width": "240px", "fontSize": "11px"}),
            html.Div(f"Total precipitation ({unit})", style={"fontSize": "11px", "color": "#555"}),
        ]
    return marker_children, label_children, count, colorbar


# --------------------------------------------------------------------------- #
# Layouts
# --------------------------------------------------------------------------- #
LABEL_STYLE = {"fontWeight": "600", "fontSize": "13px", "margin": "16px 0 4px"}


def legend() -> html.Div:
    dot = {"display": "inline-block", "width": "12px", "height": "12px", "borderRadius": "50%",
           "border": "1px solid #222", "marginRight": "8px"}
    items = [html.Div([html.Span(style={**dot, "background": NETWORK_COLORS[n]}),
                       html.Span(n, style={"fontSize": "12px"})], style={"margin": "3px 0"})
             for n in NETWORKS]
    items.append(html.Div("After a precipitation query, the fill shows the total and the "
                          "outline shows the network. A dashed white marker means no data "
                          "was reported.", style={"fontSize": "11px", "color": "#555",
                                                  "marginTop": "6px"}))
    return html.Div(items)


def map_layout() -> html.Div:
    periods = pdata.build_periods(date.today())
    period_options = [{"label": p.label, "value": k} for k, p in periods.items()]
    network_options = [{"label": f"All networks ({len(STATIONS)})", "value": "All"}] + [
        {"label": f"{n} ({(STATIONS['network'] == n).sum()})", "value": n} for n in NETWORKS]

    sidebar = html.Div([
        html.H2("Rain Map", style={"margin": "0 0 2px"}),
        html.Div("NOAA stations in the WRIA boundary", style={"fontSize": "12px", "color": "#555"}),
        html.Div(dcc.Link("Second page", href="/second"),
                 style={"marginTop": "8px", "fontSize": "13px"}),

        html.Div("Network", style=LABEL_STYLE),
        dcc.Dropdown(id="network-dd", options=network_options, value="All", clearable=False),

        html.Div("Parameters (station must report all selected)", style=LABEL_STYLE),
        dcc.Dropdown(id="param-dd", options=PARAM_OPTIONS, value=[], multi=True,
                     placeholder="Any parameter"),
        dcc.Checklist(id="grey-chk",
                      options=[{"label": " Show non-matching stations as small grey markers",
                                "value": "grey"}], value=[],
                      style={"fontSize": "12px", "marginTop": "6px"}),

        html.Div(id="station-count",
                 style={"margin": "14px 0 2px", "fontSize": "13px", "fontWeight": "600"}),
        html.Hr(),

        html.Div("Precipitation", style={**LABEL_STYLE, "marginTop": "4px"}),
        dcc.Dropdown(id="period-dd", options=period_options, value="last24", clearable=False),
        html.Div([
            html.Button("Get precipitation", id="query-btn", n_clicks=0,
                        style={"padding": "6px 12px", "cursor": "pointer"}),
            dcc.RadioItems(id="unit-radio", options=[{"label": " in", "value": "in"},
                                                     {"label": " mm", "value": "mm"}],
                           value="in", inline=True,
                           style={"fontSize": "13px", "marginLeft": "16px"},
                           labelStyle={"marginRight": "10px"}),
        ], style={"display": "flex", "alignItems": "center", "marginTop": "8px"}),
        dcc.Loading(html.Div(id="precip-status",
                             children="Nothing queried yet. Pick a period and press the button.",
                             style={"fontSize": "12px", "color": "#444", "marginTop": "8px",
                                    "minHeight": "34px"}),
                    type="dot"),
        dcc.Checklist(id="labels-chk", options=[{"label": " Show totals next to stations",
                                                 "value": "labels"}], value=[],
                      style={"fontSize": "13px", "marginTop": "4px"}),
        html.Hr(),
        html.Div("Legend", style={**LABEL_STYLE, "marginTop": "4px"}),
        legend(),
    ], style={"width": SIDEBAR_WIDTH, "flex": f"0 0 {SIDEBAR_WIDTH}", "boxSizing": "border-box",
              "padding": "16px", "overflowY": "auto", "background": "#f5f7fa",
              "borderRight": "1px solid #d0d7de", "fontFamily": "sans-serif"})

    map_panel = html.Div([
        dl.Map(
            id="map", bounds=BOUNDS, style={"width": "100%", "height": "100%"},
            children=[
                dl.TileLayer(),
                dl.GeoJSON(data=OUTLINE_GEOJSON,
                           style={"color": "#1f4e79", "weight": 3, "fillOpacity": 0.05}),
                dl.LayerGroup(id="station-layer"),
                dl.LayerGroup(id="label-layer"),
            ]),
        html.Div(id="colorbar", style={
            "position": "absolute", "right": "12px", "bottom": "28px", "zIndex": 1000,
            "background": "rgba(255,255,255,0.92)", "padding": "8px 10px",
            "borderRadius": "4px", "boxShadow": "0 1px 4px rgba(0,0,0,0.3)",
            "fontFamily": "sans-serif", "display": "none"}),
    ], style={"flex": "1", "minWidth": "0", "height": "100%", "position": "relative"})

    return html.Div([sidebar, map_panel],
                    style={"display": "flex", "height": "100vh", "width": "100vw"})


def second_layout() -> html.Div:
    return html.Div([html.H2("Second page"), html.P("Placeholder page."),
                     dcc.Link("Back to the map", href="/")],
                    style={"padding": "24px", "fontFamily": "sans-serif"})


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = Dash(__name__, suppress_callback_exceptions=True)
app.index_string = app.index_string.replace(
    "</head>",
    "<style>html,body{margin:0;padding:0;height:100%}"
    ".precip-label{background:rgba(255,255,255,0.85);border:none;box-shadow:none;"
    "padding:0 3px;font:600 11px sans-serif;color:#111}"
    ".precip-label:before{display:none}"
    ".station-tip{white-space:normal;padding:6px 8px}</style></head>")
app.layout = html.Div([dcc.Location(id="url"),
                       dcc.Store(id="precip-store"),
                       html.Div(id="page-content")])


@app.callback(Output("page-content", "children"), Input("url", "pathname"))
def route(pathname):
    return second_layout() if pathname == "/second" else map_layout()


@app.callback(
    Output("precip-store", "data"),
    Output("precip-status", "children"),
    Input("query-btn", "n_clicks"),
    State("period-dd", "value"),
    State("network-dd", "value"),
    State("param-dd", "value"),
    prevent_initial_call=True,
    running=[(Output("query-btn", "disabled"), True, False)],
)
def query_precip(n_clicks, period_key, network, params):
    try:
        result = run_precip_query(period_key, network, params)
    except pdata.PrecipFetchError as e:
        return no_update, f"Query failed: {e}"
    with_data = sum(1 for v in result["totals"].values() if v["mm"] is not None)
    status = (f"{result['label']}: {with_data} of {result['n_requested']} stations reported "
              f"data (queried {result['queried_at']}).")
    if with_data == 0:
        status += " NOAA returned no rows, so nothing was cached; see the console for the request URL."
    return result, status


@app.callback(
    Output("station-layer", "children"),
    Output("label-layer", "children"),
    Output("station-count", "children"),
    Output("colorbar", "children"),
    Output("colorbar", "style"),
    Input("network-dd", "value"),
    Input("param-dd", "value"),
    Input("grey-chk", "value"),
    Input("precip-store", "data"),
    Input("unit-radio", "value"),
    Input("labels-chk", "value"),
    State("colorbar", "style"),
)
def update_map(network, params, grey, store, unit, labels, cbar_style):
    markers, label_markers, count, colorbar = build_station_layers(
        network, params, bool(grey), store, unit, bool(labels))
    style = dict(cbar_style or {})
    style["display"] = "block" if colorbar else "none"
    return markers, label_markers, count, colorbar, style


if __name__ == "__main__":
    app.run(debug=True)
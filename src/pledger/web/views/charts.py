"""charts.py: the SVG charts, their axes and their real (deflated) lines."""

from __future__ import annotations

import datetime as dt
import math

from pandas import Series, Timestamp


def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """Round axis ticks covering [lo, hi]."""
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    ticks = [math.floor(lo / step) * step]
    while ticks[-1] < hi - 1e-9:
        ticks.append(ticks[-1] + step)
    return ticks


def compact(value: float) -> str:
    """Short axis label, e.g. 1.5M, 400K, -20."""
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= div:
            return f"{value / div:.3g}{suffix}"
    return f"{value:.3g}"


def line_chart(series: list[dict], tick_label=compact) -> dict | None:
    """
    Points, path and axes to draw a line of {date, value} points (a None value
    breaks the line). Coordinates are percentages of the plot area, so HTML
    labels and the SVG line share them. Points with a `real` value (see with_real)
    also get a second, dashed line of those, on the same axes.
    """
    shown = [p for p in series if p["value"] is not None]
    if not shown:
        return None
    reals = [p["real"] for p in shown if p.get("real") is not None]
    values = [p["value"] for p in shown] + reals
    ticks = nice_ticks(min(values), max(values))
    y_lo, y_hi = ticks[0], ticks[-1]

    def y(v: float) -> float:
        return round(100 * (1 - (v - y_lo) / (y_hi - y_lo)), 2)

    x, x_ticks = time_axis([dt.date.fromisoformat(p["date"]) for p in series])
    points, path, joined = [], [], False
    real_path, real_joined, real_last = [], False, None
    for p in series:
        if p["value"] is None:
            joined = real_joined = False
            continue
        point = {**p, "x": x(p["date"]), "y": y(p["value"])}
        points.append(point)
        path.append(f"{'L' if joined else 'M'}{point['x']},{point['y']}")
        joined = True
        if p.get("real") is None:
            real_joined = False
            continue
        real_last = {"x": point["x"], "y": y(p["real"])}
        real_path.append(f"{'L' if real_joined else 'M'}{real_last['x']},{real_last['y']}")
        real_joined = True
    return {
        "path": " ".join(path),
        "points": points,
        "y_ticks": [{"y": y(t), "label": tick_label(t)} for t in ticks],
        "x_ticks": x_ticks,
        "last": points[-1],
        "real_path": " ".join(real_path) if reals else None,
        "real_last": real_last,
    }


def with_real(series: list[dict], cpi: Series | None, dates: list[str] | None = None) -> str | None:
    """
    Add to each {date, value} point its `real` value: deflated by the CPI of its
    date (or of its date in `dates`), in the base currency of the last of those
    dates with a CPI, which is returned (see real_values). None where there is no
    CPI, and everywhere without one.
    """
    for p in series:
        p["real"] = None
    if cpi is None or not len(cpi) or not series:
        return None
    index = [Timestamp(d) for d in (dates or [p["date"] for p in series])]
    values = Series([p["value"] if p["value"] is not None else math.nan for p in series], index=index)
    real, real_date = real_values(values, cpi)
    for p, r in zip(series, real):
        p["real"] = None if math.isnan(r) else float(r)
    return real_date if any(p["real"] is not None for p in series) else None


def column_chart(series: list[dict], tick_label=compact) -> dict | None:
    """
    Columns and axes to draw a {month, value} series, one band per month, up from
    0 (or down, for a negative value). Coordinates are percentages of the plot
    area. None if every value is 0.
    """
    lo = min(0.0, *(p["value"] for p in series))
    hi = max(0.0, *(p["value"] for p in series))
    if hi - lo < 1e-9:
        return None
    ticks = nice_ticks(lo, hi)

    def y(v: float) -> float:
        return round(100 * (1 - (v - ticks[0]) / (ticks[-1] - ticks[0])), 2)

    band = 100 / len(series)
    every = math.ceil(len(series) / 8)
    columns = [
        {
            **p,
            "x": round(band * (i + 0.5), 2),
            "y": y(p["value"]),
            "top": y(max(p["value"], 0.0)),
            "height": round(abs(y(p["value"]) - y(0.0)), 2),
            "negative": p["value"] < 0,
        }
        for i, p in enumerate(series)
    ]
    return {
        "columns": columns,
        "width": round(band * 0.7, 2),  # the rest of the band is the gap between columns
        "y_ticks": [{"y": y(t), "label": tick_label(t), "axis": abs(t) < 1e-9} for t in ticks],
        "x_ticks": [
            {"x": c["x"], "label": dt.date.fromisoformat(f"{c['month']}-01").strftime("%b %Y")}
            for c in columns[::every]
        ],
        "last": columns[-1],
    }


def waterfall_chart(statement: list[dict], tick_label=compact) -> dict | None:
    """
    Floating columns to draw a result statement as a waterfall: the gains of each
    result type up from the running total, then its losses down from there, in
    statement order, and the profit from 0. Coordinates are percentages of the
    plot area. None if every gain and loss is 0.
    """
    steps, running = [], 0.0
    for r in statement:
        for kind, value in (("gain", r["gains"]), ("loss", r["losses"])):
            steps.append({
                "name": r["name"], "short": r["short"], "kind": kind,
                "value": value, "start": running, "end": running + value,
            })
            running += value
    if not any(abs(s["value"]) > 1e-9 for s in steps):
        return None
    steps.append({"name": "Profit", "short": "Profit", "kind": "total", "value": running, "start": 0.0, "end": running})
    ticks = nice_ticks(min(0.0, *(s["end"] for s in steps)), max(0.0, *(s["end"] for s in steps)))

    def y(v: float) -> float:
        return round(100 * (1 - (v - ticks[0]) / (ticks[-1] - ticks[0])), 2)

    band = 100 / len(steps)
    columns = [
        {
            **s,
            "x": round(band * (i + 0.5), 2),
            "y": y(s["end"]),
            "top": y(max(s["start"], s["end"])),
            "height": round(abs(y(s["start"]) - y(s["end"])), 2),
        }
        for i, s in enumerate(steps)
    ]
    return {
        "columns": columns,
        "width": round(band * 0.7, 2),  # the rest of the band is the gap between columns
        "y_ticks": [{"y": y(t), "label": tick_label(t), "axis": abs(t) < 1e-9} for t in ticks],
        # Each result type labelled under its pair of columns, and the profit under its own
        "x_ticks": [
            {"x": round((a["x"] + b["x"]) / 2, 2), "label": a["short"]}
            for a, b in zip(columns[:-1:2], columns[1:-1:2])
        ] + [{"x": columns[-1]["x"], "label": "Profit"}],
        "last": columns[-1],
    }


def multi_line_chart(lines: list[dict], value_text=lambda v: f"{v:,.2f}") -> dict | None:
    """
    Paths and axes to draw several lines of {date, value} points (a None value
    breaks a line) on one y axis. `lines` are {name, series, ...} dicts, returned
    with their path and last point. The tooltip of each date lists the lines with a value on it.
    """
    shown = [p["value"] for line in lines for p in line["series"] if p["value"] is not None]
    if not shown:
        return None
    ticks = nice_ticks(min(shown), max(shown))

    def y(v: float) -> float:
        return round(100 * (1 - (v - ticks[0]) / (ticks[-1] - ticks[0])), 2)

    dates = sorted({p["date"] for line in lines for p in line["series"]})
    x, x_ticks = time_axis([dt.date.fromisoformat(d) for d in dates])
    drawn, values = [], {}
    for line in lines:
        path, joined, last = [], False, None
        for p in line["series"]:
            if p["value"] is None:
                joined = False
                continue
            values[line["name"], p["date"]] = p["value"]
            last = {"x": x(p["date"]), "y": y(p["value"])}
            path.append(f"{'L' if joined else 'M'}{last['x']},{last['y']}")
            joined = True
        drawn.append({**line, "path": " ".join(path), "last": last})
    points = []
    for d in dates:
        on_date = [values.get((line["name"], d)) for line in lines]
        shown_y = next((y(v) for v in on_date if v is not None), None)
        points.append({
            "x": x(d), "y": shown_y, "date": d,
            "rows": [[line["name"], value_text(v)] for line, v in zip(lines, on_date) if v is not None],
        })
    return {
        "lines": drawn,
        "points": points,
        "y_ticks": [{"y": y(t), "label": compact(t)} for t in ticks],
        "x_ticks": x_ticks,
    }


def time_axis(dates: list[dt.date]) -> tuple:
    """
    The x of an ISO date (a percentage, from the first to the last date) and the
    ticks: the start of each month, or the first and last day within a single month.
    """
    first, last = dates[0], dates[-1]
    span = max((last - first).days, 1)

    def x(d: dt.date | str) -> float:
        d = dt.date.fromisoformat(d) if isinstance(d, str) else d
        return 50.0 if first == last else round(100 * (d - first).days / span, 2)

    month_starts = [
        dt.date(yr, mo, 1)
        for yr in range(first.year, last.year + 1)
        for mo in range(1, 13)
        if first <= dt.date(yr, mo, 1) <= last
    ]
    year_starts = [m for m in month_starts if m.month == 1]
    if len(year_starts) >= 3:  # a long span: the start of each year, labelled by its year
        every = math.ceil(len(year_starts) / 8)
        x_ticks = [{"x": x(m), "label": m.strftime("%Y")} for m in year_starts[::every]]
    elif len(month_starts) >= 2:
        every = math.ceil(len(month_starts) / 8)
        x_ticks = [{"x": x(m), "label": m.strftime("%b %Y")} for m in month_starts[::every]]
    else:
        x_ticks = [{"x": x(d), "label": d.strftime("%b %d")} for d in sorted({first, last})]
    return x, x_ticks


def stacked_chart(monthly: list[tuple[str, dict[str, float]]], groups: dict[str, list[str]]) -> dict | None:
    """
    A 100% stacked area of the month-end balances of each group of accounts (in
    order, the first one at the bottom), as SVG paths in plot percentages.
    """
    months = [(m, {g: sum(max(b.get(a, 0.0), 0.0) for a in accs) for g, accs in groups.items()})
              for m, b in monthly]
    months = [(m, totals) for m, totals in months if sum(totals.values()) > 1e-9]
    if not months:
        return None
    x, x_ticks = time_axis([dt.date.fromisoformat(f"{m}-01") for m, _ in months])
    xs = [x(f"{m}-01") for m, _ in months]
    shares = [{g: v / sum(totals.values()) for g, v in totals.items()} for _, totals in months]
    if len(xs) == 1:  # a single month: a full-width band
        xs, shares = [0.0, 100.0], shares * 2
    areas, below = [], [0.0] * len(xs)
    for slot, group in enumerate(groups):
        above = [b + s[group] for b, s in zip(below, shares)]
        top = [f"{px},{round(100 * (1 - v), 2)}" for px, v in zip(xs, above)]
        bottom = [f"{px},{round(100 * (1 - v), 2)}" for px, v in zip(xs, below)][::-1]
        areas.append({
            "name": group, "slot": slot % 8 + 1,
            "path": "M" + " L".join(top + bottom) + " Z",
            "last": shares[-1][group],
        })
        below = above
    return {
        "areas": areas,
        "points": [
            {"x": x(f"{m}-01"), "date": m, "rows": [[g, f"{100 * v / sum(t.values()):.1f}%"] for g, v in t.items()][::-1]}
            for m, t in months
        ],
        "y_ticks": [{"y": 100 - t, "label": f"{t}%"} for t in (0, 25, 50, 75, 100)],
        "x_ticks": x_ticks,
        "first": months[0][0],
        "last": months[-1][0],
    }


MAX_CHART_POINTS = 400  # long daily series are sampled down to about this many points to draw


def sampled(values: Series, limit: int = MAX_CHART_POINTS) -> Series:
    """Every k-th value of a series, so there are at most about `limit`, always with the last one."""
    step = math.ceil(len(values) / limit) if len(values) > limit else 1
    picked = list(range(0, len(values), step))
    if picked and picked[-1] != len(values) - 1:
        picked.append(len(values) - 1)
    return values.iloc[picked]


def real_values(values: Series, cpi: Series) -> tuple[Series, str | None]:
    """
    Values in the base currency deflated by the CPI of their dates, in the base
    currency of the last of their dates with a CPI (also returned). NaN on the dates
    without a CPI.
    """
    on_dates = cpi.reindex(values.index)
    known = on_dates.dropna()
    if not len(known):
        return values * math.nan, None
    return values * known.iloc[-1] / on_dates, known.index[-1].date().isoformat()

"""Regenerate the live block in README.md.

Two sources, no API keys, no dependencies beyond the standard library:

    weather   Open-Meteo   current conditions, and 11 days of daylight
    football  Wikipedia    the league table, three rows around NAC Breda

If anything fails the script exits non-zero without touching README.md, so a
broken run leaves the last good block in place rather than publishing a wrong
number. The box is measured before it is written, for the same reason.

    python3 update.py
"""

import html
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# --- configuration ---------------------------------------------------------

CITY, LAT, LON, TZ = "Stockholm", 59.3293, 18.0686, "Europe/Stockholm"

TEAM = "NAC Breda"
# Dutch seasons run August to May, so the Wikipedia page name rolls over in
# July. Promotion or relegation changes the competition but not the format:
# the script fails loudly when TEAM is missing, which is the cue to edit this.
LEAGUE = "Eerste Divisie"
COLUMNS = ("Pos", "Team", "Pld", "GD", "Pts")

README = Path(__file__).resolve().parent / "README.md"
START, END = "<!-- live:start -->", "<!-- live:end -->"

SPARK = "▁▂▃▄▅▆▇█"
MINUS = "−"  # U+2212: one cell wide, and sits at digit height

WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


class UpdateError(RuntimeError):
    """A source could not be read, or returned something unusable."""


# --- monospace width -------------------------------------------------------


def width(text: str) -> int:
    """Cells the text occupies. len() is wrong: accents take none, and some
    characters take two. Every pad in this file goes through here."""
    cells = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        cells += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return cells


def clip(text: str, cells: int) -> str:
    """Truncate to a cell budget, so a long name cannot shift the columns."""
    if width(text) <= cells:
        return text
    out = ""
    for char in text:
        if width(out + char) > cells - 1:
            break
        out += char
    return out.rstrip() + "…"


# --- sources ---------------------------------------------------------------


def fetch(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "jorisrombouts-profile"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # The body usually says why, which beats the status code alone.
        raise UpdateError(f"{url} — HTTP {exc.code}: {exc.read().decode('utf-8')[:120]}") from exc
    except OSError as exc:  # URLError and TimeoutError are both OSError
        raise UpdateError(f"{url} — {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"{url} — not JSON: {body[:120]!r}") from exc


def weather() -> dict:
    """Current conditions, plus a daylight sparkline and its trend."""
    data = fetch(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&current=temperature_2m,weather_code&daily=daylight_duration"
        f"&timezone={urllib.parse.quote(TZ)}&past_days=10&forecast_days=1"
    )
    try:
        now = data["current"]
        series = [v for v in data["daily"]["daylight_duration"] if v is not None]
    except (KeyError, TypeError) as exc:
        raise UpdateError(f"open-meteo: unexpected response — {exc}") from exc
    if len(series) < 2:
        raise UpdateError("open-meteo: not enough history for a sparkline")

    low, span = min(series), (max(series) - min(series)) or 1
    seconds = series[-1]
    per_day = (seconds - series[0]) / 60 / (len(series) - 1)

    return {
        "temp": round(now["temperature_2m"], 1),
        "sky": WMO.get(now["weather_code"], "unsettled"),
        "spark": "".join(SPARK[min(7, int((v - low) / span * 7.999))] for v in series),
        "daylight": f"{int(seconds // 3600)}h {int(seconds % 3600 // 60):02d}m",
        "trend": f"{MINUS if per_day < 0 else '+'}{abs(per_day):.1f} min/day",
    }


def season_page() -> str:
    """The Wikipedia page for the season now in progress."""
    now = datetime.now(ZoneInfo(TZ))  # not the runner's UTC
    start = now.year if now.month >= 7 else now.year - 1
    return f"{start}–{(start + 1) % 100:02d} {LEAGUE}"


def cells(row_html: str) -> list[str]:
    """The text of one table row's cells, tags and entities resolved."""
    return [
        html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).strip())
        for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.DOTALL)
    ]


def league_table(page_html: str) -> list[dict]:
    """Every row of the standings table, keyed by column name.

    The table is recognised by its header row rather than its position, and
    the columns are read by name, so neither a new section above it nor a new
    column inside it can silently shift the numbers.
    """
    for table in re.findall(r"<table[^>]*>.*?</table>", page_html, re.DOTALL):
        rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.DOTALL)
        header = {name: i for i, name in enumerate(cells(rows_html[0]))} if rows_html else {}
        if set(COLUMNS) <= header.keys():
            break
    else:
        raise UpdateError("wikipedia: no table with a standings header")

    last = max(header[name] for name in COLUMNS)
    return [
        {name: row[header[name]] for name in COLUMNS}
        for row in map(cells, rows_html[1:])
        if len(row) > last and row[0].isdigit()
    ]


def standings() -> list[dict]:
    """Three table rows centred on TEAM."""
    page = season_page()
    data = fetch(
        "https://en.wikipedia.org/w/api.php?action=parse"
        f"&page={urllib.parse.quote(page)}&prop=text&format=json&formatversion=2"
    )
    if "error" in data:
        raise UpdateError(f"wikipedia: {page} — {data['error'].get('info')}")

    rows = league_table(data["parse"]["text"])
    here = next((i for i, row in enumerate(rows) if TEAM.lower() in row["Team"].lower()), None)
    if here is None:
        raise UpdateError(f"wikipedia: {TEAM} is not in the {page} table — wrong division?")

    # Three rows centred on TEAM, shifted inwards at either end of the table.
    top = max(0, min(here - 1, len(rows) - 3))
    window = rows[top : top + 3]
    for row in window:
        row["here"] = row is rows[here]
        row["GD"] = row["GD"].replace("-", MINUS)
    return window


# --- rendering -------------------------------------------------------------

# One template for every table row, so the header and the numbers below it
# cannot drift apart. The 24-cell left column holds either the league name or
# a right-aligned position plus a team.
ROW = "  {left:<24}{pld:>3}{gd:>6}{pts:>6}{mark}"
LABEL = "  {0:<13}{1}"


def render(sky: dict, table: list[dict]) -> str:
    lines = [
        LABEL.format(CITY, f"{sky['temp']} °C, {sky['sky']}"),
        LABEL.format("", f"{sky['spark']}  {sky['daylight']}   {sky['trend']}"),
        "",
        ROW.format(left=LEAGUE, pld="P", gd="GD", pts="Pts", mark=""),
    ]
    lines += [
        ROW.format(
            left=clip(f"{row['Pos']:>3}  {row['Team']}", 24),
            pld=row["Pld"],
            gd=row["GD"],
            pts=row["Pts"],
            mark="   ←" if row["here"] else "",
        )
        for row in table
    ]

    inner = max(width(line) for line in lines) + 2
    box = (
        ["╭" + "─" * inner + "╮"]
        + ["│" + line + " " * (inner - width(line)) + "│" for line in lines]
        + ["╰" + "─" * inner + "╯"]
    )

    uneven = {width(line) for line in box}
    if len(uneven) != 1:
        raise UpdateError(f"box is not square: widths {sorted(uneven)}")
    return "\n".join(box)


def write(block: str) -> bool:
    before = README.read_text(encoding="utf-8")
    if START not in before or END not in before:
        raise UpdateError(f"markers {START} / {END} not found in {README.name}")

    head, tail = before.split(START)[0], before.split(END)[-1]
    after = f"{head}{START}\n```\n{block}\n```\n{END}{tail}"
    if after == before:
        return False
    README.write_text(after, encoding="utf-8")
    return True


def main() -> int:
    try:
        block = render(weather(), standings())
        print(block)
        print("\nREADME.md updated." if write(block) else "\nNo change.")
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("README.md left unchanged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

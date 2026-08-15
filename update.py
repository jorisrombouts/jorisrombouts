"""Regenerate the live block in README.md.

Two sources, no API keys, no dependencies beyond the standard library:

    weather   Open-Meteo   current conditions, and 11 days of daylight
    football  Wikipedia    the league table, three rows around NAC Breda

If anything fails the script exits non-zero without touching README.md, so a
broken run leaves the last good block in place rather than publishing a wrong
number.

    python3 update.py
"""

import html
import json
import re
import sys
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
MINUS = "−"  # U+2212: sits at digit height, unlike a hyphen

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


def ordinal(number: str) -> str:
    """10 -> 10th. Eleven through thirteen are the exceptions."""
    n = int(number)
    suffix = "th" if n % 100 in (11, 12, 13) else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def render(sky: dict, table: list[dict]) -> str:
    """Markdown, not ASCII art: GitHub styles the table and reflows it on a
    phone. The sparkline is the one part that needs a monospace font."""
    here = next(row for row in table if row["here"])
    lines = [
        f"#### 🌤️ {CITY} — {sky['temp']} °C, {sky['sky']}",
        "",
        f"`{sky['spark']}`  {sky['daylight']} of daylight, {sky['trend']}",
        "",
        f"#### ⚽ {TEAM} — {ordinal(here['Pos'])} in the {LEAGUE}",
        "",
        "| | Team | P | GD | Pts |",
        "|--:|:--|--:|--:|--:|",
    ]
    for row in table:
        cells = [row[name] for name in COLUMNS]
        if row["here"]:
            cells = [f"**{cell}**" for cell in cells]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write(block: str) -> bool:
    before = README.read_text(encoding="utf-8")
    if START not in before or END not in before:
        raise UpdateError(f"markers {START} / {END} not found in {README.name}")

    head, tail = before.split(START)[0], before.split(END)[-1]
    after = f"{head}{START}\n\n{block}\n\n{END}{tail}"
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

#!/usr/bin/env python3
"""
Flight search tool using the Kiwi.com Tequila API.

Searches for return flights between two locations, with filtering on flight
duration, trip length, and weekend inclusion. Outputs an HTML report with
direct booking links.

Origin and destination accept (passed directly to the Kiwi API):
  - A single IATA airport code:  LHR
  - A comma-separated list:      LHR,LGW,STN
  - A 2-letter country code:     GB  (all airports in that country)

Requires a Tequila API key (free at https://tequila.kiwi.com).
Set via --api-key or environment variable TEQUILA_API_KEY.

Usage:
  python search_flights.py --origin LHR --destination JFK \\
      --date-from 2026-06-01 --date-to 2026-06-30 \\
      --min-days 5 --max-days 9 --require-weekend \\
      --max-outbound-duration 12 --max-inbound-duration 12

  # Search all UK airports to any New York area airport:
  python search_flights.py --origin GB --destination JFK,EWR,LGA \\
      --date-from 2026-06-01 --date-to 2026-06-30
"""

import argparse
import html
import os
import sys
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

TEQUILA_SEARCH_URL = "https://tequila-api.kiwi.com/v2/search"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def has_weekend(start: date, end: date) -> bool:
    """Return True if at least one Saturday or Sunday falls in [start, end]."""
    if (end - start).days >= 6:
        return True
    d = start
    while d <= end:
        if d.weekday() in (5, 6):
            return True
        d += timedelta(days=1)
    return False


def to_kiwi_date(d: date) -> str:
    """Format a date as dd/mm/YYYY for the Kiwi API."""
    return d.strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# Kiwi Tequila API
# ---------------------------------------------------------------------------

def search_flights(api_key: str, fly_from: str, fly_to: str,
                   date_from: date, date_to: date,
                   nights_from: int, nights_to: int,
                   currency: str = "EUR",
                   limit: int = 200) -> list[dict]:
    """Query the Kiwi Tequila API for round-trip flights."""
    params = {
        "fly_from": fly_from,
        "fly_to": fly_to,
        "date_from": to_kiwi_date(date_from),
        "date_to": to_kiwi_date(date_to),
        "nights_in_dst_from": nights_from,
        "nights_in_dst_to": nights_to,
        "flight_type": "round",
        "curr": currency,
        "adults": 1,
        "limit": limit,
        "sort": "price",
        "one_for_city": 0,
    }
    headers = {"apikey": api_key}
    resp = requests.get(TEQUILA_SEARCH_URL, params=params, headers=headers)
    if resp.status_code != 200:
        print(f"API error {resp.status_code}: {resp.text[:300]}",
              file=sys.stderr)
        sys.exit(1)
    return resp.json().get("data", [])


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def outbound_duration_hours(flight: dict) -> float:
    """Outbound duration in hours (from the duration object, in seconds)."""
    return flight["duration"]["departure"] / 3600.0


def inbound_duration_hours(flight: dict) -> float:
    """Inbound (return) duration in hours."""
    return flight["duration"]["return"] / 3600.0


def flight_depart_date(flight: dict) -> date:
    """Extract the outbound departure date."""
    return datetime.fromisoformat(flight["local_departure"]).date()


def flight_return_date(flight: dict) -> date:
    """Extract the return arrival date (last route segment)."""
    # The route contains all segments; the last one with return=1 gives the
    # return arrival, but it's simpler to use local_arrival on the flight.
    # Actually, Kiwi gives local_arrival as the final arrival of the whole
    # itinerary. For the return departure, we look at route segments.
    for seg in reversed(flight.get("route", [])):
        return datetime.fromisoformat(seg["local_arrival"]).date()
    return datetime.fromisoformat(flight["local_arrival"]).date()


def outbound_segments(flight: dict) -> list[dict]:
    """Return the outbound route segments."""
    return [s for s in flight.get("route", []) if s.get("return") == 0]


def inbound_segments(flight: dict) -> list[dict]:
    """Return the inbound (return) route segments."""
    return [s for s in flight.get("route", []) if s.get("return") == 1]


def filter_flights(flights: list[dict],
                   max_outbound_hours: float | None,
                   max_inbound_hours: float | None,
                   require_weekend: bool) -> list[dict]:
    """Apply client-side filters."""
    kept = []
    for f in flights:
        if max_outbound_hours is not None:
            if outbound_duration_hours(f) > max_outbound_hours:
                continue
        if max_inbound_hours is not None:
            if inbound_duration_hours(f) > max_inbound_hours:
                continue
        if require_weekend:
            dep = flight_depart_date(f)
            ret = flight_return_date(f)
            if not has_weekend(dep, ret):
                continue
        kept.append(f)
    return kept


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def segment_summary(segments: list[dict]) -> str:
    """Human-readable summary of route segments."""
    parts = []
    for seg in segments:
        dep = seg["local_departure"][11:16]
        arr = seg["local_arrival"][11:16]
        airline = seg.get("airline", "??")
        flight_no = seg.get("flight_no", "")
        fly_from = seg.get("flyFrom", "")
        fly_to = seg.get("flyTo", "")
        parts.append(f"{airline}{flight_no} {fly_from} {dep}\u2192{fly_to} {arr}")
    return " \u00b7 ".join(parts)


def format_duration(seconds: int) -> str:
    """Format seconds as Xh Ym."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m:02d}m"


def google_flights_url(origin: str, dest: str,
                       dep_date: date, ret_date: date) -> str:
    return (
        f"https://www.google.com/travel/flights?q="
        f"Flights+from+{origin}+to+{dest}+"
        f"on+{dep_date.isoformat()}+"
        f"return+{ret_date.isoformat()}"
    )


def skyscanner_url(origin: str, dest: str,
                   dep_date: date, ret_date: date) -> str:
    dfmt = dep_date.strftime("%y%m%d")
    rfmt = ret_date.strftime("%y%m%d")
    return (
        f"https://www.skyscanner.net/transport/flights/"
        f"{origin.lower()}/{dest.lower()}/{dfmt}/{rfmt}/"
    )


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def render_html(flights: list[dict], args) -> str:
    origin_label = html.escape(args.origin.upper())
    dest_label = html.escape(args.destination.upper())
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    filters_desc = []
    if args.max_outbound_duration:
        filters_desc.append(f"Max outbound: {args.max_outbound_duration}h")
    if args.max_inbound_duration:
        filters_desc.append(f"Max inbound: {args.max_inbound_duration}h")
    filters_desc.append(f"Trip: {args.min_days}\u2013{args.max_days} days")
    if args.require_weekend:
        filters_desc.append("Weekend required")
    filters_html = " | ".join(filters_desc)

    rows = []
    for f in flights:
        dep_date = flight_depart_date(f)
        ret_date = flight_return_date(f)
        fly_from = f.get("flyFrom", "?")
        fly_to = f.get("flyTo", "?")
        price = f["price"]
        currency = f.get("currency", args.currency) if "currency" not in f else f["currency"]
        out_dur = f["duration"]["departure"]
        in_dur = f["duration"]["return"]
        out_segs = outbound_segments(f)
        in_segs = inbound_segments(f)
        out_stops = max(0, len(out_segs) - 1)
        in_stops = max(0, len(in_segs) - 1)
        days = (ret_date - dep_date).days
        weekend = "Yes" if has_weekend(dep_date, ret_date) else "No"

        deep_link = f.get("deep_link", "")
        gf_link = google_flights_url(fly_from, fly_to, dep_date, ret_date)
        sky_link = skyscanner_url(fly_from, fly_to, dep_date, ret_date)

        out_summary = html.escape(segment_summary(out_segs))
        in_summary = html.escape(segment_summary(in_segs))

        booking_links = []
        if deep_link:
            booking_links.append(
                f'<a href="{html.escape(deep_link)}" target="_blank">Kiwi.com</a>')
        booking_links.append(
            f'<a href="{gf_link}" target="_blank">Google&nbsp;Flights</a>')
        booking_links.append(
            f'<a href="{sky_link}" target="_blank">Skyscanner</a>')

        rows.append(f"""
        <tr>
          <td>{fly_from}\u2192{fly_to}</td>
          <td>{dep_date.isoformat()}</td>
          <td>{ret_date.isoformat()}</td>
          <td>{days}d</td>
          <td>{weekend}</td>
          <td class="price">{args.currency}&nbsp;{price}</td>
          <td>{format_duration(out_dur)} ({out_stops} stop{"s" if out_stops != 1 else ""})<br>
              <small>{out_summary}</small></td>
          <td>{format_duration(in_dur)} ({in_stops} stop{"s" if in_stops != 1 else ""})<br>
              <small>{in_summary}</small></td>
          <td>{"<br>".join(booking_links)}</td>
        </tr>""")

    no_results_msg = ""
    if not rows:
        no_results_msg = (
            '<tr><td colspan="9" style="text-align:center;padding:2em;">'
            "No flights found matching the filters.</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Flight Search: {origin_label} \u2192 {dest_label}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 2em; background: #f8f9fa; color: #212529; }}
  h1 {{ color: #0d6efd; }}
  .meta {{ color: #6c757d; margin-bottom: 1.5em; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff;
           box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
  th, td {{ padding: .6em .8em; border: 1px solid #dee2e6; text-align: left;
            vertical-align: top; }}
  th {{ background: #0d6efd; color: #fff; position: sticky; top: 0; }}
  tr:nth-child(even) {{ background: #f1f3f5; }}
  tr:hover {{ background: #d0ebff; }}
  .price {{ font-weight: bold; white-space: nowrap; }}
  a {{ color: #0d6efd; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  small {{ color: #6c757d; }}
</style>
</head>
<body>
<h1>Flights: {origin_label} &rarr; {dest_label}</h1>
<p class="meta">Generated {now} &mdash; {filters_html}</p>
<table>
<thead>
<tr>
  <th>Route</th><th>Outbound</th><th>Return</th><th>Days</th><th>Weekend</th>
  <th>Price</th><th>Outbound flight</th><th>Inbound flight</th><th>Book</th>
</tr>
</thead>
<tbody>
{"".join(rows)}{no_results_msg}
</tbody>
</table>
<p class="meta">{len(rows)} result(s)</p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Search return flights and produce an HTML report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single airport to single airport:
  %(prog)s --origin LHR --destination JFK \\
           --date-from 2026-06-01 --date-to 2026-06-30 \\
           --min-days 5 --max-days 9 --require-weekend \\
           --max-outbound-duration 12 --max-inbound-duration 12

  # All airports in GB to a list of NYC airports:
  %(prog)s --origin GB --destination JFK,EWR,LGA \\
           --date-from 2026-06-01 --date-to 2026-06-30

  Credentials via environment:
    export TEQUILA_API_KEY=your_key
""",
    )

    p.add_argument("--origin", required=True,
                   help="Origin: IATA code (LHR), comma list (LHR,LGW), "
                        "or 2-letter country (GB)")
    p.add_argument("--destination", required=True,
                   help="Destination: IATA code (JFK), comma list (JFK,EWR), "
                        "or 2-letter country (US)")

    p.add_argument("--date-from", required=True, type=date.fromisoformat,
                   help="Start of outbound date range (YYYY-MM-DD)")
    p.add_argument("--date-to", required=True, type=date.fromisoformat,
                   help="End of outbound date range (YYYY-MM-DD)")

    p.add_argument("--min-days", type=int, default=1,
                   help="Minimum days between outbound and return (default 1)")
    p.add_argument("--max-days", type=int, default=14,
                   help="Maximum days between outbound and return (default 14)")

    p.add_argument("--max-outbound-duration", type=float, default=None,
                   help="Max outbound flight duration in hours")
    p.add_argument("--max-inbound-duration", type=float, default=None,
                   help="Max inbound flight duration in hours")

    p.add_argument("--require-weekend", action="store_true",
                   help="Only include trips that span at least one Sat or Sun")

    p.add_argument("--limit", type=int, default=200,
                   help="Max results to fetch from API (default 200)")
    p.add_argument("--currency", default="EUR",
                   help="Price currency code (default EUR)")

    p.add_argument("--api-key", default=None,
                   help="Tequila API key (or set TEQUILA_API_KEY)")

    p.add_argument("--output", "-o", default=None,
                   help="Output HTML file (default: flights_ORIGIN_DEST.html)")
    p.add_argument("--no-open", action="store_true",
                   help="Do not open the HTML file in a browser")

    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    api_key = args.api_key or os.environ.get("TEQUILA_API_KEY")
    if not api_key:
        print("Error: Tequila API key required.", file=sys.stderr)
        print("  Set --api-key or TEQUILA_API_KEY env var.", file=sys.stderr)
        print("  Sign up free at https://tequila.kiwi.com", file=sys.stderr)
        sys.exit(1)

    fly_from = args.origin.upper()
    fly_to = args.destination.upper()

    print(f"Searching flights {fly_from} \u2192 {fly_to}")
    print(f"  Dates: {args.date_from} to {args.date_to}")
    print(f"  Trip length: {args.min_days}\u2013{args.max_days} days")

    flights = search_flights(
        api_key, fly_from, fly_to,
        args.date_from, args.date_to,
        args.min_days, args.max_days,
        currency=args.currency,
        limit=args.limit,
    )
    print(f"  API returned {len(flights)} offers")

    flights = filter_flights(
        flights,
        args.max_outbound_duration,
        args.max_inbound_duration,
        args.require_weekend,
    )
    print(f"  After filtering: {len(flights)} offers")

    # Already sorted by price from the API, but ensure it.
    flights.sort(key=lambda f: f["price"])

    html_content = render_html(flights, args)
    origin_tag = fly_from.replace(",", "-")
    dest_tag = fly_to.replace(",", "-")
    output_path = args.output or f"flights_{origin_tag}_{dest_tag}.html"
    Path(output_path).write_text(html_content, encoding="utf-8")
    print(f"Report written to {output_path}")

    if not args.no_open:
        webbrowser.open(Path(output_path).resolve().as_uri())


if __name__ == "__main__":
    main()

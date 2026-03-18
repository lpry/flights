#!/usr/bin/env python3
"""
Flight search tool using the Amadeus REST API.

Searches for return flights between two airports, with filtering on flight
duration, trip length, and weekend inclusion. Outputs an HTML report with
links to booking pages.

Origin and destination accept:
  - A single IATA airport code:  LHR
  - A comma-separated list:      LHR,LGW,STN
  - A 2-letter country code:     GB  (resolves to all airports in that country)

Requires an Amadeus API key/secret (free at https://developers.amadeus.com).
Set via CLI flags or environment variables AMADEUS_API_KEY / AMADEUS_API_SECRET.

Usage:
  python search_flights.py --origin LHR --destination JFK \
      --date-from 2026-06-01 --date-to 2026-06-30 \
      --min-days 5 --max-days 9 --require-weekend \
      --max-outbound-duration 12 --max-inbound-duration 12

  # Search all UK airports to any New York area airport:
  python search_flights.py --origin GB --destination JFK,EWR,LGA \
      --date-from 2026-06-01 --date-to 2026-06-30
"""

import argparse
import html
import itertools
import os
import sys
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

AMADEUS_AUTH_URL = "https://api.amadeus.com/v1/security/oauth2/token"
AMADEUS_FLIGHTS_URL = "https://api.amadeus.com/v2/shopping/flight-offers"
AMADEUS_LOCATIONS_URL = "https://api.amadeus.com/v1/reference-data/locations"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def date_range(start: date, end: date):
    """Yield each date from *start* to *end* inclusive."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


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


def iso_duration_to_hours(iso: str) -> float:
    """Convert an ISO-8601 duration like 'PT13H45M' to fractional hours."""
    s = iso
    if s.startswith("PT"):
        s = s[2:]
    hours = 0.0
    if "H" in s:
        h_part, s = s.split("H", 1)
        hours += float(h_part)
    if "M" in s:
        m_part, s = s.split("M", 1)
        hours += float(m_part) / 60.0
    return hours


# ---------------------------------------------------------------------------
# Amadeus REST API client
# ---------------------------------------------------------------------------

class AmadeusClient:
    """Minimal Amadeus API client using requests."""

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = None
        self.token_expires_at = 0.0

    def _authenticate(self):
        resp = requests.post(AMADEUS_AUTH_URL, data={
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.api_secret,
        })
        resp.raise_for_status()
        body = resp.json()
        self.access_token = body["access_token"]
        self.token_expires_at = time.time() + body["expires_in"] - 30

    def _ensure_token(self):
        if self.access_token is None or time.time() >= self.token_expires_at:
            self._authenticate()

    def search_flights(self, origin: str, destination: str,
                       depart_date: date, return_date: date,
                       max_results: int = 10, currency: str = "EUR"):
        self._ensure_token()
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": depart_date.isoformat(),
            "returnDate": return_date.isoformat(),
            "adults": 1,
            "max": max_results,
            "currencyCode": currency,
        }
        resp = requests.get(
            AMADEUS_FLIGHTS_URL,
            params=params,
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        if resp.status_code == 401:
            self._authenticate()
            resp = requests.get(
                AMADEUS_FLIGHTS_URL,
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
        if resp.status_code != 200:
            print(f"  API error {resp.status_code} for "
                  f"{depart_date} → {return_date}: {resp.text[:200]}",
                  file=sys.stderr)
            return []
        return resp.json().get("data", [])

    def airports_in_country(self, country_code: str) -> list[str]:
        """Return IATA codes for all airports in a country."""
        self._ensure_token()
        codes: list[str] = []
        # The API paginates; fetch up to a reasonable limit.
        params = {
            "subType": "AIRPORT",
            "countryCode": country_code.upper(),
            "page[limit]": 100,
        }
        resp = requests.get(
            AMADEUS_LOCATIONS_URL,
            params=params,
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        if resp.status_code != 200:
            print(f"Error looking up airports for country {country_code}: "
                  f"{resp.status_code} {resp.text[:200]}", file=sys.stderr)
            sys.exit(1)
        for loc in resp.json().get("data", []):
            iata = loc.get("iataCode")
            if iata:
                codes.append(iata)
        if not codes:
            print(f"No airports found for country code '{country_code}'.",
                  file=sys.stderr)
            sys.exit(1)
        return sorted(set(codes))


def resolve_location_spec(spec: str, client: AmadeusClient) -> list[str]:
    """Resolve a location specifier to a list of IATA airport codes.

    Accepted formats:
      - 3-letter IATA code:           "LHR"
      - Comma-separated IATA codes:   "LHR,LGW,STN"
      - 2-letter ISO country code:    "GB"
    """
    spec = spec.strip().upper()
    if "," in spec:
        codes = [c.strip() for c in spec.split(",") if c.strip()]
        # Expand any 2-letter country codes within the list.
        result = []
        for c in codes:
            if len(c) == 2:
                result.extend(client.airports_in_country(c))
            else:
                result.append(c)
        return sorted(set(result))
    if len(spec) == 2:
        return client.airports_in_country(spec)
    return [spec]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def itinerary_duration_hours(itinerary: dict) -> float:
    return iso_duration_to_hours(itinerary["duration"])


def segment_summary(segments: list) -> str:
    parts = []
    for seg in segments:
        dep = seg["departure"]["at"]
        arr = seg["arrival"]["at"]
        carrier = seg.get("carrierCode", "??")
        flight_no = seg.get("number", "")
        parts.append(f"{carrier}{flight_no} {dep[11:16]}\u2192{arr[11:16]}")
    return " \u00b7 ".join(parts)


def booking_url(origin: str, destination: str,
                depart_date: date, return_date: date) -> str:
    return (
        f"https://www.google.com/travel/flights?q="
        f"Flights+from+{origin}+to+{destination}+"
        f"on+{depart_date.isoformat()}+"
        f"return+{return_date.isoformat()}"
    )


def skyscanner_url(origin: str, destination: str,
                   depart_date: date, return_date: date) -> str:
    dfmt = depart_date.strftime("%y%m%d")
    rfmt = return_date.strftime("%y%m%d")
    return (
        f"https://www.skyscanner.net/transport/flights/"
        f"{origin.lower()}/{destination.lower()}/"
        f"{dfmt}/{rfmt}/"
    )


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_offers(offers: list, max_outbound_hours, max_inbound_hours):
    kept = []
    for offer in offers:
        itineraries = offer["itineraries"]
        outbound = itineraries[0]
        inbound = itineraries[1]
        out_h = itinerary_duration_hours(outbound)
        in_h = itinerary_duration_hours(inbound)
        if max_outbound_hours is not None and out_h > max_outbound_hours:
            continue
        if max_inbound_hours is not None and in_h > max_inbound_hours:
            continue
        kept.append(offer)
    return kept


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def offer_airports(offer: dict) -> tuple[str, str]:
    """Extract the actual origin and destination IATA codes from an offer."""
    outbound = offer["itineraries"][0]
    origin = outbound["segments"][0]["departure"]["iataCode"]
    destination = outbound["segments"][-1]["arrival"]["iataCode"]
    return origin, destination


def render_html(results: list, args, origin_codes: list[str],
                dest_codes: list[str]) -> str:
    origin_label = html.escape(args.origin.upper())
    dest_label = html.escape(args.destination.upper())
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    filters_desc = []
    filters_desc.append(f"From: {', '.join(origin_codes)}")
    filters_desc.append(f"To: {', '.join(dest_codes)}")
    if args.max_outbound_duration:
        filters_desc.append(f"Max outbound: {args.max_outbound_duration}h")
    if args.max_inbound_duration:
        filters_desc.append(f"Max inbound: {args.max_inbound_duration}h")
    filters_desc.append(f"Trip: {args.min_days}\u2013{args.max_days} days")
    if args.require_weekend:
        filters_desc.append("Weekend required")
    filters_html = " | ".join(filters_desc)

    rows = []
    for r in results:
        offer = r["offer"]
        dep_date = r["depart_date"]
        ret_date = r["return_date"]
        act_origin, act_dest = offer_airports(offer)
        outbound = offer["itineraries"][0]
        inbound = offer["itineraries"][1]
        price = offer["price"]["total"]
        currency = offer["price"].get("currency", "EUR")
        out_h = itinerary_duration_hours(outbound)
        in_h = itinerary_duration_hours(inbound)
        out_stops = len(outbound["segments"]) - 1
        in_stops = len(inbound["segments"]) - 1
        days = (ret_date - dep_date).days
        weekend = "Yes" if has_weekend(dep_date, ret_date) else "No"

        google_link = booking_url(act_origin, act_dest, dep_date, ret_date)
        sky_link = skyscanner_url(act_origin, act_dest, dep_date, ret_date)

        out_summary = html.escape(segment_summary(outbound["segments"]))
        in_summary = html.escape(segment_summary(inbound["segments"]))

        rows.append(f"""
        <tr>
          <td>{act_origin}\u2192{act_dest}</td>
          <td>{dep_date.isoformat()}</td>
          <td>{ret_date.isoformat()}</td>
          <td>{days}d</td>
          <td>{weekend}</td>
          <td class="price">{currency}&nbsp;{price}</td>
          <td>{out_h:.1f}h ({out_stops} stop{"s" if out_stops != 1 else ""})<br>
              <small>{out_summary}</small></td>
          <td>{in_h:.1f}h ({in_stops} stop{"s" if in_stops != 1 else ""})<br>
              <small>{in_summary}</small></td>
          <td>
            <a href="{google_link}" target="_blank">Google&nbsp;Flights</a><br>
            <a href="{sky_link}" target="_blank">Skyscanner</a>
          </td>
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
    export AMADEUS_API_KEY=your_key
    export AMADEUS_API_SECRET=your_secret
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

    p.add_argument("--max-results-per-query", type=int, default=10,
                   help="Max offers to fetch per date pair (default 10)")
    p.add_argument("--currency", default="EUR",
                   help="Price currency code (default EUR)")

    p.add_argument("--api-key", default=None,
                   help="Amadeus API key (or set AMADEUS_API_KEY)")
    p.add_argument("--api-secret", default=None,
                   help="Amadeus API secret (or set AMADEUS_API_SECRET)")

    p.add_argument("--output", "-o", default=None,
                   help="Output HTML file (default: flights_ORIGIN_DEST.html)")
    p.add_argument("--no-open", action="store_true",
                   help="Do not open the HTML file in a browser")

    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    api_key = args.api_key or os.environ.get("AMADEUS_API_KEY")
    api_secret = args.api_secret or os.environ.get("AMADEUS_API_SECRET")
    if not api_key or not api_secret:
        print("Error: Amadeus API credentials required.", file=sys.stderr)
        print("  Set --api-key/--api-secret or AMADEUS_API_KEY/AMADEUS_API_SECRET.",
              file=sys.stderr)
        print("  Sign up free at https://developers.amadeus.com", file=sys.stderr)
        sys.exit(1)

    client = AmadeusClient(api_key, api_secret)

    # Resolve origins and destinations.
    origin_codes = resolve_location_spec(args.origin, client)
    dest_codes = resolve_location_spec(args.destination, client)
    print(f"Origins:      {', '.join(origin_codes)}")
    print(f"Destinations: {', '.join(dest_codes)}")

    # Build the list of (outbound_date, return_date) pairs to query.
    date_pairs: set[tuple[date, date]] = set()
    for dep in date_range(args.date_from, args.date_to):
        for days in range(args.min_days, args.max_days + 1):
            ret = dep + timedelta(days=days)
            if args.require_weekend and not has_weekend(dep, ret):
                continue
            date_pairs.add((dep, ret))

    sorted_pairs = sorted(date_pairs)
    route_pairs = list(itertools.product(origin_codes, dest_codes))
    total_queries = len(sorted_pairs) * len(route_pairs)
    print(f"Searching {len(sorted_pairs)} date(s) \u00d7 "
          f"{len(route_pairs)} route(s) = {total_queries} queries \u2026")

    all_results = []
    query_num = 0
    for orig, dest in route_pairs:
        for dep, ret in sorted_pairs:
            query_num += 1
            print(f"  [{query_num}/{total_queries}] "
                  f"{orig}\u2192{dest} {dep}\u2192{ret} \u2026",
                  end=" ", flush=True)
            offers = client.search_flights(
                orig, dest, dep, ret,
                max_results=args.max_results_per_query,
                currency=args.currency,
            )
            offers = filter_offers(offers,
                                   args.max_outbound_duration,
                                   args.max_inbound_duration)
            print(f"{len(offers)} offers")
            for offer in offers:
                all_results.append({
                    "offer": offer,
                    "depart_date": dep,
                    "return_date": ret,
                })

    all_results.sort(key=lambda r: float(r["offer"]["price"]["total"]))

    print(f"\nTotal matching offers: {len(all_results)}")

    html_content = render_html(all_results, args, origin_codes, dest_codes)
    origin_tag = args.origin.upper().replace(",", "-")
    dest_tag = args.destination.upper().replace(",", "-")
    output_path = args.output or f"flights_{origin_tag}_{dest_tag}.html"
    Path(output_path).write_text(html_content, encoding="utf-8")
    print(f"Report written to {output_path}")

    if not args.no_open:
        webbrowser.open(Path(output_path).resolve().as_uri())


if __name__ == "__main__":
    main()

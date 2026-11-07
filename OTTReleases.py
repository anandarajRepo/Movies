#!/usr/bin/env python3
"""
OTTReleases.py - find Hollywood movies available / newly released on OTT in India.

Data source: JustWatch (https://www.justwatch.com/in), via the same public
GraphQL endpoint the website itself uses. For every movie it prints the OTT
service(s) and a hyperlink (the service's own watch URL when available,
otherwise the JustWatch page for the movie).

"Hollywood" = movies with the United States among their production countries
and English as the original language.

Usage:
    python OTTReleases.py                      # movies from the last 30 days
    python OTTReleases.py --days 90            # last 90 days
    python OTTReleases.py --year 2025          # everything from 2025
    python OTTReleases.py --all                # whole catalogue (slow)
    python OTTReleases.py --services netflix,prime   # filter by service name
    python OTTReleases.py --csv out.csv --json out.json

Requires: pip install requests
Note: JustWatch's GraphQL API is unofficial and may change; respect its terms.
"""

import argparse
import csv
import json
import sys
import time
from datetime import date, timedelta

import requests

GRAPHQL_URL = "https://apis.justwatch.com/graphql"
SITE_URL = "https://www.justwatch.com"
COUNTRY = "IN"
LANGUAGE = "en"
PAGE_SIZE = 100

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# Only subscription / free / ad-supported streaming counts as "OTT availability".
# Add "RENT" / "BUY" to this list if you also want transactional (TVOD) offers.
MONETIZATION_TYPES = ["FLATRATE", "FREE", "ADS"]

QUERY = """
query GetTitles($country: Country!, $language: Language!, $first: Int!,
                $after: String, $filter: TitleFilter) {
  popularTitles(country: $country, first: $first, after: $after,
                filter: $filter, sortBy: RELEASE_YEAR, sortAsc: false) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        ... on Movie {
          id
          content(country: $country, language: $language) {
            title
            originalReleaseDate
            originalReleaseYear
            originalLanguage
            fullPath
            productionCountries
          }
          offers(country: $country, platform: WEB) {
            monetizationType
            standardWebURL
            availableToTime
            package { clearName shortName }
          }
        }
      }
    }
  }
}
"""


def fetch_page(session, variables, retries=3):
    payload = {"query": QUERY, "variables": variables}
    for attempt in range(1, retries + 1):
        try:
            resp = session.post(GRAPHQL_URL, json=payload, headers=HEADERS, timeout=30)
            if resp.status_code == 429:
                time.sleep(2 * attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(data["errors"][0].get("message", "GraphQL error"))
            return data["data"]["popularTitles"]
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
            if attempt == retries:
                raise
            time.sleep(1.5 * attempt)
            print(f"  retry {attempt}/{retries - 1} after error: {exc}", file=sys.stderr)


def is_hollywood(content):
    countries = content.get("productionCountries") or []
    lang = (content.get("originalLanguage") or "").lower()
    # originalLanguage is an ISO code ("en") or language name depending on API version
    return "US" in countries and lang in ("en", "eng", "english", "")


def build_filter(args):
    flt = {
        "objectTypes": ["MOVIE"],
        "productionCountries": ["US"],
        "monetizationTypes": MONETIZATION_TYPES,
    }
    if args.year:
        flt["releaseYear"] = {"min": args.year, "max": args.year}
    elif not args.all:
        since = date.today() - timedelta(days=args.days)
        flt["releaseYear"] = {"min": since.year}
    return flt


def collect(args):
    session = requests.Session()
    variables = {
        "country": COUNTRY,
        "language": LANGUAGE,
        "first": PAGE_SIZE,
        "after": None,
        "filter": build_filter(args),
    }
    cutoff = None
    if not args.all and not args.year:
        cutoff = (date.today() - timedelta(days=args.days)).isoformat()

    wanted = [s.strip().lower() for s in args.services.split(",")] if args.services else []
    movies, seen = [], set()
    pages = 0

    while True:
        page = fetch_page(session, variables)
        pages += 1
        for edge in page["edges"]:
            node = edge.get("node") or {}
            content = node.get("content")
            if not content or node.get("id") in seen:
                continue
            seen.add(node["id"])
            if not is_hollywood(content):
                continue
            release = (content.get("originalReleaseDate") or "")[:10]
            if cutoff and release and release < cutoff:
                continue

            # group offers by OTT service
            services = {}
            for offer in node.get("offers") or []:
                if offer.get("monetizationType") not in MONETIZATION_TYPES:
                    continue
                pkg = offer["package"]["clearName"]
                if wanted and not any(w in pkg.lower() for w in wanted):
                    continue
                services.setdefault(pkg, offer.get("standardWebURL"))
            if not services:
                continue

            movies.append({
                "title": content["title"],
                "release_date": release or str(content.get("originalReleaseYear") or ""),
                "justwatch_url": SITE_URL + content["fullPath"] if content.get("fullPath") else "",
                "services": [{"ott": k, "url": v} for k, v in sorted(services.items())],
            })

        info = page["pageInfo"]
        if not info["hasNextPage"] or (args.max_pages and pages >= args.max_pages):
            break
        variables["after"] = info["endCursor"]
        time.sleep(0.5)  # be polite

    movies.sort(key=lambda m: m["release_date"], reverse=True)
    return movies


def print_report(movies):
    if not movies:
        print("No matching Hollywood movies found on Indian OTT platforms.")
        return
    print(f"\nHollywood movies on OTT in India: {len(movies)}\n")
    for i, m in enumerate(movies, 1):
        print(f"{i}. {m['title']}  ({m['release_date']})")
        for s in m["services"]:
            print(f"     - {s['ott']}: {s['url'] or m['justwatch_url']}")
        print(f"     JustWatch: {m['justwatch_url']}")


def write_csv(movies, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["title", "release_date", "ott_service", "link", "justwatch_url"])
        for m in movies:
            for s in m["services"]:
                w.writerow([m["title"], m["release_date"], s["ott"],
                            s["url"] or m["justwatch_url"], m["justwatch_url"]])


def main():
    p = argparse.ArgumentParser(description="Hollywood movies on OTT in India (via JustWatch).")
    p.add_argument("--days", type=int, default=30, help="release window in days (default 30)")
    p.add_argument("--year", type=int, help="only movies released in this year")
    p.add_argument("--all", action="store_true", help="whole catalogue, no date limit")
    p.add_argument("--services", help="comma-separated OTT name filter, e.g. netflix,prime,hotstar")
    p.add_argument("--max-pages", type=int, default=5, help="page limit, 0 = unlimited (default 5)")
    p.add_argument("--csv", help="write results to CSV file")
    p.add_argument("--json", help="write results to JSON file")
    args = p.parse_args()

    try:
        movies = collect(args)
    except Exception as exc:  # network / API failure
        sys.exit(f"Failed to fetch data from JustWatch: {exc}")

    print_report(movies)
    if args.csv:
        write_csv(movies, args.csv)
        print(f"\nSaved CSV -> {args.csv}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(movies, fh, indent=2, ensure_ascii=False)
        print(f"Saved JSON -> {args.json}")


if __name__ == "__main__":
    main()

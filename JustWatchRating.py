#!/usr/bin/env python3
"""
JustWatchRating.py - find well-liked movies on JustWatch India that are
streaming on at least one Indian OTT service.

A movie qualifies when:
  * dislikes <= 10% of likes   (dislikes / likes <= 0.10)
  * it is offered in the Indian region (country IN) on at least one OTT
    service (subscription / free / ad-supported by default)

Results are sorted year-wise and month-wise (newest first by default) and
printed grouped under "YYYY - Month" headings.

Usage:
    python JustWatchRating.py                    # all years, default 10% ratio
    python JustWatchRating.py --year 2025        # a single year
    python JustWatchRating.py --min-likes 100    # ignore titles with few votes
    python JustWatchRating.py --ratio 0.05       # stricter: dislikes <= 5% of likes
    python JustWatchRating.py --services netflix,prime
    python JustWatchRating.py --oldest-first
    python JustWatchRating.py --csv out.csv --json out.json

Requires: pip install requests
Note: JustWatch's GraphQL API is unofficial and may change (field names such
as interactions.likelistAdditions / dislikelistAdditions); respect its terms of use.
"""

import argparse
import csv
import json
import sys
import time
from calendar import month_name
from collections import OrderedDict

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

# Streaming availability that counts as "OTT". Add "RENT" / "BUY" if wanted.
MONETIZATION_TYPES = ["FLATRATE", "FREE", "ADS"]

QUERY = """
query GetTitles($country: Country!, $language: Language!, $first: Int!,
                $after: String, $filter: TitleFilter) {
  popularTitles(country: $country, first: $first, after: $after,
                filter: $filter) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        ... on Movie {
          id
          content(country: $country, language: $language) {
            title
            originalReleaseDate
            originalReleaseYear
            fullPath
            interactions { likelistAdditions dislikelistAdditions }
          }
          offers(country: $country, platform: WEB) {
            monetizationType
            standardWebURL
            package { clearName }
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
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(data["errors"][0].get("message", "GraphQL error"))
            return data["data"]["popularTitles"]
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
            if attempt == retries:
                raise
            print(f"  retry {attempt}/{retries - 1} after error: {exc}", file=sys.stderr)
            time.sleep(1.5 * attempt)
    raise RuntimeError("rate limited by JustWatch (HTTP 429)")


def parse_date(content):
    """Return (year, month) from the release date, month 0 if unknown."""
    raw = (content.get("originalReleaseDate") or "")[:10]
    parts = raw.split("-")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        year = content.get("originalReleaseYear")
        return (int(year), 0) if year else (0, 0)


def dislike_ratio_ok(likes, dislikes, ratio):
    """True when dislikes are at most `ratio` (default 10%) of likes."""
    return likes > 0 and dislikes <= ratio * likes


def collect(args):
    session = requests.Session()
    flt = {"objectTypes": ["MOVIE"]}
    if args.year:
        flt["releaseYear"] = {"min": args.year, "max": args.year}
    variables = {
        "country": COUNTRY,
        "language": LANGUAGE,
        "first": PAGE_SIZE,
        "after": None,
        "filter": flt,
    }
    wanted = [s.strip().lower() for s in args.services.split(",")] if args.services else []

    movies, seen, pages = [], set(), 0
    while True:
        page = fetch_page(session, variables)
        pages += 1
        for edge in page["edges"]:
            node = edge.get("node") or {}
            content = node.get("content")
            if not content or node.get("id") in seen:
                continue
            seen.add(node["id"])

            inter = content.get("interactions") or {}
            likes = inter.get("likelistAdditions") or 0
            dislikes = inter.get("dislikelistAdditions") or 0
            if likes < args.min_likes or not dislike_ratio_ok(likes, dislikes, args.ratio):
                continue

            services = {}
            for offer in node.get("offers") or []:
                if offer.get("monetizationType") not in MONETIZATION_TYPES:
                    continue
                pkg = offer["package"]["clearName"]
                if wanted and not any(w in pkg.lower() for w in wanted):
                    continue
                services.setdefault(pkg, offer.get("standardWebURL"))
            if not services:  # must be on at least one Indian OTT
                continue

            year, month = parse_date(content)
            movies.append({
                "title": content["title"],
                "year": year,
                "month": month,
                "release_date": (content.get("originalReleaseDate") or "")[:10],
                "likes": likes,
                "dislikes": dislikes,
                "dislike_pct": round(100.0 * dislikes / likes, 2),
                "justwatch_url": SITE_URL + content["fullPath"] if content.get("fullPath") else "",
                "services": [{"ott": k, "url": v} for k, v in sorted(services.items())],
            })

        info = page["pageInfo"]
        if not info["hasNextPage"] or (args.max_pages and pages >= args.max_pages):
            break
        variables["after"] = info["endCursor"]
        time.sleep(0.5)  # be polite

    # year-wise, then month-wise, then best (lowest dislike %) first
    movies.sort(key=lambda m: (m["year"], m["month"]), reverse=not args.oldest_first)
    return movies


def month_label(month):
    return month_name[month] if 1 <= month <= 12 else "Unknown month"


def print_report(movies, ratio):
    if not movies:
        print("No movies found matching the criteria.")
        return
    print(f"\nMovies on Indian OTT with dislikes <= {ratio:.0%} of likes: {len(movies)}")
    groups = OrderedDict()
    for m in movies:
        groups.setdefault((m["year"], m["month"]), []).append(m)
    for (year, month), items in groups.items():
        print(f"\n=== {year or 'Unknown year'} - {month_label(month)} ({len(items)}) ===")
        for m in items:
            print(f"  {m['title']}  [{m['release_date'] or year}]  "
                  f"likes={m['likes']} dislikes={m['dislikes']} ({m['dislike_pct']}%)")
            for s in m["services"]:
                print(f"       - {s['ott']}: {s['url'] or m['justwatch_url']}")


def write_csv(movies, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["year", "month", "title", "release_date", "likes", "dislikes",
                    "dislike_pct", "ott_service", "link", "justwatch_url"])
        for m in movies:
            for s in m["services"]:
                w.writerow([m["year"], month_label(m["month"]), m["title"], m["release_date"],
                            m["likes"], m["dislikes"], m["dislike_pct"], s["ott"],
                            s["url"] or m["justwatch_url"], m["justwatch_url"]])


def main():
    p = argparse.ArgumentParser(
        description="Movies on Indian OTT where dislikes are <= 10%% of likes (via JustWatch).")
    p.add_argument("--ratio", type=float, default=0.10,
                   help="max dislikes/likes ratio (default 0.10 = 10%%)")
    p.add_argument("--min-likes", type=int, default=1,
                   help="ignore titles with fewer likes than this (default 1)")
    p.add_argument("--year", type=int, help="only movies released in this year")
    p.add_argument("--services", help="comma-separated OTT name filter, e.g. netflix,prime")
    p.add_argument("--max-pages", type=int, default=5, help="page limit, 0 = unlimited (default 5)")
    p.add_argument("--oldest-first", action="store_true", help="sort oldest year/month first")
    p.add_argument("--csv", help="write results to CSV file")
    p.add_argument("--json", help="write results to JSON file")
    args = p.parse_args()

    try:
        movies = collect(args)
    except Exception as exc:
        sys.exit(f"Failed to fetch data from JustWatch: {exc}")

    print_report(movies, args.ratio)
    if args.csv:
        write_csv(movies, args.csv)
        print(f"\nSaved CSV -> {args.csv}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(movies, fh, indent=2, ensure_ascii=False)
        print(f"Saved JSON -> {args.json}")


if __name__ == "__main__":
    main()

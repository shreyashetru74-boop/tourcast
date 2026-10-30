"""Turns Qloo heatmap grid points into named, ranked tour cities and a route.

Qloo's heatmap returns thousands of (lat, lng, affinity) points. We snap each
point to the nearest real city (offline GeoNames data, no extra API calls),
score each city by the affinity of its strongest points, and then build a
travel-efficient route.
"""
from __future__ import annotations

import math
from collections import defaultdict
from functools import lru_cache

import geonamescache

from . import config


# GeoNames quirks: neighbourhoods recorded with a whole metro's population.
NAME_FIXES = {
    "Rasapūdipalem": "Visakhapatnam",
    "East Harlem": "New York City",
    "Paradise": "Las Vegas",
}


@lru_cache(maxsize=1)
def _cities() -> list[dict]:
    gc = geonamescache.GeonamesCache(min_city_population=15000)
    out = []
    for c in gc.get_cities().values():
        if c["name"] in NAME_FIXES and any(
                o["name"] == NAME_FIXES[c["name"]] and o["countrycode"] == c["countrycode"]
                for o in gc.get_cities().values() if o["population"] > 100000):
            continue  # the real city is already in the list
        label = c["name"]
        if c["countrycode"] == "US" and c.get("admin1code"):
            label = f'{c["name"]}, {c["admin1code"]}'
        out.append({"name": c["name"], "label": label, "lat": c["latitude"],
                    "lng": c["longitude"], "cc": c["countrycode"],
                    "pop": c["population"]})
    return out


def country_code(country: str) -> str:
    if country in config.COUNTRIES:
        return config.COUNTRIES[country]
    if len(country) == 2:
        return country.upper()
    gc = geonamescache.GeonamesCache()
    for code, c in gc.get_countries().items():
        if c["name"].lower() == country.lower():
            return code
    return "US"


def cities_in_country(country: str, min_pop: int = 50000) -> list[dict]:
    cc = country_code(country)
    return sorted((c for c in _cities() if c["cc"] == cc and c["pop"] >= min_pop),
                  key=lambda c: -c["pop"])


@lru_cache(maxsize=32)
def tour_markets(cc: str, min_pop: int = 40000) -> tuple:
    """Cities that work as tour stops: drops neighbourhoods and suburbs that sit
    within 30 km of a city at least 1.5x their size (East Harlem -> New York)."""
    cities = sorted((c for c in _cities() if c["cc"] == cc and c["pop"] >= min_pop),
                    key=lambda c: -c["pop"])
    kept: list[dict] = []
    for c in cities:
        # Merge suburbs into a city 1.5x their size within 30 km, and anything
        # within 5 km of a bigger city (GeoNames lists some neighbourhoods, like
        # South Boston, with almost the whole city's population).
        if not any((d := km(c["lat"], c["lng"], k["lat"], k["lng"])) < 5
                   or (k["pop"] >= 1.5 * c["pop"] and d < 30) for k in kept):
            kept.append(c)
    return tuple(kept)


def find_city(name: str, country: str | None = None) -> dict | None:
    name = name.strip().lower()
    cc = country_code(country) if country else None
    matches = [c for c in _cities()
               if (c["name"].lower() == name or c["label"].lower() == name)
               and (cc is None or c["cc"] == cc)]
    return max(matches, key=lambda c: c["pop"]) if matches else None


def km(a_lat, a_lng, b_lat, b_lng) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def rank_cities(points: list[dict], country: str, max_cities: int = 8,
                min_sep_km: float = 120, scored: list[dict] | None = None) -> list[dict]:
    """Rank cities by fan affinity, skipping near-duplicate markets."""
    scored = scored if scored is not None else score_cities(points, country)
    chosen: list[dict] = []
    for s in scored:
        if all(km(s["lat"], s["lng"], c["lat"], c["lng"]) >= min_sep_km for c in chosen):
            chosen.append(dict(s))
        if len(chosen) >= max_cities:
            break
    for i, c in enumerate(chosen, 1):
        c["rank"] = i
    return chosen


def biggest_markets(country: str, n: int) -> list[dict]:
    """The naive plan: the n most populous cities (suburbs merged into metros)."""
    out: list[dict] = []
    for c in tour_markets(country_code(country)):
        if all(km(c["lat"], c["lng"], o["lat"], o["lng"]) >= 120 for o in out):
            out.append(c)
        if len(out) >= n:
            break
    return out


def lookup_score(scored: list[dict], name: str, lat: float | None = None,
                 lng: float | None = None) -> dict | None:
    """Find a city's fan-match entry by name (or by position, within 40 km)."""
    exact = name.strip().lower()
    for s in scored:  # full label first, so "Portland, ME" never matches Portland, OR
        if s["city"].lower() == exact:
            return s
    key = name.split(",")[0].strip().lower()
    for s in scored:
        short = s["city"].split(",")[0].lower()
        if short == key or short == key.replace(" city", "") or key == short.replace(" city", ""):
            return s
    if lat is not None and lng is not None:
        near = [s for s in scored if km(s["lat"], s["lng"], lat, lng) <= 40]
        if near:
            return max(near, key=lambda s: s["match"])
    return None


def score_cities(points: list[dict], country: str, snap_km: float = 60) -> list[dict]:
    """Group heatmap points by nearest city; score each city by fan affinity."""
    candidates = tour_markets(country_code(country))
    grid: dict[tuple, list[dict]] = defaultdict(list)
    for c in candidates:
        grid[(int(c["lat"]), int(c["lng"]))].append(c)

    buckets: dict[str, list[float]] = defaultdict(list)
    city_by_label: dict[str, dict] = {}
    for p in points:
        gx, gy = int(p["lat"]), int(p["lng"])
        best, best_d = None, float("inf")
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for c in grid.get((gx + dx, gy + dy), ()):
                    d = km(p["lat"], p["lng"], c["lat"], c["lng"])
                    if d > snap_km:
                        continue
                    # Prefer the metro over its suburbs: a point 20 km from New York
                    # belongs to New York, not to Yonkers 10 km away.
                    d_adj = d / (c["pop"] / 100000) ** 0.3
                    if d_adj < best_d:
                        best, best_d = c, d_adj
        if best:
            buckets[best["label"]].append(p["affinity"])
            city_by_label[best["label"]] = best

    scored = []
    for label, affs in buckets.items():
        top = sorted(affs, reverse=True)[:3]
        strong = sum(1 for a in affs if a >= 0.9)
        c = city_by_label[label]
        scored.append({
            "city": label, "lat": c["lat"], "lng": c["lng"], "population": c["pop"],
            "match": round(sum(top) / len(top) * 100, 1),
            "hot_points": strong, "points": len(affs),
        })
    scored.sort(key=lambda s: (s["match"], s["hot_points"]), reverse=True)
    annotate_ranks(scored)
    return scored


HIDDEN_MAX_POP = 400_000  # a hidden market is a small or mid-sized city, never a metro


def annotate_ranks(scored: list[dict]) -> None:
    """Add Fan Fit rank, size rank and the hidden-market flag to every city.

    A hidden market ranks near the top for the artist's sound but far lower by
    population: the cities a biggest-cities tour would never pick.
    """
    n = len(scored)
    by_size = sorted(scored, key=lambda s: -s["population"])
    size_rank = {s["city"]: i + 1 for i, s in enumerate(by_size)}
    top_cut = max(10, math.ceil(n * 0.08))
    for i, s in enumerate(scored, 1):
        s["fit_rank"] = i
        s["markets"] = n
        s["top_pct"] = max(1, math.ceil(i / n * 100)) if n else None
        s["size_rank"] = size_rank[s["city"]]
        s["hidden"] = (i <= top_cut and s["size_rank"] >= max(20, 3 * i)
                       and s["population"] < HIDDEN_MAX_POP)


def hidden_markets(scored: list[dict], n: int = 6, min_sep_km: float = 80) -> list[dict]:
    """The strongest hidden markets, spaced apart so they aren't one metro area."""
    out: list[dict] = []
    for s in scored:
        if s.get("hidden") and all(km(s["lat"], s["lng"], o["lat"], o["lng"]) >= min_sep_km for o in out):
            out.append(s)
        if len(out) >= n:
            break
    return out


def blend_scores(layer_scores: list[list[dict]], weights: list[float]) -> list[dict]:
    """Blend several artists' city scores by weight (a city missing from a layer
    counts as zero for it), then re-rank. Working per city rather than per grid
    point is robust to heatmap grids that don't line up between artists."""
    total = sum(weights) or 1.0
    merged: dict[str, dict] = {}
    for scored, w in zip(layer_scores, weights):
        for s in scored:
            m = merged.setdefault(s["city"], {**s, "match": 0.0, "hot_points": 0, "points": 0})
            m["match"] += s["match"] * w / total
            m["hot_points"] += round(s["hot_points"] * w / total)
            m["points"] = max(m["points"], s["points"])
    out = list(merged.values())
    for m in out:
        m["match"] = round(m["match"], 1)
    out.sort(key=lambda s: (s["match"], s["hot_points"]), reverse=True)
    annotate_ranks(out)
    return out


def blend_points(layers: list[list[dict]], weights: list[float]) -> list[dict]:
    """Heatmap points for display: each artist's layer scaled by its weight."""
    top = max(weights) or 1.0
    return [{**p, "affinity": p["affinity"] * w / top} for pts, w in zip(layers, weights) for p in pts]


def order_route(stops: list[dict], start: dict | None = None) -> dict:
    """Nearest-neighbour route from the home city through every stop."""
    remaining = stops[:]
    route, total = [], 0.0
    here = start or (remaining[0] if remaining else None)
    if start is None and remaining:
        route.append({**remaining.pop(0), "leg_km": 0})
    while remaining and here:
        nxt = min(remaining, key=lambda s: km(here["lat"], here["lng"], s["lat"], s["lng"]))
        leg = km(here["lat"], here["lng"], nxt["lat"], nxt["lng"])
        total += leg
        route.append({**nxt, "leg_km": round(leg)})
        remaining.remove(nxt)
        here = nxt
    return {"stops": route, "total_km": round(total)}


def hotspots(points: list[dict], n: int = 3, min_sep_km: float = 1.5) -> list[dict]:
    """Top-affinity points inside one city, spaced apart (fan neighbourhoods)."""
    out: list[dict] = []
    for p in sorted(points, key=lambda p: (p["affinity"], p["popularity"]), reverse=True):
        if all(km(p["lat"], p["lng"], o["lat"], o["lng"]) >= min_sep_km for o in out):
            out.append({"lat": round(p["lat"], 5), "lng": round(p["lng"], 5),
                        "match": round(p["affinity"] * 100, 1)})
        if len(out) >= n:
            break
    return out


def nearest_affinity(points: list[dict], lat: float, lng: float, radius_km: float = 40) -> float | None:
    """How strong an artist's fan base is around a location (for co-headliner fit).

    Averages the five strongest heatmap points within the radius, scaled by how
    much of the area they cover, so two artists rarely tie at 100%.
    """
    near = [p["affinity"] for p in points
            if abs(p["lat"] - lat) <= 0.6 and abs(p["lng"] - lng) <= 0.8
            and km(p["lat"], p["lng"], lat, lng) <= radius_km]
    if not near:
        return None
    top = sorted(near, reverse=True)[:5]
    coverage = min(1.0, len(near) / 5)
    return sum(top) / len(top) * (0.85 + 0.15 * coverage) * 0.995 ** max(0, 5 - len(top))

"""The agent's tools. Each one wraps one or more Qloo calls and records its
result in PlanState, which is what the UI renders.

The LLM sees short JSON summaries; the full data goes to the browser.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import geo
from .qloo import Qloo

Emit = Callable[[str, dict], Awaitable[None]]


@dataclass
class PlanState:
    artist_names: list[str]
    country: str
    home_city: str
    num_shows: int
    age: str
    emit: Emit
    plan_id: str = ""
    artist_label: str = ""          # the user's own act name, if given
    edited: bool = False            # changed by a visitor after planning
    qloo: Qloo = field(default_factory=Qloo)
    seeds: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    similar: list[dict] = field(default_factory=list)
    fan_points: list[dict] = field(default_factory=list)
    ranked_cities: list[dict] = field(default_factory=list)
    route: dict = field(default_factory=dict)
    city_details: dict[str, dict] = field(default_factory=dict)
    crossover: dict[str, list] = field(default_factory=dict)
    final: dict = field(default_factory=dict)
    scored: list[dict] = field(default_factory=list)      # every city with fan signal
    llm_cities: list[str] | None = None                  # AI-only baseline picks
    compare: dict = field(default_factory=dict)
    blend: list[float] | None = None                     # sound-blender weights per seed
    _seed_scores: dict[str, list] = field(default_factory=dict)
    _city_tasks: dict[str, asyncio.Future] = field(default_factory=dict)
    _cross_task: asyncio.Future | None = None
    _artist_heat: dict[str, asyncio.Future] = field(default_factory=dict)
    _venue_strategy: str | None = None

    @property
    def seed_ids(self) -> list[str]:
        return [s["id"] for s in self.seeds if s.get("id")]

    def snapshot(self) -> dict:
        heat = sorted(self.fan_points, key=lambda p: p["affinity"], reverse=True)[:900]
        return {
            "country": self.country, "home_city": self.home_city, "age": self.age,
            "seeds": self.seeds, "missing": self.missing, "similar": self.similar,
            "heat": [[round(p["lat"], 3), round(p["lng"], 3), round(p["affinity"], 3)] for p in heat],
            "ranked_cities": self.ranked_cities, "route": self.route,
            "city_details": self.city_details, "crossover": self.crossover,
            "final": self.final, "qloo_calls": len(self.qloo.calls), "mock": self.qloo.mock,
            "compare": self.compare, "alternatives": self.alternatives(), "plan_id": self.plan_id,
            "hidden_markets": self.hidden(), "artist_label": self.artist_label, "blend": self.blend,
            "markets": len(self.scored),
        }

    def hidden(self) -> list[dict]:
        on = {s["city"] for s in self.route.get("stops", [])}
        return [{**_rank_fields(h), "city": h["city"], "lat": h["lat"], "lng": h["lng"],
                 "population": h["population"], "on_tour": h["city"] in on}
                for h in geo.hidden_markets(self.scored)]

    def alternatives(self, n: int = 6) -> list[dict]:
        """Strong cities not on the tour, for the 'add a stop' list."""
        on = {s["city"] for s in self.route.get("stops", [])}
        return [{"city": c["city"], "match": c["match"], **_rank_fields(c)}
                for c in self.ranked_cities if c["city"] not in on][:n]


def _rank_fields(c: dict) -> dict:
    return {k: c.get(k) for k in ("fit_rank", "markets", "top_pct", "size_rank", "hidden")}


# ----------------------------------------------------------------- tools
async def resolve_artists(st: PlanState, names: list[str] | None = None) -> dict:
    names = [n for n in (names or st.artist_names) if n.strip()][:4]
    found = await asyncio.gather(*(st.qloo.search_artist(n) for n in names))
    st.seeds, st.missing = [], []
    for n, f in zip(names, found):
        (st.seeds.append(f) if f else st.missing.append(n))
    await st.emit("state", {"seeds": st.seeds, "missing": st.missing})
    return {"resolved": [{"input": n, "qloo_name": f["name"]} for n, f in zip(names, found) if f],
            "not_found": st.missing}


async def find_similar_artists(st: PlanState) -> dict:
    if not st.seed_ids:
        return {"error": "No resolved artists. Call resolve_artists first."}
    base = {"signal.interests.entities": ",".join(st.seed_ids),
            "filter.exclude.entities": ",".join(st.seed_ids),
            "signal.demographics.age": st.age, "take": 10}
    normal, trending = await asyncio.gather(
        st.qloo.insights("urn:entity:artist", **base),
        st.qloo.insights("urn:entity:artist", **base, **{"bias.trends": "high"}),
    )
    t_rank = {a["name"]: i for i, a in enumerate(trending)}
    st.similar = []
    for i, a in enumerate(normal):
        rising = a["name"] in t_rank and t_rank[a["name"]] + 2 <= i
        st.similar.append({**a, "rising": rising})
    seen = {a["name"] for a in st.similar}
    for a in trending[:4]:  # trend-boosted artists the plain list missed
        if a["name"] not in seen:
            st.similar.append({**a, "rising": True})
    await st.emit("state", {"similar": st.similar})
    for a in [a for a in st.similar if a.get("id")][:4]:
        _artist_heat_task(st, a)  # warm co-headliner data in the background
    return {"similar_artists": [{"name": a["name"], "affinity": a["affinity"],
                                 "rising": a["rising"]} for a in st.similar[:12]]}


async def map_fan_cities(st: PlanState, max_cities: int = 10) -> dict:
    if not st.seed_ids:
        return {"error": "No resolved artists. Call resolve_artists first."}
    st.fan_points = await st.qloo.heatmap(st.seed_ids, st.country, st.age)
    st.scored = geo.score_cities(st.fan_points, st.country)
    # Rank extra cities beyond what the agent asked for, so the UI can offer swaps.
    want = max(4, min(max_cities, 12))
    st.ranked_cities = geo.rank_cities(st.fan_points, st.country,
                                       max_cities=max(want, st.num_shows + 6), scored=st.scored)
    await st.emit("state", {"heat": st.snapshot()["heat"], "ranked_cities": st.ranked_cities})
    # Start fetching details for the likely stops now, while the agent decides.
    prefetch(st, st.ranked_cities[:st.num_shows + 2])
    if not st.ranked_cities:
        return {"error": f"Qloo returned no heatmap data for {st.country}."}
    return {"heatmap_points": len(st.fan_points), "markets_scored": len(st.scored),
            "ranked_cities": [{"city": c["city"], "fan_fit_rank": c["fit_rank"],
                               "size_rank": c["size_rank"], "hidden_market": c["hidden"]}
                              for c in st.ranked_cities[:want]],
            "hidden_markets": [h["city"] for h in st.hidden()]}


async def plan_route(st: PlanState, cities: list[str]) -> dict:
    pool = st.ranked_cities + [c for c in st.scored if c not in st.ranked_cities]
    by_name = {c["city"].lower(): c for c in reversed(pool)}
    by_short = {c["city"].split(",")[0].lower(): c for c in reversed(pool)}
    stops, unknown = [], []
    for name in cities:
        c = by_name.get(name.lower()) or by_short.get(name.split(",")[0].strip().lower())
        (stops.append(c) if c and c not in stops else unknown.append(name))
    if not stops:
        return {"error": "None of those cities are in the ranked list. Use map_fan_cities names."}
    home = geo.find_city(st.home_city, st.country) if st.home_city else None
    start = {"lat": home["lat"], "lng": home["lng"]} if home else None
    st.route = geo.order_route(stops, start)
    st.route["home"] = ({"city": home["label"], "lat": home["lat"], "lng": home["lng"]}
                        if home else None)
    await st.emit("state", {"route": st.route})
    return {"route": [f'{s["city"]} ({s["leg_km"]} km leg)' for s in st.route["stops"]],
            "total_km": st.route["total_km"], "ignored": unknown}


def _artist_heat_task(st: PlanState, artist: dict) -> asyncio.Future:
    # Share one in-flight request per artist when several cities ask at once.
    if artist["id"] not in st._artist_heat:
        st._artist_heat[artist["id"]] = _quiet(asyncio.ensure_future(
            st.qloo.heatmap([artist["id"]], st.country, st.age)))
    return st._artist_heat[artist["id"]]


async def _artist_heat(st: PlanState, artist: dict) -> list[dict]:
    return await _artist_heat_task(st, artist)


def _quiet(fut: asyncio.Future) -> asyncio.Future:
    """A background task's error surfaces when it's awaited; don't also log it unawaited."""
    fut.add_done_callback(lambda f: f.cancelled() or f.exception())
    return fut


def prefetch(st: PlanState, cities: list[dict]) -> None:
    """Fetch city details and fan crossover in the background (results are reused)."""
    for c in cities:
        _city_local_task(st, c)
    if st._cross_task is None and st.seed_ids:
        st._cross_task = _quiet(asyncio.ensure_future(_crossover_data(st)))


def _city_local_task(st: PlanState, c: dict) -> asyncio.Future:
    if c["city"] not in st._city_tasks:
        st._city_tasks[c["city"]] = _quiet(asyncio.ensure_future(_city_local(st, c)))
    return st._city_tasks[c["city"]]


async def _city_local(st: PlanState, c: dict) -> tuple[list, list, str | None]:
    """Fan hotspots inside a city and the places those fans like."""
    # Include the state for US cities so "Portland, ME" isn't read as Portland, Oregon.
    local_pts = await st.qloo.heatmap(st.seed_ids, f'{c["city"]}, {st.country}', st.age)
    # Guard against the location resolving to a different city: keep nearby points only.
    local_pts = [p for p in local_pts if geo.km(p["lat"], p["lng"], c["lat"], c["lng"]) <= 60]
    spots = geo.hotspots(local_pts, n=3)
    anchor = spots[0] if spots else {"lat": c["lat"], "lng": c["lng"]}
    venues, strategy = await _venues(st, c["city"], anchor)
    return spots, venues, strategy


async def _venues(st: PlanState, city: str, spot: dict | None) -> tuple[list[dict], str | None]:
    base = {"signal.interests.entities": ",".join(st.seed_ids),
            "signal.demographics.age": st.age, "take": 12}
    cc = geo.country_code(st.country)
    strategies = {
        "point_radius": {"filter.location": f'POINT({spot["lng"]} {spot["lat"]})',
                         "filter.location.radius": 6000},
        "location_query": {"filter.location.query": f"{city}, {st.country}"},
        "geocode_name": {"filter.geocode.name": city.split(",")[0],
                         "filter.geocode.country_code": cc},
    }
    order = [st._venue_strategy] if st._venue_strategy else list(strategies)
    for name in order:
        found = await st.qloo.insights("urn:entity:place", **base, **strategies[name])
        found = _gig_friendly(found)
        if len(found) >= 1:
            st._venue_strategy = name
            return found[:5], name
    return [], None


# Places that make no sense for a gig or pop-up, even if fans visit them.
_AVOID = ("memorial", "cemetery", "holocaust", "church", "cathedral", "temple", "mosque",
          "synagogue", "gurdwara", "hospital", "clinic", "school", "university", "airport",
          "station", "police", "court", "embassy", "funeral", "monument", "war ", "prison")
# Places that do: shown first.
_PREFER = ("bar", "pub", "cafe", "café", "coffee", "music", "venue", "club", "theater",
           "theatre", "record", "vinyl", "brew", "lounge", "hall", "ballroom", "tavern",
           "bookstore", "books", "wine", "taproom", "gallery", "studio")


# Fine to visit, but not where you'd play or pop up; used only if nothing better.
_SCENERY = ("park", "garden", "zoo", "beach", "trail", "bridge", "plaza", "square",
            "island", "lake", "mountain", "viewpoint", "observatory", "landmark",
            "golf", "country club", "residence", "resort", "spa", "pga", "tennis")


def _gig_friendly(places: list[dict]) -> list[dict]:
    def text(p):
        return (" ".join([p.get("name") or ""] + (p.get("tags") or []))).lower()
    ok = [p for p in places if not any(w in text(p) for w in _AVOID)]
    strict = [p for p in ok if not any(w in text(p) for w in _SCENERY)]
    chosen = strict if len(strict) >= 2 else ok
    return sorted(chosen, key=lambda p: not any(w in text(p) for w in _PREFER))


def find_scored(st: PlanState, city: str) -> dict | None:
    """A city's scored entry: exact name first (two Portlands), then short name."""
    pool = st.ranked_cities + st.scored
    exact = city.strip().lower()
    short = city.split(",")[0].strip().lower()
    return (next((x for x in pool if x["city"].lower() == exact), None)
            or next((x for x in pool if x["city"].split(",")[0].lower() == short), None))


async def city_deep_dive(st: PlanState, city: str) -> dict:
    c = find_scored(st, city)
    if not c:
        return {"error": f"{city} is not in the ranked city list."}
    spots, venues, strategy = await _city_local_task(st, c)

    # Best co-headliner: which similar artist's fans are strongest in this city?
    pool = [a for a in st.similar if a.get("id")][:4]
    heats = await asyncio.gather(*(_artist_heat(st, a) for a in pool))
    fits = []
    for a, pts in zip(pool, heats):
        aff = geo.nearest_affinity(pts, c["lat"], c["lng"])
        if aff is not None:
            fits.append({"name": a["name"], "local_match": round(aff * 100, 1),
                         "rising": a.get("rising", False), "image": a.get("image")})
    fits.sort(key=lambda f: f["local_match"], reverse=True)

    detail = {"city": c["city"], "match": c["match"], "hotspots": spots,
              "venues": venues, "venue_method": strategy, "co_headliners": fits[:2],
              "population": c.get("population"), **_rank_fields(c)}
    st.city_details[c["city"]] = detail
    await st.emit("city", detail)
    return {"city": c["city"], "fan_fit_rank": f'#{c.get("fit_rank")} of {c.get("markets")}',
            "size_rank": c.get("size_rank"), "hidden_market": c.get("hidden"),
            "fan_hotspots": len(spots),
            "venues": [v["name"] for v in venues] or "no venue data for this city",
            "best_co_headliners": [f'{f["name"]} ({f["local_match"]}% local match'
                                   f'{", rising" if f["rising"] else ""})' for f in fits[:2]]}


async def fan_crossover(st: PlanState) -> dict:
    if not st.seed_ids:
        return {"error": "No resolved artists."}
    if st._cross_task is None:
        st._cross_task = asyncio.ensure_future(_crossover_data(st))
    st.crossover = await st._cross_task
    await st.emit("state", {"crossover": st.crossover})
    return {k: [e["name"] for e in v] for k, v in st.crossover.items()}


async def _crossover_data(st: PlanState) -> dict:
    p = {"signal.interests.entities": ",".join(st.seed_ids),
         "signal.demographics.age": st.age, "take": 6}
    brands, movies, shows = await asyncio.gather(
        st.qloo.insights("urn:entity:brand", **p),
        st.qloo.insights("urn:entity:movie", **p),
        st.qloo.insights("urn:entity:tv_show", **p),
    )
    def unique(items):  # Qloo can return the same brand under two entity IDs
        seen, out = set(), []
        for e in items:
            key = (e.get("name") or "").casefold()
            if key and key not in seen:
                seen.add(key)
                out.append(e)
        return out

    return {"brands": unique(brands), "movies": unique(movies), "tv_shows": unique(shows)}


async def finalize_plan(st: PlanState, headline: str, city_notes: list[dict] | None = None,
                        caveats: list[str] | None = None, decisions: list[dict] | None = None) -> dict:
    st.final = {"headline": headline, "city_notes": city_notes or [],
                "caveats": caveats or [], "decisions": decisions or []}
    await st.emit("state", {"final": st.final})
    return {"ok": True}


async def reweight(st: PlanState, weights: list[float]) -> None:
    """Sound blender: re-rank every market for a weighted mix of the seed artists.

    Each seed artist's own heatmap is scored per city, the city scores are
    blended by weight, and the tour is re-picked from the new ranking.
    """
    seeds = [s for s in st.seeds if s.get("id")]
    if len(seeds) < 2 or len(weights) != len(seeds):
        raise ValueError("The sound blender needs two or more artists.")
    if not hasattr(st, "_original"):
        st._original = (st.fan_points, st.scored)          # the unweighted mix
    if max(weights) - min(weights) < 1e-6:
        st.fan_points, st.scored = st._original
        st.blend = None
    else:
        layers = await asyncio.gather(*(_artist_heat(st, s) for s in seeds))
        for s, pts in zip(seeds, layers):
            if s["id"] not in st._seed_scores:
                st._seed_scores[s["id"]] = geo.score_cities(pts, st.country)
        st.scored = geo.blend_scores([st._seed_scores[s["id"]] for s in seeds], weights)
        st.fan_points = geo.blend_points(layers, weights)
        st.blend = [round(w, 3) for w in weights]
    n = len(st.route.get("stops", [])) or st.num_shows
    st.ranked_cities = geo.rank_cities(st.fan_points, st.country,
                                       max_cities=n + 6, scored=st.scored)
    await plan_route(st, [c["city"] for c in st.ranked_cities[:n]])
    # Cities kept on the tour keep their details; refresh their ranks.
    for name, d in st.city_details.items():
        c = find_scored(st, name)
        if c:
            d.update(match=c["match"], **_rank_fields(c))


def explain_decisions(st: PlanState, limit: int = 4) -> list[dict]:
    """The judgement calls behind the route, derived from the data.

    Covers strong markets left out because they duplicate a nearby stop, strong
    markets left out for higher-ranked ones, and hidden markets that were picked.
    """
    stops = st.route.get("stops", [])
    if not stops or not st.scored:
        return []
    on = {s["city"] for s in stops}
    worst = max((s.get("fit_rank") or 0) for s in stops)
    out: list[dict] = []
    for c in st.scored:
        if c["fit_rank"] > worst:
            break
        if c["city"] in on:
            continue
        near = min(stops, key=lambda s: geo.km(c["lat"], c["lng"], s["lat"], s["lng"]))
        d = round(geo.km(c["lat"], c["lng"], near["lat"], near["lng"]))
        if d < 150:
            out.append({"city": c["city"], "action": "skipped",
                        "reason": f"Ranks #{c['fit_rank']}, but it's {d} km from {near['city']}, "
                                  "which is already on the tour: the same fans."})
        else:
            out.append({"city": c["city"], "action": "skipped",
                        "reason": f"Ranks #{c['fit_rank']} but isn't on this route. "
                                  "Add it from Other strong markets."})
    for s in stops:
        if s.get("hidden"):
            out.append({"city": s["city"], "action": "picked",
                        "reason": f"Hidden market: #{s['fit_rank']} for this sound but only "
                                  f"#{s['size_rank']} by size."})
    skips = [o for o in out if o["action"] == "skipped"][:2]
    picks = [o for o in out if o["action"] == "picked"][:2]
    return (skips + picks)[:limit]


def build_comparison(st: PlanState) -> dict:
    """Score three plans with the same Qloo heatmap: Tourcast, biggest cities, AI-only."""
    if not st.scored or not st.route.get("stops"):
        return {}
    n = len(st.route["stops"])
    rank_of = {s["city"]: i + 1 for i, s in enumerate(st.scored)}

    def entry(name, lat=None, lng=None):
        if lat is None:
            known = geo.find_city(name.split(",")[0], st.country)
            lat, lng = (known["lat"], known["lng"]) if known else (None, None)
        s = geo.lookup_score(st.scored, name, lat, lng)
        if not s:
            return {"city": name, "match": None, "rank": None}
        return {"city": name, "match": s["match"], "rank": rank_of.get(s["city"])}

    def summary(rows):
        vals = [r["match"] for r in rows if r["match"] is not None]
        ranks = sorted(r["rank"] for r in rows if r["rank"] is not None)
        return {"rows": rows,
                "avg_match": round(sum(vals) / len(vals), 1) if vals else None,
                "median_rank": ranks[len(ranks) // 2] if ranks else None,
                "no_signal": sum(1 for r in rows if r["match"] is None)}

    plans = {
        "tourcast": summary([entry(s["city"]) for s in st.route["stops"]]),
        "biggest": summary([entry(c["label"], c["lat"], c["lng"])
                            for c in geo.biggest_markets(st.country, n)]),
    }
    if st.llm_cities:
        plans["ai_only"] = summary([entry(c) for c in st.llm_cities[:n]])
    st.compare = {"markets_scored": len(st.scored), **plans, "insight": _insight(st, plans)}
    return st.compare


def _insight(st: PlanState, plans: dict) -> dict | None:
    """The one-line takeaway: the biggest city to skip and the small one to play."""
    big = [r for r in plans["biggest"]["rows"] if r["rank"]]
    skip = max(big, key=lambda r: r["rank"]) if big else None
    stops = {s["city"] for s in st.route.get("stops", [])}
    hidden = st.hidden()
    play = next((h for h in hidden if h["city"] in stops), hidden[0] if hidden else None)
    if not play:
        tour = [find_scored(st, c) for c in stops]
        tour = [t for t in tour if t]
        play = max(tour, key=lambda t: t.get("size_rank") or 0) if tour else None
    if not skip or not play or (skip["rank"] or 0) <= (play.get("fit_rank") or 0) * 3:
        return None
    return {"skip": skip, "play": {"city": play["city"], "fit_rank": play.get("fit_rank"),
                                   "size_rank": play.get("size_rank"),
                                   "on_tour": play["city"] in stops},
            "markets": len(st.scored)}


TOOLS: dict[str, Callable[..., Awaitable[dict]]] = {
    "resolve_artists": resolve_artists,
    "find_similar_artists": find_similar_artists,
    "map_fan_cities": map_fan_cities,
    "plan_route": plan_route,
    "city_deep_dive": city_deep_dive,
    "fan_crossover": fan_crossover,
    "finalize_plan": finalize_plan,
}


def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {},
                       "required": required or []}}}


TOOL_SCHEMAS = [
    _fn("resolve_artists", "Look up artists in Qloo's taste graph. Defaults to the user's artists.",
        {"names": {"type": "array", "items": {"type": "string"}}}),
    _fn("find_similar_artists", "Artists whose fans overlap with the resolved artists; flags rising ones."),
    _fn("map_fan_cities", "Qloo heatmap of where the artist's fans are across the chosen country, "
        "ranked into cities by fan affinity.",
        {"max_cities": {"type": "integer", "description": "How many candidate cities (4-12)."}}),
    _fn("plan_route", "Order chosen cities into an efficient route from the home city.",
        {"cities": {"type": "array", "items": {"type": "string"},
                    "description": "City names exactly as returned by map_fan_cities."}},
        ["cities"]),
    _fn("city_deep_dive", "For one tour city: fan hotspots inside the city, venues the fans like, "
        "and the best local co-headliner.", {"city": {"type": "string"}}, ["city"]),
    _fn("fan_crossover", "Brands, films and TV shows the artist's fans over-index on, "
        "for promotion and sponsorship ideas."),
    _fn("finalize_plan", "Submit the final tour plan. Call exactly once, at the end.",
        {"headline": {"type": "string", "description": "One-sentence summary of the tour."},
         "city_notes": {"type": "array", "items": {"type": "object", "properties": {
             "city": {"type": "string"},
             "why": {"type": "string", "description": "Why this city, citing Qloo evidence."},
             "promo": {"type": "string", "description": "One concrete promotion idea for this city."}},
             "required": ["city", "why", "promo"]}},
         "caveats": {"type": "array", "items": {"type": "string"}},
         "decisions": {"type": "array", "description":
             "2-4 judgement calls you made: strong cities you left out and why, or "
             "lower-ranked cities you chose and why. Cite ranks and distances from tool results.",
             "items": {"type": "object", "properties": {
                 "city": {"type": "string"},
                 "action": {"type": "string", "enum": ["picked", "skipped"]},
                 "reason": {"type": "string"}},
                 "required": ["city", "action", "reason"]}}},
        ["headline", "city_notes"]),
]


def summarize(result: Any) -> str:
    text = json.dumps(result, default=str)
    return text if len(text) < 3000 else text[:2990] + "...}"

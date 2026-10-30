"""Synthetic responses shaped like Qloo's, for demo mode (QLOO_MOCK=1).

Used only when no Qloo key is configured, so the UI can be developed and
tested offline. The app shows a "DEMO DATA" badge whenever this is active.
"""
from __future__ import annotations

import hashlib
import random

from .geo import cities_in_country, find_city

_ARTISTS = ["Neon Harbor", "The Paper Kites Club", "Lowlight Avenue", "Mira Sol",
            "Velvet Monsoon", "Static Gardens", "Juniper Lane", "Ocean Hymns",
            "Golden Static", "Kite Theory", "Saltwater Radio", "Indigo Pilots"]
_BRANDS = ["Fjallraven", "Urban Outfitters", "Patagonia", "Polaroid", "Converse", "Muji"]
_MOVIES = ["Lady Bird", "Before Sunrise", "Frances Ha", "Lost in Translation", "Her", "Aftersun"]
_SHOWS = ["Normal People", "Fleabag", "Atlanta", "The Bear", "Master of None"]
_PLACES = ["The Listening Room", "Corner Vinyl Cafe", "Backyard Stage", "Lantern Bar",
           "Second Floor Studio", "The Tin Shed", "Courtyard Social"]


def _rng(*parts) -> random.Random:
    seed = int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)
    return random.Random(seed)


def _ent(name, subtype, aff, extra=None):
    e = {"name": name, "entity_id": hashlib.md5(name.encode()).hexdigest().upper(),
         "subtype": subtype, "popularity": 0.9, "query": {"affinity": aff}, "properties": {},
         "tags": []}
    if extra:
        e.update(extra)
    return e


def mock_response(path: str, params: dict) -> dict:
    if path == "/search":
        q = params.get("query", "Artist")
        return {"results": [_ent(q.title(), "urn:entity:artist", None)]}

    if path == "/v2/trending":
        return {"results": {"trending": []}}

    ftype = params.get("filter.type", "")
    signal = params.get("signal.interests.entities", "")
    r = _rng(signal, ftype, params.get("bias.trends", ""), params.get("signal.demographics.age", ""))

    if ftype == "urn:heatmap":
        loc = params.get("filter.location.query", "United States")
        city = find_city(loc.split(",")[0]) if "," in loc else None
        pts = []
        if city:  # neighbourhood-level points around one city
            for _ in range(120):
                pts.append((city["lat"] + r.uniform(-0.12, 0.12),
                            city["lng"] + r.uniform(-0.12, 0.12)))
        else:
            for c in cities_in_country(loc, min_pop=150000)[:60]:
                for _ in range(4):
                    pts.append((c["lat"] + r.uniform(-0.2, 0.2), c["lng"] + r.uniform(-0.2, 0.2)))
        out = []
        for lat, lng in pts:
            a = r.random() ** 0.5
            out.append({"location": {"latitude": lat, "longitude": lng},
                        "query": {"affinity": a, "popularity": a * 0.9}})
        return {"results": {"heatmap": out}}

    names = {"urn:entity:artist": _ARTISTS, "urn:entity:brand": _BRANDS,
             "urn:entity:movie": _MOVIES, "urn:entity:tv_show": _SHOWS,
             "urn:entity:place": _PLACES}.get(ftype)
    if not names:
        return {"results": {"entities": []}}
    pool = names[:]
    r.shuffle(pool)
    take = int(params.get("take", 8))
    ents = []
    for i, n in enumerate(pool[:take]):
        extra = None
        if ftype == "urn:entity:place":
            extra = {"properties": {"address": f"{r.randint(1, 200)} Example Street"}}
        ents.append(_ent(n, ftype, round(0.97 - i * 0.02, 3), extra))
    return {"results": {"entities": ents}}

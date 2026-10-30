"""Thin async client for the Qloo hackathon API, with a private on-disk cache.

Every call is logged to a per-request trace so the UI can show judges exactly
which Qloo requests produced each recommendation (provenance).
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import time
from typing import Any

import httpx

from . import config
from .mock_data import mock_response

_sem = asyncio.Semaphore(6)  # be polite to the API: max 6 calls in flight
# Which agent step made a request, so the trace can count calls per step.
CURRENT_STEP: contextvars.ContextVar[str | None] = contextvars.ContextVar("step", default=None)


class QlooError(Exception):
    pass


def _cache_path(path: str, params: dict) -> str:
    key = json.dumps([path, sorted(params.items())], default=str)
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    return os.path.join(config.CACHE_DIR, f"{digest}.json")


class Qloo:
    """One instance per plan request; collects a call log for provenance."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.mock = config.QLOO_MOCK

    async def get(self, path: str, params: dict[str, Any]) -> dict:
        params = {k: v for k, v in params.items() if v not in (None, "", [])}
        started = time.time()
        cache_file = _cache_path(path, params)
        source = "cache"
        if os.path.exists(cache_file):
            with open(cache_file, encoding="utf-8") as f:
                body = json.load(f)
        elif self.mock:
            source = "mock"
            body = mock_response(path, params)
        else:
            source = "live"
            body = await self._fetch(path, params)
            os.makedirs(config.CACHE_DIR, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(body, f)
        self.calls.append({
            "path": path,
            "params": {k: (v if len(str(v)) < 90 else str(v)[:87] + "...") for k, v in params.items()},
            "source": source,
            "ms": int((time.time() - started) * 1000),
            "step": CURRENT_STEP.get(),
        })
        return body

    async def _fetch(self, path: str, params: dict) -> dict:
        headers = {"X-Api-Key": config.QLOO_API_KEY}
        async with _sem:
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=40) as client:
                        r = await client.get(f"{config.QLOO_BASE_URL}{path}",
                                             params=params, headers=headers)
                except httpx.HTTPError as e:
                    if attempt == 2:
                        raise QlooError(f"Network error calling Qloo: {e}") from e
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt == 2:
                        raise QlooError(f"Qloo returned {r.status_code}")
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if r.status_code == 401:
                    raise QlooError("Qloo rejected the API key (401). Check QLOO_API_KEY "
                                    "and that QLOO_BASE_URL is the hackathon host.")
                if r.status_code >= 400:
                    # 400/403: bad parameter or unsupported type. Return empty so the
                    # caller can try a fallback instead of crashing the whole plan.
                    return {"success": False, "status": r.status_code, "error": r.text[:300],
                            "results": {}}
                return r.json()
        return {"results": {}}

    # ------------------------------------------------------------ helpers
    async def search_artist(self, name: str) -> dict | None:
        body = await self.get("/search", {"query": name, "types": "urn:entity:artist", "take": 3})
        results = body.get("results") or []
        if not results:
            return None
        lname = name.lower().strip()
        exact = [r for r in results if r.get("name", "").lower().strip() == lname]
        best = (exact or results)[0]
        return slim(best)

    async def insights(self, filter_type: str, **params) -> list[dict]:
        body = await self.get("/v2/insights", {"filter.type": filter_type, **params})
        return [slim(e) for e in (body.get("results") or {}).get("entities", [])]

    async def heatmap(self, entity_ids: list[str], location_query: str, age: str = "") -> list[dict]:
        body = await self.get("/v2/insights", {
            "filter.type": "urn:heatmap",
            "signal.interests.entities": ",".join(entity_ids),
            "filter.location.query": location_query,
            "signal.demographics.age": age,
        })
        pts = []
        for p in (body.get("results") or {}).get("heatmap", []):
            loc, q = p.get("location") or {}, p.get("query") or {}
            if "latitude" in loc and "longitude" in loc:
                pts.append({
                    "lat": loc["latitude"], "lng": loc["longitude"],
                    "affinity": q.get("affinity", 0) or 0,
                    "popularity": q.get("popularity", 0) or 0,
                })
        return pts


def slim(e: dict) -> dict:
    """Keep only the fields the app needs (Qloo responses are very large)."""
    props = e.get("properties") or {}
    img = props.get("image") or {}
    q = e.get("query") or {}
    return {
        "id": e.get("entity_id"),
        "name": e.get("name"),
        "type": e.get("subtype") or e.get("type"),
        "affinity": q.get("affinity"),
        "popularity": e.get("popularity"),
        "image": img.get("url"),
        "address": props.get("address"),
        "lat": (e.get("location") or {}).get("lat"),
        "lng": (e.get("location") or {}).get("lon") or (e.get("location") or {}).get("lng"),
        "tags": [t.get("name") for t in (e.get("tags") or [])[:5] if isinstance(t, dict)],
        "year": props.get("release_year"),
    }

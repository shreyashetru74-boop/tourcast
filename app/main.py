"""FastAPI app: serves the frontend, streams the agent's progress, edits tours."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .agent import _fill_notes, run_plan
from .tools import PlanState, build_comparison, city_deep_dive, plan_route, reweight

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("tourcast").setLevel(logging.INFO)

app = FastAPI(title="Tourcast", version="1.1")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Recent plans, kept in memory so a tour can be edited after it's planned.
PLANS: "OrderedDict[str, PlanState]" = OrderedDict()
MAX_PLANS = 200
# Same inputs within this window return the saved plan instantly (clearly labelled).
RECENT: dict[str, dict] = {}
REPLAY_SECONDS = 6 * 3600


PLAN_DIR = os.path.join(config.CACHE_DIR, "plans")
_ID = re.compile(r"^[0-9a-f]{12}$")


def save_plan(st: PlanState, extra: dict | None = None) -> None:
    """Keep a copy of the finished plan on disk so its share link still opens."""
    try:
        os.makedirs(PLAN_DIR, exist_ok=True)
        with open(os.path.join(PLAN_DIR, f"{st.plan_id}.json"), "w", encoding="utf-8") as f:
            json.dump({**st.snapshot(), "calls": st.qloo.calls, **(extra or {})}, f, default=str)
    except OSError:
        logging.getLogger("tourcast").warning("Could not save plan %s", st.plan_id)


class PlanRequest(BaseModel):
    artists: list[str] = Field(..., min_length=1, max_length=4)
    artist_label: str = ""
    country: str = "United States"
    home_city: str = ""
    num_shows: int = Field(5, ge=2, le=10)
    age: str = ""
    fresh: bool = False  # true = always plan live, never reuse a saved plan


def request_key(req: PlanRequest) -> str:
    return json.dumps([sorted(a.strip().lower() for a in req.artists if a.strip()),
                       req.artist_label.strip().lower(), req.country, req.home_city.strip().lower(),
                       req.num_shows, req.age])


class StopsRequest(BaseModel):
    cities: list[str] = Field(..., min_length=2, max_length=10)


@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def get_config():
    return {"countries": list(config.COUNTRIES), "ages": config.AGE_BUCKETS,
            "mock": config.QLOO_MOCK, "llm": config.LLM_ENABLED,
            "model": config.LLM_MODEL if config.LLM_ENABLED else None}


@app.post("/api/plan")
async def plan(req: PlanRequest):
    """Streams newline-delimited JSON events: trace, state, city, done, error."""
    key = request_key(req)
    hit = RECENT.get(key)
    if (not req.fresh and hit and time.time() - hit["at"] < REPLAY_SECONDS
            and hit["plan_id"] in PLANS and not PLANS[hit["plan_id"]].edited):
        saved = PLANS[hit["plan_id"]]

        async def replay():
            yield json.dumps({"event": "trace", "data": {"kind": "note", "text":
                "Showing the plan made for these same inputs at " +
                time.strftime("%H:%M", time.localtime(hit["at"])) + "."}}) + "\n"
            yield json.dumps({"event": "done", "data": {
                **saved.snapshot(), "calls": saved.qloo.calls, "agent": hit["agent"],
                "seconds": hit["seconds"], "replayed": True}}, default=str) + "\n"

        return StreamingResponse(replay(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache"})

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(kind: str, data: dict) -> None:
        if kind == "done" and data.get("route", {}).get("stops"):
            RECENT[key] = {"plan_id": st.plan_id, "at": time.time(),
                           "agent": data.get("agent"), "seconds": data.get("seconds")}
        await queue.put({"event": kind, "data": data})

    st = PlanState(
        artist_names=[a.strip()[:80] for a in req.artists if a.strip()],
        country=req.country if req.country in config.COUNTRIES else "United States",
        home_city=req.home_city.strip()[:80], num_shows=req.num_shows,
        age=req.age if req.age in config.AGE_BUCKETS else "", emit=emit,
        plan_id=uuid.uuid4().hex[:12], artist_label=req.artist_label.strip()[:60],
    )
    PLANS[st.plan_id] = st
    while len(PLANS) > MAX_PLANS:
        PLANS.popitem(last=False)

    async def worker():
        try:
            await run_plan(st)
            save_plan(st)
        except Exception as e:
            logging.getLogger("tourcast").exception("Plan failed")
            await queue.put({"event": "error", "data": {"message": str(e)[:300]}})
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(worker())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield json.dumps(item, default=str) + "\n"
        finally:
            task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/plan/{plan_id}/stops")
async def set_stops(plan_id: str, req: StopsRequest):
    """Replace the tour's stops (remove or add cities); re-route and fill in details."""
    st = PLANS.get(plan_id)
    if not st or not st.scored:
        raise HTTPException(404, "This plan has expired. Plan the tour again to edit it.")

    async def quiet(kind: str, data: dict) -> None:
        return None

    st.emit = quiet
    st.edited = True  # never replay an edited plan to someone else
    result = await plan_route(st, req.cities)
    if "error" in result:
        raise HTTPException(400, result["error"])
    await asyncio.gather(*(city_deep_dive(st, s["city"]) for s in st.route["stops"]
                           if s["city"] not in st.city_details))
    _fill_notes(st)
    build_comparison(st)
    save_plan(st)
    return {**st.snapshot(), "calls": st.qloo.calls}


class BlendRequest(BaseModel):
    weights: list[float] = Field(..., min_length=2, max_length=4)


@app.post("/api/plan/{plan_id}/blend")
async def blend(plan_id: str, req: BlendRequest):
    """Sound blender: re-rank every market for a weighted mix of the seed artists."""
    st = PLANS.get(plan_id)
    if not st or not st.scored:
        raise HTTPException(404, "This plan has expired. Plan the tour again to use the blender.")
    if any(w < 0 for w in req.weights) or sum(req.weights) <= 0:
        raise HTTPException(400, "Weights must be zero or more, and not all zero.")

    async def quiet(kind: str, data: dict) -> None:
        return None

    st.emit = quiet
    st.edited = True
    try:
        await reweight(st, req.weights)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await asyncio.gather(*(city_deep_dive(st, s["city"]) for s in st.route["stops"]
                           if s["city"] not in st.city_details))
    _fill_notes(st)
    build_comparison(st)
    save_plan(st)
    return {**st.snapshot(), "calls": st.qloo.calls}


@app.get("/api/plan/{plan_id}")
async def get_plan(plan_id: str):
    """A saved plan, for share links. Editable only while the server still holds it."""
    if not _ID.match(plan_id):
        raise HTTPException(404, "Plan not found.")
    st = PLANS.get(plan_id)
    if st and st.route.get("stops"):
        return {**st.snapshot(), "calls": st.qloo.calls, "editable": True}
    path = os.path.join(PLAN_DIR, f"{plan_id}.json")
    if not os.path.exists(path):
        raise HTTPException(404, "This plan link has expired. Plan the tour again to get a new one.")
    with open(path, encoding="utf-8") as f:
        return {**json.load(f), "editable": False}


@app.get("/p/{plan_id}")
async def shared_page(plan_id: str):
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache"})

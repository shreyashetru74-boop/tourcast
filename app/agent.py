"""Tourcast agent: an LLM plans the tour by calling Qloo-backed tools.

If no LLM key is configured, or the model fails mid-run, a deterministic
orchestrator finishes the remaining steps, so the demo always completes.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time

from openai import AsyncOpenAI, BadRequestError

from . import config
from .qloo import CURRENT_STEP
from .tools import TOOL_SCHEMAS, TOOLS, PlanState, build_comparison, summarize

log = logging.getLogger("tourcast")
_call_ids = itertools.count(1)


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL,
                       timeout=config.LLM_TIMEOUT_S, max_retries=0)


_effort_ok = True  # flips to False if the provider rejects reasoning_effort


async def _complete(client: AsyncOpenAI, **kwargs):
    """chat.completions.create with low reasoning effort when supported (faster)."""
    global _effort_ok
    if _effort_ok and config.LLM_REASONING_EFFORT:
        try:
            return await client.chat.completions.create(
                reasoning_effort=config.LLM_REASONING_EFFORT, **kwargs)
        except BadRequestError as e:
            if "reasoning" not in str(e).lower():
                raise
            _effort_ok = False
    return await client.chat.completions.create(**kwargs)

SYSTEM = """You are Tourcast, a tour-planning agent for independent musicians.
Ground every decision in Qloo taste data returned by your tools; never invent
cities, venues, numbers or artists that the tools did not return.

Be fast: call independent tools in the same turn.
Turn 1: resolve_artists, find_similar_artists and map_fan_cities together
        (max_cities about num_shows + 4). If no artist resolves, finalize_plan.
Turn 2: choose exactly num_shows cities and call plan_route. Prefer the best
        fan_fit_rank, but use judgement: favour hidden markets (cities that rank
        far higher for this sound than their size), skip near-duplicate markets,
        and include the home city only if it ranks well.
Turn 3: city_deep_dive for every route city, plus fan_crossover, all together.
Turn 4: finalize_plan. Headline: one sentence naming the sharpest finding (for
        example, a small city that out-ranks a big one). For each city, "why"
        cites its Fan Fit rank (e.g. "#4 of 491") and whether it is a hidden
        market; "promo" is one specific idea combining that city's co-headliner,
        one named place from its venues and, if it fits, a crossover film or brand.
        Caveat: Qloo shows aggregate taste affinity, not ticket demand or fees.
        Also list 2-4 "decisions": strong cities you left out (and why, e.g. too close
        to another stop) and lower-ranked or hidden-market cities you chose (and why).
        Every reason must rest only on facts from the tools: Fan Fit rank, size rank,
        hidden-market status, or distance to another stop. Do not claim audiences are
        "underserved", "growing" or "loyal"; the data doesn't say that.
Keep your own messages to one short sentence."""


async def run_plan(st: PlanState) -> None:
    started = time.time()
    used_llm = False
    # The AI-only baseline runs alongside the real plan, for the comparison panel.
    baseline = (asyncio.create_task(llm_only_guess(st.artist_names, st.country, st.num_shows))
                if config.LLM_ENABLED else None)
    if config.LLM_ENABLED:
        await st.emit("trace", {"kind": "note", "text": "The AI agent is planning your tour."})
        try:
            await asyncio.wait_for(_llm_loop(st), timeout=config.AGENT_BUDGET_S)
            used_llm = True
        except asyncio.TimeoutError:
            log.warning("Agent exceeded %ss; finishing deterministically", config.AGENT_BUDGET_S)
            await st.emit("trace", {"kind": "note", "text": "Finishing the plan from Qloo data."})
        except Exception as e:  # keep the demo alive whatever the model does
            log.warning("Agent model error, using fallback: %s: %s", type(e).__name__, str(e)[:300])
            await st.emit("trace", {"kind": "note", "text": "Finishing the plan from Qloo data."})
    else:
        await st.emit("trace", {"kind": "note", "text": "Planning your tour from Qloo data."})
    await _complete_missing(st)
    if baseline:
        try:
            st.llm_cities = await asyncio.wait_for(baseline, timeout=15)
        except Exception:
            baseline.cancel()
    build_comparison(st)
    await st.emit("done", {**st.snapshot(), "calls": st.qloo.calls,
                           "agent": "llm" if used_llm else "deterministic",
                           "seconds": round(time.time() - started, 1)})


async def _call_tool(st: PlanState, name: str, args: dict) -> dict:
    call_id = f"t{next(_call_ids)}"
    await st.emit("trace", {"kind": "tool", "name": name, "args": args, "id": call_id})
    token = CURRENT_STEP.set(call_id)
    try:
        result = await TOOLS[name](st, **args)
    except TypeError as e:
        result = {"error": f"Bad arguments for {name}: {e}"}
    finally:
        CURRENT_STEP.reset(token)
    await st.emit("trace", {"kind": "result", "name": name, "id": call_id,
                            "qloo_calls": sum(1 for c in st.qloo.calls if c.get("step") == call_id),
                            "summary": summarize(result)[:400]})
    return result


async def _llm_loop(st: PlanState) -> None:
    client = _client()
    brief = {
        "artists": st.artist_names, "country": st.country, "home_city": st.home_city,
        "num_shows": st.num_shows, "audience_age": config.AGE_BUCKETS.get(st.age, "All ages"),
    }
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "Plan my tour: " + json.dumps(brief)}]
    for _ in range(config.MAX_AGENT_STEPS):
        resp = await _complete(client, model=config.LLM_MODEL, messages=messages,
                               tools=TOOL_SCHEMAS, tool_choice="auto", temperature=0.3)
        msg = resp.choices[0].message
        if msg.content and msg.content.strip():
            await st.emit("trace", {"kind": "think", "text": msg.content.strip()[:500]})
        if not msg.tool_calls:
            break
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        async def run_one(tc):
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if name not in TOOLS:
                return {"error": f"Unknown tool {name}"}
            return await _call_tool(st, name, args)

        # Independent calls in one turn (e.g. several city deep dives) run in parallel;
        # finalize_plan waits until the others are done.
        # resolve_artists runs first because the other tools need its results.
        first = [tc for tc in msg.tool_calls if tc.function.name == "resolve_artists"]
        last = [tc for tc in msg.tool_calls if tc.function.name == "finalize_plan"]
        normal = [tc for tc in msg.tool_calls if tc not in first and tc not in last]
        results = {}
        for tc in first:
            results[tc.id] = await run_one(tc)
        results.update(zip([tc.id for tc in normal], await asyncio.gather(*(run_one(tc) for tc in normal))))
        for tc in last:
            results[tc.id] = await run_one(tc)
        for tc in msg.tool_calls:
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": summarize(results[tc.id])})
        if st.final:
            break


async def _complete_missing(st: PlanState) -> None:
    """Run any step the agent skipped, so the plan is always complete."""
    if not st.seeds:
        await _call_tool(st, "resolve_artists", {})
    if not st.seeds:
        await st.emit("trace", {"kind": "note", "text": "None of the artists were found in Qloo."})
        if not st.final:
            st.final = {"headline": "We couldn't find these artists in Qloo's taste graph. "
                                    "Try entering better-known artists you sound like.",
                        "city_notes": [], "caveats": []}
        return
    if not st.similar:
        await _call_tool(st, "find_similar_artists", {})
    if not st.ranked_cities:
        await _call_tool(st, "map_fan_cities", {"max_cities": st.num_shows + 4})
    if not st.ranked_cities:
        return
    if not st.route.get("stops"):
        picks = [c["city"] for c in st.ranked_cities[:st.num_shows]]
        await _call_tool(st, "plan_route", {"cities": picks})
    pending = [_call_tool(st, "city_deep_dive", {"city": s["city"]})
               for s in st.route.get("stops", []) if s["city"] not in st.city_details]
    if not st.crossover:
        pending.append(_call_tool(st, "fan_crossover", {}))
    await asyncio.gather(*pending)
    _fill_notes(st)


def _fill_notes(st: PlanState) -> None:
    """Template copy for any city the agent didn't write notes for."""
    final = st.final or {}
    given = final.get("city_notes", [])
    full = {n.get("city", "").strip().lower(): n for n in given}
    shorts: dict[str, list] = {}
    for n in given:
        shorts.setdefault(n.get("city", "").split(",")[0].strip().lower(), []).append(n)
    brands = [b["name"] for b in st.crossover.get("brands", [])]
    films = [m["name"] for m in st.crossover.get("movies", [])]
    out = []
    for i, stop in enumerate(st.route.get("stops", [])):
        # Match the agent's note by full name; by short name only if unambiguous
        # (two Portlands must not share one note).
        same_short = [x for x in st.route["stops"]
                      if x["city"].split(",")[0].lower() == stop["city"].split(",")[0].lower()]
        note = full.get(stop["city"].lower())
        if not note and len(same_short) == 1:
            cands = shorts.get(stop["city"].split(",")[0].lower(), [])
            note = cands[0] if len(cands) == 1 else None
        if note:
            out.append({**note, "city": stop["city"]})
            continue
        d = st.city_details.get(stop["city"], {})
        why = _why(stop)
        co = (d.get("co_headliners") or [{}])[0].get("name")
        if co:
            why += f" {co} has the strongest local following among similar artists."
        promo = _promo(i, co, [v["name"] for v in d.get("venues") or []], films, brands)
        out.append({"city": stop["city"], "why": why, "promo": promo})
    seeds = ", ".join(s["name"] for s in st.seeds)
    who = f"{st.artist_label}'s" if st.artist_label else "A"
    headline = None if final.get("auto_headline") else final.get("headline")
    st.final = {
        "auto_headline": not headline,
        "headline": headline or
        f'{who} {len(out)}-city {st.country} tour, built around where fans of {seeds} actually are.',
        "city_notes": out,
        "decisions": _decisions(st, final.get("decisions") or []),
        "caveats": final.get("caveats") or [
            "Qloo shows aggregate taste affinity, not ticket demand, capacity or artist fees.",
            "Check venue availability and local promoters before booking."],
    }


def _decisions(st: PlanState, given: list[dict]) -> list[dict]:
    """Keep the agent's decisions only if they name real cities; otherwise use the
    planner's own explanation, so every listed decision is grounded in data."""
    from .tools import explain_decisions, find_scored
    on = {x["city"] for x in st.route.get("stops", [])}
    kept = []
    for d in given:
        if not isinstance(d, dict) or not d.get("reason") or not d.get("city"):
            continue
        c = find_scored(st, d["city"])
        if not c:
            continue
        action = "picked" if c["city"] in on else "skipped"
        if d.get("action") in ("picked", "skipped") and d["action"] != action:
            continue  # the tour was edited since; this reason no longer applies
        kept.append({"city": c["city"], "action": action, "reason": str(d["reason"])[:240], "by": "agent"})
    if kept:
        return kept[:4]
    return [{**d, "by": "planner"} for d in explain_decisions(st)]


def _why(stop: dict) -> str:
    rank, n = stop.get("fit_rank"), stop.get("markets")
    if not rank:
        return f'{stop["match"]}% fan affinity on Qloo\'s heatmap.'
    text = f"Ranks #{rank} of {n} markets for this sound (top {stop.get('top_pct')}%)."
    if stop.get("hidden"):
        text += (f" A hidden market: it's only #{stop['size_rank']} by size, so a "
                 "biggest-cities tour would never stop here.")
    return text


def _promo(i: int, co: str | None, venues: list[str], films: list[str], brands: list[str]) -> str:
    """A specific idea built from this city's own Qloo results, varied per stop."""
    venue = venues[i % len(venues)] if venues else None
    film = films[i % len(films)] if films else None
    brand = brands[i % len(brands)] if brands else None
    options = []
    if co and venue:
        options.append(f"Co-bill with {co}, and add an in-store or acoustic set at {venue} the afternoon of the show.")
    if venue and film:
        options.append(f'Host a "{film}" screening at or near {venue}, with a short set before the film.')
    if co and brand:
        options.append(f"Share the bill with {co} and pitch {brand} to sponsor the night; both over-index with these fans.")
    if venue:
        options.append(f"Do a pop-up or signing at {venue}, a spot these fans already go to.")
    if not options:
        return "Target ads at the fan hotspots marked on the map in the two weeks before the show."
    return options[i % len(options)]


async def llm_only_guess(artists: list[str], country: str, n: int) -> list[str] | None:
    """Baseline for the 'without Qloo' comparison: the same model, no taste data."""
    if not config.LLM_ENABLED:
        return None
    client = _client()
    prompt = (f"Without any external data, list the {n} best cities in {country} for a tour by "
              f"an artist who sounds like {', '.join(artists)}. Reply with only a JSON array of city names.")
    try:
        resp = await asyncio.wait_for(_complete(
            client, model=config.LLM_MODEL, temperature=0.2,
            messages=[{"role": "user", "content": prompt}]), timeout=config.LLM_TIMEOUT_S)
        text = (resp.choices[0].message.content or "").strip().strip("`")
        text = text[text.find("["): text.rfind("]") + 1]
        cities = json.loads(text)
        return [str(c) for c in cities][:n]
    except Exception as e:
        log.warning("AI-only comparison skipped: %s: %s", type(e).__name__, str(e)[:200])
        return None

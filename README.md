# Tourcast

**Tour where your sound is already loved.**

Tourcast is an AI agent for independent musicians. Enter the artists you sound
like (no streams of your own needed) and a country. It uses Qloo's taste graph
to rank every market in that country for your sound, finds the *hidden
markets* (small cities that rank far above their size), picks the cities,
orders the route, zooms into the fan neighbourhoods in each city, finds places
those fans like and the best local act to share the bill with, and writes a
promotion plan for every stop.

Live demo: https://tourcast-12ym.onrender.com

## The problem

Most independent artists book tours on guesswork: the big cities, the places
friends have played, or where a promoter happens to reply. Major artists have
data teams that know where their listeners are. Small artists play half-empty
rooms in the wrong cities and lose money on travel.

An LLM on its own can't fix this. Ask it where fans of a niche singer-songwriter
live and it defaults to the largest cities. Tourcast answers from Qloo's taste
data instead: for Phoebe Bridgers, Qloo's heatmap points to Western
Massachusetts college towns, Burlington (Vermont) and both Portlands rather
than simply New York and Los Angeles.

## What the agent does

| Step | Agent tool | Qloo endpoint and parameters |
|---|---|---|
| Find the artists | `resolve_artists` | `GET /search` with `types=urn:entity:artist` |
| Find artists with overlapping fans; flag rising ones | `find_similar_artists` | `GET /v2/insights`, `filter.type=urn:entity:artist`, `signal.interests.entities`, `filter.exclude.entities`, compared with and without `bias.trends=high` |
| Map where the fans are | `map_fan_cities` | `GET /v2/insights`, `filter.type=urn:heatmap`, `filter.location.query=<country>`, optional `signal.demographics.age` |
| Choose cities and route | `plan_route` | Uses the ranked heatmap cities (no extra calls) |
| Fan hotspots inside each city | `city_deep_dive` | `urn:heatmap` with `filter.location.query=<city, country>` |
| Venues those fans like | `city_deep_dive` | `filter.type=urn:entity:place` with a `filter.location` WKT point and radius at the strongest hotspot (falls back to other location parameters) |
| Best local co-headliner | `city_deep_dive` | Country heatmaps for the top similar artists, read at each tour city |
| Sponsorship and promo leads | `fan_crossover` | `urn:entity:brand`, `urn:entity:movie`, `urn:entity:tv_show` with the artists as signals |

The LLM (any OpenAI-compatible model, Gemini by default) decides which tools to
call and in what order, chooses the final cities with judgement (for example,
skipping a near-duplicate market) and writes the reasoning and promo idea for
each stop. Every number it cites comes from a tool result.

The page also shows:
- **A one-line finding** at the top, e.g. "Skip Houston (#279 for this sound) /
  Play Burlington, VT (#4)" for Phoebe Bridgers.
- **Fan Fit rank** for every city: its rank among all markets Qloo has signal
  for, with a percentile, and bars comparing fit with size.
- **Hidden markets:** cities in the top 8% for the sound, at least three times
  lower by size rank, and under 400,000 people. They're badged in the tour,
  circled on the map, and can be added with one click.
- **Sound blender:** with two "sounds like" artists, a slider re-weights the mix;
  each artist's own heatmap is scored per city, blended, and the tour re-planned.
- **Why not just play the biggest cities?** Three plans scored with the same
  Qloo heatmap: Tourcast's, the country's biggest cities, and the same LLM
  picking cities with no Qloo data, compared by median Fan Fit rank.
- **Tour editing:** remove a stop or add another strong market or hidden market.
- **Tour poster:** a downloadable gig poster (PNG) drawn from the plan: the skip/play finding, every stop with its Fan Fit rank, hidden markets in pink, and the comparison claim.
- **Share link, Copy plan and Print:** every plan gets a /p/<id> link, a
  plain-text version for a booking agent, and a print layout for PDF.
- **How the agent decided:** the judgement calls behind the route (strong cities
  skipped because another stop covers the same fans, hidden markets picked),
  each checked against the data before it's shown.
- **Every Qloo request behind the plan**, with its parameters, for provenance.

The agent runs independent tool calls in parallel (for example, all city deep
dives at once). If the model is slow or fails, a time limit hands over to a
fixed-order planner that uses the same tools, so a plan always completes.

## Blind test: does "sounds like" find real fans?

Tourcast's central promise is that a new act with no streams can plan from the
artists they sound like. `scripts/blind_test.py` tests that: for each of 12
artists it plans a tour from only the 3 artists Qloo rates most similar (never
the artist's own data), then ranks those cities in the artist's own Qloo fan
map, against a biggest-cities tour and random tours. Results are in
[evidence.md](evidence.md) and on the results page.

## How a request becomes a result

1. Heatmap: Qloo returns thousands of grid points, each with an `affinity`
   score for the artist's audience.
2. Tourcast snaps each point to the nearest real city using offline GeoNames
   data (suburbs and neighbourhoods are merged into their metro), and scores
   each city by the average affinity of its three strongest points. That score
   is the "fan match" percentage shown on the tour list.
3. Cities closer than 120 km to a higher-ranked city are skipped so the tour
   doesn't play the same market twice.
4. The route is ordered by nearest-neighbour from the home city.
5. Inside each city, the three strongest heatmap points at least 1.5 km apart
   become the fan hotspots on the map. Places are fetched around the strongest
   hotspot, and places unsuitable for a gig or pop-up (memorials, hospitals,
   places of worship and so on) are filtered out.
6. A co-headliner's local strength is the average of their five strongest
   heatmap points within 40 km of the city.

## Run it locally

Requires Python 3.10 or newer.

```bash
git clone <your repo URL>
cd tourcast
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
# edit .env: add QLOO_API_KEY and (optionally) LLM_API_KEY
uvicorn app.main:app --reload
```

Open http://localhost:8000.

- Without `QLOO_API_KEY` the app runs on synthetic demo data and shows a
  "Demo data" badge.
- Without `LLM_API_KEY` the same tools run in a fixed order and the notes use
  templates; the "AI without Qloo" column is hidden.
- Gemini "lite" models (for example `gemini-3.1-flash-lite`) are the fastest
  choice for the agent.

`python scripts/smoke_test.py` runs one full plan from the command line.
After deploying, `python scripts/warm_cache.py https://your-app.onrender.com`
pre-runs the example tours so the first visitors get fast, cached results.
`python scripts/blind_test.py` runs the blind test (about 50 Qloo requests) and
updates `static/evidence.json` and `evidence.md`.

## Deploy (Render)

1. Push this repo to GitHub.
2. On render.com: **New → Web Service**, connect the repo (or paste its URL under
   Public Git Repository). Alternatively, **New → Blueprint** uses `render.yaml`.
3. Settings:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add environment variables: `QLOO_API_KEY`, `LLM_API_KEY`,
   `LLM_MODEL=gemini-3.1-flash-lite`, `PYTHON_VERSION=3.12.7`.
   Keys never reach the browser; the frontend only talks to this backend.

## Project layout

```
app/
  main.py       FastAPI app: streams agent progress as NDJSON
  agent.py      LLM tool-calling loop + deterministic fallback
  tools.py      Agent tools, each wrapping Qloo calls
  qloo.py       Qloo API client with caching and call log
  geo.py        Heatmap points -> ranked cities, route, hotspots
  mock_data.py  Synthetic data for demo mode
static/         Frontend (HTML, CSS, JS, Leaflet map)
```
## Credits

- Taste data: Qloo Insights API.
- City data: GeoNames (geonames.org), licensed under CC BY 4.0.
- Map tiles: Esri; map data © OpenStreetMap contributors.
   
## License

MIT. See [LICENSE](LICENSE).

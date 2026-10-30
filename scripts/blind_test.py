"""Blind test: can Tourcast find an artist's real fans from "sounds like" artists alone?

For each test artist A:
  1. Ask Qloo for the 3 artists most similar to A (A itself excluded).
  2. Plan a 5-city tour from those 3 artists only, the way a new act with no
     streams would use Tourcast. A's own data is never used for planning.
  3. Score those 5 cities against A's own Qloo heatmap (Fan Fit rank among all
     markets with signal for A), and do the same for a biggest-cities tour and
     for random tours.

Lower ranks are better. The output is aggregate ranks only (no raw Qloo data),
written to static/evidence.json (shown in the app) and evidence.md (for the
README and Devpost).

Usage (with QLOO_API_KEY in .env):
    python scripts/blind_test.py            # full test, about 50 Qloo requests
    python scripts/blind_test.py --quick    # 4 artists
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import geo  # noqa: E402
from app.qloo import Qloo  # noqa: E402

N_STOPS = 5
ARTISTS = [
    ("Phoebe Bridgers", "United States"),
    ("Noah Kahan", "United States"),
    ("Mitski", "United States"),
    ("Hozier", "United States"),
    ("Khruangbin", "United States"),
    ("Tyler Childers", "United States"),
    ("Vampire Weekend", "United States"),
    ("Leon Bridges", "United States"),
    ("Anuv Jain", "India"),
    ("Prateek Kuhad", "India"),
    ("Arlo Parks", "United Kingdom"),
    ("Fontaines D.C.", "United Kingdom"),
]


def rank_in(target_scored: list[dict], city: dict) -> int | None:
    hit = geo.lookup_score(target_scored, city["city"] if "city" in city else city["label"],
                           city["lat"], city["lng"])
    return hit["fit_rank"] if hit else None


def median_rank(ranks: list[int | None], miss: int) -> float:
    return statistics.median(r if r is not None else miss for r in ranks)


async def test_artist(q: Qloo, name: str, country: str, rng: random.Random) -> dict | None:
    artist = await q.search_artist(name)
    if not artist:
        print(f"  {name}: not found in Qloo, skipped")
        return None
    similar = await q.insights("urn:entity:artist", **{
        "signal.interests.entities": artist["id"], "filter.exclude.entities": artist["id"], "take": 6})
    similar = [s for s in similar if s.get("id") and s["name"].lower() != name.lower()][:3]
    if len(similar) < 2:
        print(f"  {name}: too few similar artists, skipped")
        return None

    truth_pts, blind_pts = await asyncio.gather(
        q.heatmap([artist["id"]], country), q.heatmap([s["id"] for s in similar], country))
    truth = geo.score_cities(truth_pts, country)
    blind = geo.score_cities(blind_pts, country)
    if len(truth) < 20 or not blind:
        print(f"  {name}: not enough heatmap data in {country}, skipped")
        return None
    miss = len(truth) + 1  # a city with no signal for the artist counts as last

    picks = geo.rank_cities(blind_pts, country, max_cities=N_STOPS, scored=blind)
    big = geo.biggest_markets(country, N_STOPS)
    t_ranks = [rank_in(truth, c) for c in picks]
    b_ranks = [rank_in(truth, {"city": c["label"], "lat": c["lat"], "lng": c["lng"]}) for c in big]
    rand_meds = [median_rank([c["fit_rank"] for c in rng.sample(truth, N_STOPS)], miss) for _ in range(500)]

    row = {
        "artist": artist["name"], "country": country, "markets": len(truth),
        "planned_from": [s["name"] for s in similar],
        "tourcast": [{"city": c["city"], "rank": r} for c, r in zip(picks, t_ranks)],
        "biggest": [{"city": c["label"], "rank": r} for c, r in zip(big, b_ranks)],
        "tourcast_median": median_rank(t_ranks, miss),
        "biggest_median": median_rank(b_ranks, miss),
        "random_median": round(statistics.median(rand_meds)),
        "top20_hits": sum(1 for r in t_ranks if r is not None and r <= 20),
    }
    print(f"  {row['artist']} ({country}, {row['markets']} markets) from {', '.join(row['planned_from'])}: "
          f"Tourcast #{row['tourcast_median']:g}, biggest #{row['biggest_median']:g}, random #{row['random_median']}")
    return row


async def main() -> None:
    quick = "--quick" in sys.argv
    q = Qloo()
    if q.mock:
        print("WARNING: no QLOO_API_KEY found, so this run uses synthetic demo data. "
              "Its numbers mean nothing; don't publish them.\n")
    rng = random.Random(42)
    artists = ARTISTS[:4] if quick else ARTISTS
    rows = []
    for name, country in artists:
        try:
            row = await test_artist(q, name, country, rng)
        except Exception as e:  # one failure shouldn't stop the test
            print(f"  {name}: failed ({e})")
            row = None
        if row:
            rows.append(row)
    if not rows:
        sys.exit("No artist could be tested. Check the Qloo key.")

    fmt = lambda x: int(x) if float(x).is_integer() else round(x, 1)  # noqa: E731
    wins = sum(1 for r in rows if r["tourcast_median"] < r["biggest_median"])
    summary = {
        "artists": len(rows),
        "tourcast_median": fmt(statistics.median(r["tourcast_median"] for r in rows)),
        "biggest_median": fmt(statistics.median(r["biggest_median"] for r in rows)),
        "random_median": fmt(statistics.median(r["random_median"] for r in rows)),
        "beat_biggest": wins,
        "top20_hits": sum(r["top20_hits"] for r in rows),
        "top20_total": N_STOPS * len(rows),
        "demo_data": q.mock,
        "qloo_requests": len(q.calls),
    }
    for r in rows:
        r["tourcast_median"], r["biggest_median"] = fmt(r["tourcast_median"]), fmt(r["biggest_median"])

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "evidence.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, indent=1)

    lines = [
        "## Blind test",
        "",
        f"Tourcast planned a {N_STOPS}-city tour for each artist using only the 3 artists Qloo rates "
        "most similar to them, never the artist's own data. The cities were then ranked in the "
        "artist's own Qloo fan map (lower is better).",
        "",
        f"**Across {summary['artists']} artists, Tourcast's blind picks ranked a median "
        f"#{summary['tourcast_median']} in the artist's real fan map, vs #{summary['biggest_median']} "
        f"for a biggest-cities tour and #{summary['random_median']} for a random tour. "
        f"Tourcast beat the biggest-cities tour for {wins} of {summary['artists']} artists; "
        f"{summary['top20_hits']} of {summary['top20_total']} blind picks were in the artist's own top 20.**",
        "",
        "| Artist | Planned from | Markets | Tourcast | Biggest cities | Random |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['artist']} ({r['country']}) | {', '.join(r['planned_from'])} | {r['markets']} | "
                     f"#{r['tourcast_median']} | #{r['biggest_median']} | #{r['random_median']} |")
    lines += ["", "Limitations: the artist's fan map is Qloo's aggregate taste affinity, not ticket sales; "
              "similar artists come from Qloo, so their fans overlap by design. The test shows that "
              "borrowing similar artists' geography recovers an artist's own geography, not that the "
              "tours would sell out."]
    with open(os.path.join(root, "evidence.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("\n" + "\n".join(lines[4:6]))
    print(f"\nSaved static/evidence.json (shown in the app) and evidence.md. "
          f"{summary['qloo_requests']} Qloo requests.")


if __name__ == "__main__":
    asyncio.run(main())

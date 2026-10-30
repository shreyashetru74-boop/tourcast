"""Pre-run the example tours so judges get fast, cached results.

Run once after each deploy:
    python scripts/warm_cache.py https://your-app.onrender.com
(or with no argument for a local server on port 8000)
"""
import json
import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")

EXAMPLES = [
    {"artists": ["Phoebe Bridgers", "Bon Iver"], "artist_label": "Juniper Lane",
     "country": "United States", "home_city": "Los Angeles", "num_shows": 5},
    {"artists": ["Phoebe Bridgers"], "country": "United States", "home_city": "Los Angeles", "num_shows": 5},
    {"artists": ["Anuv Jain", "Prateek Kuhad"], "country": "India", "home_city": "Bengaluru", "num_shows": 5},
    {"artists": ["Arlo Parks"], "country": "United Kingdom", "home_city": "London", "num_shows": 5},
]

for ex in EXAMPLES:
    started = time.time()
    try:
        with httpx.stream("POST", f"{BASE}/api/plan", json=ex, timeout=240) as r:
            done = None
            for line in r.iter_lines():
                if line:
                    ev = json.loads(line)
                    if ev["event"] == "done":
                        done = ev["data"]
        stops = [s["city"] for s in (done or {}).get("route", {}).get("stops", [])]
        print(f"{', '.join(ex['artists'])}: {time.time() - started:.0f}s -> {stops}")
    except Exception as e:  # keep warming the others
        print(f"{', '.join(ex['artists'])}: failed ({e})")

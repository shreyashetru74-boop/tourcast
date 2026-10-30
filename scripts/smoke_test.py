"""Runs one full plan through the API and prints the event stream summary.
Usage: python scripts/smoke_test.py  (uses mock data if QLOO_API_KEY is unset)"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app

c = TestClient(app)
print("config:", c.get("/api/config").json())
body = {"artists": sys.argv[1:] or ["Phoebe Bridgers"], "country": "United States",
        "home_city": "Los Angeles", "num_shows": 5}
if body["artists"][0] == "india":
    body = {"artists": ["Anuv Jain", "Prateek Kuhad"], "country": "India",
            "home_city": "Bengaluru", "num_shows": 5}
with c.stream("POST", "/api/plan", json=body) as r:
    for line in r.iter_lines():
        if not line: continue
        ev = json.loads(line)
        k, d = ev["event"], ev["data"]
        if k == "trace":
            print("TRACE", d.get("kind"), d.get("name", ""), (d.get("summary") or d.get("text") or json.dumps(d.get("args", "")))[:150])
        elif k == "done":
            print("DONE agent=", d["agent"], "calls=", d["qloo_calls"], "secs=", d["seconds"])
            print("route:", [(s["city"], s["match"], s["leg_km"]) for s in d["route"]["stops"]], d["route"]["total_km"], "km")
            for n in d["final"]["city_notes"]:
                print(" -", n["city"], "|", n["why"][:90], "|", n["promo"][:80])
            for city, det in d["city_details"].items():
                print("  ", city, "venues:", [v["name"] for v in det["venues"]][:3], "co:", [x["name"] for x in det["co_headliners"]])
        elif k == "error":
            print("ERROR", d)

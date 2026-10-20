"""Timing: instant push, missed push (catch-up), and a burst of edits. Board catch-up interval = 10 s as in production."""
import json, time, urllib.request
B, S = "http://127.0.0.1:" + __import__("os").getenv("BOARD_PORT", "8099"), "http://127.0.0.1:" + __import__("os").getenv("MOCK_PORT", "8090")
WK, W8 = "2026-10-19", 74
fails = []


def req(url, method="GET", body=None):
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as res:
        return json.loads(res.read() or b"{}")


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ("  -- " + str(detail) if detail else ""), flush=True)
    if not ok:
        fails.append(label)


def until(cond, secs=40):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if cond():
                return time.time() - t0
        except Exception:
            pass
        time.sleep(0.1)
    return None


def vals(names, day):  # one board request per check
    s = req(f"{B}/api/state?week={WK}")
    m = next(x for x in s["members"] if x["name"] == "ORLANDO")
    lg = (s["logs"].get(m["id"]) or {}).get("values") or {}
    return [lg.get(next(x["id"] for x in m["tasks"] if x["name"] == n), [0] * 7)[day] for n in names]


def val(name, day):
    return vals([name], day)[0]


rows = req(f"{S}/dump")["rows"]
row = lambda name: next(r for r in rows if r["a"] == name)["row"]

req(f"{S}/edit", "POST", {"row": row("AI Research"), "col": W8 + 1, "value": 3})
t = until(lambda: val("AI Research", 0) == 3)
check("instant push: sheet edit on the board within 3 s", t is not None and t < 3, "%.1f s" % (t or -1))

req(f"{S}/edit", "POST", {"row": row("AI Research"), "col": W8 + 2, "value": 5, "noTrigger": True})
t = until(lambda: val("AI Research", 1) == 5)
check("missed push: catch-up delivers within 15 s", t is not None and t < 15, "%.1f s" % (t or -1))

for i, name in enumerate(["SMO, Meta ads and site build", "Copywriting and scripting", "Mino's Content Academy", "Personal brand research", "IG Reels posted"]):
    req(f"{S}/edit", "POST", {"row": row(name), "col": W8 + 3, "value": i + 1, "noTrigger": i % 2 == 1})
t = until(lambda: vals(["SMO, Meta ads and site build", "Copywriting and scripting", "Mino's Content Academy", "Personal brand research", "IG Reels posted"], 2) == [1, 2, 3, 4, 5])
check("burst of 5 edits (2 with missed pushes): all on the board within 15 s", t is not None and t < 15, "%.1f s" % (t or -1))
print("FAILURES:", fails or "none")

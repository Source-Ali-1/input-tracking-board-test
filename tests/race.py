"""A sheet edit whose trigger runs late must not delete a board entry made in the meantime (both sides)."""
import json, time, urllib.request
B, S = "http://localhost:" + __import__("os").getenv("BOARD_PORT", "8099"), "http://localhost:" + __import__("os").getenv("MOCK_PORT", "8090")
WK, W8 = "2026-10-19", 74
fails = []
def req(url, method="GET", body=None):
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as res: return json.loads(res.read() or b"{}")
def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ("  -- " + str(detail) if detail and not ok else ""), flush=True)
    if not ok: fails.append(label)
def wait(cond, secs=30):
    for _ in range(secs * 4):
        try:
            if cond(): return True
        except Exception: pass
        time.sleep(0.25)
    return False
def orl():
    s = req(f"{B}/api/state?week={WK}"); m = next(x for x in s["members"] if x["name"] == "ORLANDO"); return m, s["logs"].get(m["id"])
rows = req(f"{S}/dump")["rows"]; smo_row = next(r for r in rows if r["a"] == "SMO, Meta ads and site build")["row"]
m, lg = orl(); smo = next(t["id"] for t in m["tasks"] if t["name"].startswith("SMO"))
base = lg or {"cats": m["cats"], "tasks": m["tasks"], "values": {}}
def board_put(day, v):
    m, lg = orl(); lg = lg or base
    vals = dict(lg["values"]); row = list(vals.get(smo) or [0] * 7); row[day] = v; vals[smo] = row
    req(f"{B}/api/logs/{m['id']}/{WK}", "PUT", {"cats": lg["cats"], "tasks": lg["tasks"], "values": vals, "changed": {"cells": [[smo, day]]}})
board_val = lambda day: (orl()[1]["values"].get(smo) or [0] * 7)[day]
sheet_val = lambda day: req(f"{S}/cell?r={smo_row}&c={W8 + 1 + day}")

# start: Wed = 1 everywhere
board_put(2, 1)
check("setup: Wed = 1 on both sides", wait(lambda: sheet_val(2) == 1))
# 1) sheet: user clears Wed now, but Google runs the trigger only later
req(f"{S}/editQueued", "POST", {"row": smo_row, "col": W8 + 3, "value": ""})
time.sleep(1.2)
# 2) board: user enters Wed = 2 after that (the sheet's script is busy, so it can't take it yet)
req(f"{S}/down?v=1")
board_put(2, 2)
time.sleep(1)
# 3) the late trigger runs, then the sheet frees up and gets the board's entry
req(f"{S}/runQueued", "POST", {})
check("late sheet clear doesn't delete the newer board entry (board)", board_val(2) == 2, board_val(2))
req(f"{S}/down?v=0")
check("the newer board entry reaches the sheet", wait(lambda: sheet_val(2) == 2, 40), sheet_val(2))
time.sleep(13)
check("after catch-up: both sides still 2", board_val(2) == 2 and sheet_val(2) == 2, (board_val(2), sheet_val(2)))

# vice versa: board clears first, sheet types later (trigger late) -> the sheet's later value wins
board_put(3, 5)
wait(lambda: sheet_val(3) == 5)
board_put(3, 0)                      # board clears Thu
wait(lambda: sheet_val(3) in (0, ""))
time.sleep(1.2)
req(f"{S}/editQueued", "POST", {"row": smo_row, "col": W8 + 4, "value": 7})   # later: sheet types 7, trigger delayed
time.sleep(1)
req(f"{S}/runQueued", "POST", {})
check("later sheet entry wins over an earlier board clear (board)", wait(lambda: board_val(3) == 7), board_val(3))
time.sleep(13)
check("after catch-up: both sides 7", board_val(3) == 7 and sheet_val(3) == 7, (board_val(3), sheet_val(3)))
print("FAILURES:", fails or "none")

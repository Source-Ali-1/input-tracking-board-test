"""Sheet-side: add a new person block by hand at the bottom, then check the board (and back)."""
import json, time, urllib.request
B, S = "http://localhost:8099", "http://localhost:8090"
fails = []


def req(url, method="GET", body=None):
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as res:
        return json.loads(res.read() or b"{}")


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label, flush=True)
    if not ok:
        fails.append(label)


def wait(cond, secs=30):
    for _ in range(secs * 2):
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


rows = req(f"{S}/dump")["rows"]
last = max(r["row"] for r in rows)
for i in range(6):   # the person types into fresh rows under the last block
    req(f"{S}/insertRow", "POST", {"after": last + i})
hdr = next(r for r in rows if r["a"] == "Input")["cells"]
W8 = 74
lines = [["NINA"], ["October 2026"], hdr[:W8 + 10], ["Habits"], ["Walk", "Health", "count"], ["Stretch", "", "count"]]
for i, vals in enumerate(lines):
    req(f"{S}/edit", "POST", {"row": last + 1 + i, "col": 1, "values": [vals]})


def nina():
    return next((m for m in req(f"{B}/api/state?week=2026-10-19")["members"] if m["name"] == "NINA"), None)


check("sheet new person block -> board member NINA", wait(lambda: nina() and [t["name"] for t in nina()["tasks"]] == ["Walk", "Stretch"]))
n = nina()
walk_row = last + 5
req(f"{S}/edit", "POST", {"row": walk_row, "col": W8, "values": [[5, 1]]})   # KPI 5, Mon 1
check("sheet value + KPI in the new block -> board", wait(lambda: (lambda s: s["logs"][n["id"]]["values"][n["tasks"][0]["id"]][0] == 1
      and next(t for t in s["logs"][n["id"]]["tasks"] if t["name"] == "Walk")["target"] == 5)(req(f"{B}/api/state?week=2026-10-19"))))
# vice versa: board edits NINA -> sheet
s = req(f"{B}/api/state?week=2026-10-19")
lg = s["logs"][n["id"]]
vals = dict(lg["values"])
vals[n["tasks"][1]["id"]] = [0, 0, 3, 0, 0, 0, 0]
req(f"{B}/api/logs/{n['id']}/2026-10-19", "PUT", {"cats": lg["cats"], "tasks": lg["tasks"], "values": vals})
check("board value for the new person -> sheet", wait(lambda: req(f"{S}/cell?r={walk_row + 1}&c={W8 + 3}") == 3))
time.sleep(12)
names = [m["name"] for m in req(f"{B}/api/state?week=2026-10-19")["members"]]
check("no duplicates after catch-up (one NINA, others intact)", names.count("NINA") == 1 and len(names) == 4)
print("FAILURES:", fails or "none")

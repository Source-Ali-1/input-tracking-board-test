"""The board page saves from a stale copy right after sheet edits: the sheet edits must survive (both sides)."""
import json, time, urllib.request, copy
B, S = "http://localhost:8099", "http://localhost:8090"
WK, W8 = "2026-10-19", 74
fails = []


def req(url, method="GET", body=None):
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as res:
        return json.loads(res.read() or b"{}")


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ("  -- " + str(detail) if detail and not ok else ""), flush=True)
    if not ok:
        fails.append(label)


def wait(cond, secs=20):
    for _ in range(secs * 4):
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def orl():
    s = req(f"{B}/api/state?week={WK}")
    m = next(x for x in s["members"] if x["name"] == "ORLANDO")
    return m, s["logs"].get(m["id"])


def tid(m, name):
    return next(t["id"] for t in m["tasks"] if t["name"] == name)


rows = req(f"{S}/dump")["rows"]
row = lambda name: next(r for r in rows if r["a"] == name)["row"]
m, lg = orl()
# the page's copy of the week, taken now (it will be stale in a moment)
if not lg:
    req(f"{B}/api/logs/{m['id']}/{WK}", "PUT", {"cats": m["cats"], "tasks": m["tasks"], "values": {}})
    m, lg = orl()
page_copy = copy.deepcopy(lg)
ai, smo, cw = tid(m, "AI Research"), tid(m, "SMO, Meta ads and site build"), tid(m, "Copywriting and scripting")

# sheet edits: a KPI and a value
req(f"{S}/edit", "POST", {"row": row("SMO, Meta ads and site build"), "col": W8, "values": [[12]]})
req(f"{S}/edit", "POST", {"row": row("AI Research"), "col": W8 + 2, "values": [[7]]})
check("sheet KPI + value reached the board", wait(lambda: (lambda m, lg: next(t for t in lg["tasks"] if t["id"] == smo)["target"] == 12
      and lg["values"][ai][1] == 7)(*orl())))

# the page (with its old copy) saves a different cell
vals = copy.deepcopy(page_copy["values"])
vals.setdefault(cw, [0] * 7)[3] = 2
r = req(f"{B}/api/logs/{m['id']}/{WK}", "PUT", {"cats": page_copy["cats"], "tasks": page_copy["tasks"], "values": vals, "changed": {"cells": [[cw, 3]]}})
m, lg = orl()
check("stale page save keeps the sheet's KPI on the board", next(t for t in lg["tasks"] if t["id"] == smo)["target"] == 12)
check("stale page save keeps the sheet's value on the board", lg["values"][ai][1] == 7)
check("the cell the page changed is saved", lg["values"][cw][3] == 2)
check("server answers with the merged week", next(t for t in r["log"]["tasks"] if t["id"] == smo)["target"] == 12)
time.sleep(13)
check("sheet still has its KPI and value; the page's cell reached it",
      req(f"{S}/cell?r={row('SMO, Meta ads and site build')}&c={W8}") == 12 and req(f"{S}/cell?r={row('AI Research')}&c={W8 + 2}") == 7
      and req(f"{S}/cell?r={row('Copywriting and scripting')}&c={W8 + 4}") == 2)

# Edit inputs → Save from a stale editor: only the KPI the user changed is applied
req(f"{S}/edit", "POST", {"row": row("AI Research"), "col": W8, "values": [[8]]})
wait(lambda: next(t for t in orl()[1]["tasks"] if t["id"] == ai)["target"] == 8)
m, lg = orl()
stale_tasks = [dict(t, target=(15 if t["id"] == cw else t["target"])) for t in page_copy["tasks"]]
req(f"{B}/api/members/{m['id']}", "PUT", {"name": m["name"], "cats": page_copy["cats"], "tasks": stale_tasks, "week": WK, "changedTargets": [cw]})
req(f"{B}/api/logs/{m['id']}/{WK}", "PUT", {"cats": page_copy["cats"], "tasks": stale_tasks, "values": page_copy["values"],
    "changed": {"structure": True, "targets": [cw]}})
m, lg = orl()
check("editor save applies the KPI the user changed", next(t for t in lg["tasks"] if t["id"] == cw)["target"] == 15)
check("editor save keeps a KPI the sheet changed meanwhile (week)", next(t for t in lg["tasks"] if t["id"] == ai)["target"] == 8)
check("editor save keeps the sheet's values", lg["values"][ai][1] == 7)
time.sleep(13)
check("sheet: AI Research KPI still 8, Copywriting KPI now 15",
      req(f"{S}/cell?r={row('AI Research')}&c={W8}") == 8 and req(f"{S}/cell?r={row('Copywriting and scripting')}&c={W8}") == 15,
      (req(f"{S}/cell?r={row('AI Research')}&c={W8}"), req(f"{S}/cell?r={row('Copywriting and scripting')}&c={W8}")))
st = req(f"{B}/api/sync/status")
check("converged", st["outbox"] == 0 and st["lastError"] is None, st)
print("FAILURES:", fails or "none")

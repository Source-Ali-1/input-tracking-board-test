"""Rows inserted next to a struck-through row: Google Sheets copies the strike onto a new row
inserted above or below it. The new row must not stay struck, must become a normal input (or
section) on the board, and the removed row next to it must stay removed."""
import json, time, urllib.request
B, S = "http://localhost:8099", "http://localhost:8090"
CW = "2026-10-19"
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


def wait(cond, secs=40):
    for _ in range(secs * 2):
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


state = lambda: req(f"{B}/api/state?week={CW}")
rows = lambda: req(f"{S}/dump")["rows"]
row_of = lambda tid: next(r for r in rows() if "itb_task=" + tid in r["md"])
row_at = lambda n: next((r for r in rows() if r["row"] == n), {"row": n, "strike": False})
member = lambda mid: next(m for m in state()["members"] if m["id"] == mid)

m = state()["members"][0]
mid = m["id"]
t = [x for x in m["tasks"] if x.get("type") != "check"][2]
tid = t["id"]
req(f"{B}/api/members/{mid}", "PUT", {"name": m["name"], "week": CW, "cats": m["cats"],
                                      "tasks": [x for x in m["tasks"] if x["id"] != tid], "changedTargets": []})
check("input removed on the board is struck through", wait(lambda: row_of(tid)["strike"] and "itb_archived=1" in row_of(tid)["md"]))

for where, name in (("above", "Inserted above"), ("below", "Inserted below")):
    r = row_of(tid)["row"]
    after = r - 1 if where == "above" else r
    req(f"{S}/insertRow", "POST", {"after": after, "inherit": "below" if where == "above" else "above"})
    new = after + 1
    check(f"{where}: the new row is no longer struck through after the insert", wait(lambda: not row_at(new)["strike"]), row_at(new))
    req(f"{S}/edit", "POST", {"row": new, "col": 1, "values": [[name, "", "count"]]})
    check(f"{where}: what's typed there is a new input on the board",
          wait(lambda: any(x["name"] == name for x in member(mid)["tasks"])), [x["name"] for x in member(mid)["tasks"]])
    check(f"{where}: and its row is not struck through", not next(x for x in rows() if x["a"] == name)["strike"])
    check(f"{where}: the removed row next to it is still struck and removed",
          row_of(tid)["strike"] and "itb_archived=1" in row_of(tid)["md"] and not any(x["id"] == tid for x in member(mid)["tasks"]))

# a section typed into an inserted row
r = row_of(tid)["row"]
req(f"{S}/insertRow", "POST", {"after": r, "inherit": "above"})
req(f"{S}/edit", "POST", {"row": r + 1, "col": 1, "value": "New section"})
check("section typed in an inserted row is not struck through",
      wait(lambda: not next(x for x in rows() if x["a"] == "New section")["strike"]))
check("and shows as a section on the board", wait(lambda: any(c["name"] == "New section" for c in member(mid)["cats"])),
      [c["name"] for c in member(mid)["cats"]])

time.sleep(12)
names = [x["name"] for x in member(mid)["tasks"]]
check("after a catch-up: both new inputs once each, the removed one still gone",
      names.count("Inserted above") == 1 and names.count("Inserted below") == 1 and not any(x["id"] == tid for x in member(mid)["tasks"]), names)
st = req(f"{B}/api/sync/status")
check("no sync error, nothing waiting", not st["lastError"] and st["outbox"] == 0, st)
print("FAILURES:", fails or "none")

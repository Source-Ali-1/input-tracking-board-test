"""Removed inputs come back from the sheet: an input removed on the board is struck through in the
sheet. Typing in that row (a value, or a new name) brings it back as a live input on both sides,
without the strike, and other people's rows are untouched."""
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
board_task = lambda mid, tid: next((t for t in next(m for m in state()["members"] if m["id"] == mid)["tasks"] if t["id"] == tid), None)
hdr = next(r for r in rows() if r["a"] == "Input")["cells"]
col = hdr.index("Mon 10/19") + 1  # Monday of this week, 1-based


def remove_on_board(mid, tid):
    m = next(x for x in state()["members"] if x["id"] == mid)
    req(f"{B}/api/members/{mid}", "PUT", {"name": m["name"], "week": CW, "cats": m["cats"],
                                          "tasks": [t for t in m["tasks"] if t["id"] != tid], "changedTargets": []})


for case, who in (("value", 0), ("rename", -1)):
    m = state()["members"][who]
    mid = m["id"]
    t = [x for x in m["tasks"] if x.get("type") != "check"][1]
    tid, name = t["id"], t["name"]
    remove_on_board(mid, tid)
    check(f"{case}: input removed on the board is struck through in the sheet",
          wait(lambda: row_of(tid)["strike"] and "itb_archived=1" in row_of(tid)["md"]))
    check(f"{case}: and gone from the board", board_task(mid, tid) is None)
    r = row_of(tid)["row"]
    if case == "value":
        req(f"{S}/edit", "POST", {"row": r, "col": col + 1, "value": 5})  # Tuesday
        check("value: typing a number in the struck row brings the input back on the board",
              wait(lambda: board_task(mid, tid) is not None))
        check("value: and the number arrives",
              wait(lambda: (state()["logs"][mid]["values"].get(tid) or [0] * 7)[1] == 5), state()["logs"][mid]["values"].get(tid))
    else:
        req(f"{S}/edit", "POST", {"row": r, "col": 1, "value": "Brand new input"})
        check("rename: typing a new name in the struck row brings it back under the new name",
              wait(lambda: (board_task(mid, tid) or {}).get("name") == "Brand new input"), board_task(mid, tid))
    after = row_of(tid)
    check(f"{case}: the row is no longer struck through or tagged removed",
          not after["strike"] and "itb_archived=1" not in after["md"], after)
    time.sleep(12)
    check(f"{case}: still on the board after a catch-up", board_task(mid, tid) is not None)

names_s = sorted(m["name"] for m in req(f"{S}/exec?secret=s3cret")["members"])
names_b = sorted(m["name"] for m in state()["members"])
check("one of each person on both sides", names_s == names_b and len(names_b) == len(set(names_b)), (names_s, names_b))
st = req(f"{B}/api/sync/status")
check("no sync error, nothing waiting", not st["lastError"] and st["outbox"] == 0, st)
print("FAILURES:", fails or "none")

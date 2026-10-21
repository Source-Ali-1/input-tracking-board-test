"""Removed inputs come back from the sheet with all their data: an input removed on the board is struck
through in the sheet and keeps every number. Bringing it back (typing a value, typing a new name,
taking the strike off by hand, or taking it off where no trigger reports it) puts it back on the
board with this week's numbers, its KPI and past weeks' numbers, and other people's rows are untouched."""
import json, time, urllib.request
B, S = "http://localhost:8099", "http://localhost:8090"
CW, PW = "2026-10-19", "2026-10-12"
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


state = lambda week=CW: req(f"{B}/api/state?week={week}")
rows = lambda: req(f"{S}/dump")["rows"]
cell = lambda r, c: req(f"{S}/cell?r={r}&c={c}")
row_of = lambda tid: next(r for r in rows() if "itb_task=" + tid in r["md"])
board_task = lambda mid, tid: next((t for t in next(m for m in state()["members"] if m["id"] == mid)["tasks"] if t["id"] == tid), None)
hdr = next(r for r in rows() if r["a"] == "Input")["cells"]
col = hdr.index("Mon 10/19") + 1   # Monday of this week, 1-based (its KPI is the column before)
pcol = hdr.index("Mon 10/12") + 1  # Monday of last week


def values(mid, tid, week=CW):
    return ((state(week)["logs"].get(mid) or {}).get("values") or {}).get(tid) or [0] * 7


def kpi(mid, tid, week=CW):
    lg = state(week)["logs"].get(mid) or {}
    return next((t.get("target") for t in lg.get("tasks", []) if t["id"] == tid), None)


def put_log(mid, week, tid, vals, target=None):
    s = state(week)
    m = next(x for x in s["members"] if x["id"] == mid)
    lg = s["logs"].get(mid) or {"cats": m["cats"], "tasks": m["tasks"], "values": {}}
    tasks = [dict(t, target=target) if target is not None and t["id"] == tid else t for t in lg["tasks"]]
    v = dict(lg.get("values") or {})
    v[tid] = vals
    req(f"{B}/api/logs/{mid}/{week}", "PUT", {"cats": lg["cats"], "tasks": tasks, "values": v})


def remove_on_board(mid, tid):
    m = next(x for x in state()["members"] if x["id"] == mid)
    req(f"{B}/api/members/{mid}", "PUT", {"name": m["name"], "week": CW, "cats": m["cats"],
                                          "tasks": [t for t in m["tasks"] if t["id"] != tid], "changedTargets": []})


for k, case in enumerate(("value", "rename", "unstrike", "unstrike-no-trigger")):
    members = state()["members"]
    m = members[k % len(members)]
    mid = m["id"]
    t = [x for x in m["tasks"] if x.get("type") != "check"][1 + k // len(members)]
    tid = t["id"]
    r = row_of(tid)["row"]
    # Numbers on the board first: this week (with its own KPI) and last week.
    put_log(mid, CW, tid, [3, 0, 4, 0, 0, 0, 0], target=9)
    put_log(mid, PW, tid, [1, 2, 0, 0, 0, 0, 0])
    check(f"{case}: this week's numbers and KPI reach the sheet",
          wait(lambda: cell(r, col) == 3 and cell(r, col + 2) == 4 and cell(r, col - 1) == 9),
          [cell(r, col - 1), cell(r, col), cell(r, col + 2)])
    check(f"{case}: last week's numbers reach the sheet", wait(lambda: cell(r, pcol) == 1 and cell(r, pcol + 1) == 2))

    remove_on_board(mid, tid)
    check(f"{case}: input removed on the board is struck through in the sheet",
          wait(lambda: row_of(tid)["strike"] and "itb_archived=1" in row_of(tid)["md"]))
    check(f"{case}: and gone from the board", board_task(mid, tid) is None)
    time.sleep(3)
    check(f"{case}: the struck row keeps this week's numbers and KPI in the sheet",
          cell(r, col) == 3 and cell(r, col + 2) == 4 and cell(r, col - 1) == 9,
          [cell(r, col - 1), cell(r, col), cell(r, col + 2)])
    time.sleep(10)  # a catch-up check passes
    check(f"{case}: still removed on the board after a catch-up", board_task(mid, tid) is None)
    check(f"{case}: and the sheet still has the numbers", cell(r, col) == 3 and cell(r, col + 2) == 4)

    r = row_of(tid)["row"]
    want = [3, 0, 4, 0, 0, 0, 0]
    if case == "value":
        req(f"{S}/edit", "POST", {"row": r, "col": col + 1, "value": 5})  # Tuesday
        want = [3, 5, 4, 0, 0, 0, 0]
    elif case == "rename":
        req(f"{S}/edit", "POST", {"row": r, "col": 1, "value": "Brand new input"})
        check("rename: it comes back under the new name",
              wait(lambda: (board_task(mid, tid) or {}).get("name") == "Brand new input"), board_task(mid, tid))
    elif case == "unstrike":
        req(f"{S}/unstrike", "POST", {"row": r})
    else:
        req(f"{S}/unstrike", "POST", {"row": r, "noTrigger": True})
        req(f"{S}/tick")  # the every-minute check notices it
    check(f"{case}: the input is back on the board", wait(lambda: board_task(mid, tid) is not None))
    check(f"{case}: with this week's numbers", wait(lambda: values(mid, tid) == want), values(mid, tid))
    check(f"{case}: with this week's KPI", wait(lambda: kpi(mid, tid) == 9), kpi(mid, tid))
    check(f"{case}: with last week's numbers", wait(lambda: values(mid, tid, PW)[:2] == [1, 2]), values(mid, tid, PW))
    after = row_of(tid)
    check(f"{case}: the row is no longer struck through or tagged removed",
          not after["strike"] and "itb_archived=1" not in after["md"], after)
    time.sleep(12)
    check(f"{case}: still on the board with its numbers after a catch-up",
          board_task(mid, tid) is not None and values(mid, tid) == want, values(mid, tid))

names_s = sorted(m["name"] for m in req(f"{S}/exec?secret=s3cret")["members"])
names_b = sorted(m["name"] for m in state()["members"])
check("one of each person on both sides", names_s == names_b and len(names_b) == len(set(names_b)), (names_s, names_b))
st = req(f"{B}/api/sync/status")
check("no sync error, nothing waiting", not st["lastError"] and st["outbox"] == 0, st)
print("FAILURES:", fails or "none")

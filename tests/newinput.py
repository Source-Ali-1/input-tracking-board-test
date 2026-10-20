"""A new input gets its Weekly KPI, Week total and Win / Loss in every week, both ways.

Sheet -> board: a row added in the sheet has a Week total in every week (even with no KPI yet); a KPI
typed in one week fills the input's empty KPIs in the later weeks, on the sheet and on the board.
Board -> sheet: an input added on the board gets its KPI and formulas in this and the later weeks,
its logged days reach the sheet, and a KPI set on the board for one week carries forward too.
KPIs already in a week are never overwritten, and earlier weeks are left alone.
"""
import json, os, time, urllib.request
B, S = "http://localhost:" + os.getenv("BOARD_PORT", "8099"), "http://localhost:" + os.getenv("MOCK_PORT", "8090")
TOTAL = '=SUM(RC[-7]:RC[-1])+COUNTIF(RC[-7]:RC[-1],TRUE)'
WL = '=IF(RC[-9]="","",IF(RC[-1]>=RC[-9],"Win",TEXT(IFERROR(RC[-1]/RC[-9],0),"0%")))'
W7, W8, W9, W10 = "2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02"
COL = {W7: 64, W8: 74, W9: 84, W10: 94}  # each week's KPI (Target) column
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


def wait(cond, secs=25):
    for _ in range(secs * 2):
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def row(name):
    return next(r for r in req(f"{S}/dump")["rows"] if r["a"] == name)


def kpi(name, week):
    v = row(name)["cells"][COL[week] - 1]
    return None if v in ("", None) else v


def orlando(week=W8):
    st = req(f"{B}/api/state?week={week}")
    m = next(x for x in st["members"] if x["name"] == "ORLANDO")
    return m, st["logs"].get(m["id"])


def btask(name, week):
    """The input as the board shows it that week: the week's log if it has one, else the person's setup."""
    m, lg = orlando(week)
    src = lg["tasks"] if lg else m["tasks"]
    t = next(t for t in src if t["name"] == name)
    return t, ((lg or {}).get("values") or {}).get(t["id"])


def btarget(name, week):
    return btask(name, week)[0]["target"]


def save_inputs(tasks, changed):
    """What the board page does on "Save inputs": the person's setup, then this week's log."""
    m, lg = orlando()
    req(f"{B}/api/members/{m['id']}", "PUT", {"name": m["name"], "cats": m["cats"], "tasks": tasks, "week": W8,
                                              "changedTargets": changed})
    vals = (lg or {}).get("values") or {}
    req(f"{B}/api/logs/{m['id']}/{W8}", "PUT", {"cats": m["cats"], "tasks": tasks, "values": vals,
                                                "changed": {"cells": [], "targets": changed, "structure": True}})


def log_days(name, week, days):
    """The board page logging day values ({day: value}) for one input."""
    m, lg = orlando(week)
    t, vals = btask(name, week)
    base = lg or {"cats": m["cats"], "tasks": m["tasks"], "values": {}}
    arr = list(vals or [0] * 7)
    for d, v in days.items():
        arr[d] = v
    req(f"{B}/api/logs/{m['id']}/{week}", "PUT", {"cats": base["cats"], "tasks": base["tasks"], "values": {t["id"]: arr},
        "changed": {"cells": [[t["id"], d] for d in days], "targets": [], "structure": False}})


def set_board_kpi(name, week, value):
    """The board page changing one input's KPI for one week (Edit inputs while on that week)."""
    m, lg = orlando(week)
    base = lg or {"cats": m["cats"], "tasks": m["tasks"], "values": {}}
    tasks = [dict(t, target=value) if t["name"] == name else t for t in base["tasks"]]
    tid = next(t["id"] for t in tasks if t["name"] == name)
    req(f"{B}/api/logs/{m['id']}/{week}", "PUT", {"cats": base["cats"], "tasks": tasks, "values": base.get("values") or {},
        "changed": {"cells": [], "targets": [tid], "structure": True}})


def formulas_ok(name, weeks=(W7, W8, W9, W10)):
    c = row(name)["cells"]
    return all(c[COL[w] + 7] == TOTAL and c[COL[w] + 8] == WL for w in weeks)


req(f"{S}/week?to={W10}")  # two weeks ahead already exist, as when the board looked ahead
check("weeks 9 and 10 exist in the sheet", wait(lambda: str(req(f"{S}/dump")["rows"][2]["cells"][COL[W10]]).startswith("Mon")))

# ---------- sheet -> board ----------
yt = row("YouTube video")["row"]
req(f"{S}/insertRow", "POST", {"after": yt})
req(f"{S}/edit", "POST", {"row": yt + 1, "col": 1, "values": [["Threads posts", "Reach", "count"]]})
check("sheet: a new row has the Week total and Win / Loss formulas in every week", wait(lambda: formulas_ok("Threads posts")),
      row("Threads posts")["cells"][COL[W8] + 7:COL[W8] + 9])
check("sheet: the Week total doesn't wait for a KPI (only Win / Loss does)", "RC[-8]" not in TOTAL and row("Threads posts")["cells"][COL[W8] + 7] == TOTAL)
check("board: the new input shows this week, no KPI yet", wait(lambda: btarget("Threads posts", W8) == 0))

r = row("Threads posts")["row"]
req(f"{S}/edit", "POST", {"row": r, "col": COL[W8], "value": 5})
check("sheet: KPI 5 typed in week 8 fills weeks 9 and 10", wait(lambda: kpi("Threads posts", W9) == 5 and kpi("Threads posts", W10) == 5),
      [kpi("Threads posts", w) for w in (W7, W8, W9, W10)])
check("sheet: the week before stays empty", kpi("Threads posts", W7) is None)
check("board: KPI 5 in weeks 8, 9 and 10", wait(lambda: [btarget("Threads posts", w) for w in (W8, W9, W10)] == [5, 5, 5]),
      [btarget("Threads posts", w) for w in (W8, W9, W10)])

req(f"{S}/edit", "POST", {"row": r, "col": COL[W10], "value": 7})
req(f"{S}/edit", "POST", {"row": r, "col": COL[W9], "value": 6})
check("sheet: a KPI changed later in a week doesn't overwrite later weeks' KPIs",
      wait(lambda: kpi("Threads posts", W9) == 6) and kpi("Threads posts", W10) == 7 and kpi("Threads posts", W8) == 5)
check("board: weeks 9 and 10 show 6 and 7", wait(lambda: [btarget("Threads posts", w) for w in (W8, W9, W10)] == [5, 6, 7]),
      [btarget("Threads posts", w) for w in (W8, W9, W10)])

req(f"{S}/edit", "POST", {"row": r, "col": COL[W8] + 1, "values": [[2, 0, 3]]})
check("board: days typed in the sheet arrive (Mon 2, Wed 3)", wait(lambda: (btask("Threads posts", W8)[1] or [])[:3] == [2, 0, 3]),
      btask("Threads posts", W8)[1])

# ---------- board -> sheet ----------
m, _ = orlando()
content = next(c for c in m["cats"] if c["name"] == "Content")
save_inputs(m["tasks"] + [{"id": "boardpins1", "catId": content["id"], "name": "Pins", "goal": "Reach", "target": 3, "unit": "count", "type": "count"}],
            ["boardpins1"])
check("sheet: an input added on the board gets KPI 3 in weeks 8, 9 and 10",
      wait(lambda: [kpi("Pins", w) for w in (W8, W9, W10)] == [3, 3, 3]), [kpi("Pins", w) for w in (W7, W8, W9, W10)])
check("sheet: ...and not in the week before", kpi("Pins", W7) is None)
check("sheet: ...with the Week total and Win / Loss formulas in every week", formulas_ok("Pins"))
check("board: Pins shows KPI 3 this week and in the weeks the board already has (9, 10)",
      wait(lambda: [btarget("Pins", w) for w in (W8, W9, W10)] == [3, 3, 3]))

log_days("Pins", W8, {0: 1, 2: 2})
pins = lambda: row("Pins")["cells"][COL[W8]:COL[W8] + 7]
check("sheet: days logged on the board arrive (Mon 1, Wed 2)", wait(lambda: pins()[0] == 1 and pins()[2] == 2), pins())

set_board_kpi("Pins", W9, 4)
check("sheet: a KPI changed on the board for week 9 only changes week 9",
      wait(lambda: kpi("Pins", W9) == 4) and kpi("Pins", W8) == 3 and kpi("Pins", W10) == 3, [kpi("Pins", w) for w in (W8, W9, W10)])

m, _ = orlando()
save_inputs(m["tasks"] + [{"id": "boardstor1", "catId": content["id"], "name": "Stories", "target": 2, "unit": "check", "type": "check"}],
            ["boardstor1"])
check("sheet: a second input added on the board (tick box) gets KPI 2 in weeks 8, 9 and 10",
      wait(lambda: [kpi("Stories", w) for w in (W8, W9, W10)] == [2, 2, 2]) and formulas_ok("Stories"),
      [kpi("Stories", w) for w in (W7, W8, W9, W10)])
check("sheet: adding it left Pins' KPIs alone", [kpi("Pins", w) for w in (W8, W9, W10)] == [3, 4, 3])

# A sheet input with no KPI anywhere, given one on the board for this week: the sheet carries it on.
r = row("Threads posts")["row"]
req(f"{S}/insertRow", "POST", {"after": r})
req(f"{S}/edit", "POST", {"row": r + 1, "col": 1, "values": [["Shorts", "", "count"]]})
check("board: a sheet input without a KPI arrives", wait(lambda: btarget("Shorts", W8) == 0))
set_board_kpi("Shorts", W8, 4)
check("sheet: a KPI set on the board this week fills the empty later weeks",
      wait(lambda: [kpi("Shorts", w) for w in (W8, W9, W10)] == [4, 4, 4]) and kpi("Shorts", W7) is None,
      [kpi("Shorts", w) for w in (W7, W8, W9, W10)])

# ---------- after a catch-up, both sides still agree ----------
req(f"{S}/tick")
time.sleep(14)  # the board's catch-up pull runs every 10 s here
want = {"Threads posts": [5, 6, 7], "Pins": [3, 4, 3], "Stories": [2, 2, 2], "Shorts": [4, 4, 4]}
got_sheet = {n: [kpi(n, w) for w in (W8, W9, W10)] for n in want}
got_board = {n: [btarget(n, w) for w in (W8, W9, W10)] for n in want}
check("after a catch-up: the sheet's KPIs are unchanged", got_sheet == want, got_sheet)
check("after a catch-up: the board shows the same KPIs", got_board == want, got_board)
names = [r["a"] for r in req(f"{S}/dump")["rows"]]
check("no input row was added twice", all(names.count(n) == 1 for n in want), {n: names.count(n) for n in want})
print("FAILURES:", fails or "none")

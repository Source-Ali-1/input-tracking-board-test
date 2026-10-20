"""Edge cases, both directions, each re-checked after a catch-up cycle (board SYNC_INTERVAL=10 in the rig)."""
import json, time, urllib.request
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


def settle():
    time.sleep(13)  # one catch-up cycle


def bm(name, week=WK):
    s = req(f"{B}/api/state?week={week}")
    m = next(x for x in s["members"] if x["name"] == name)
    return m, s["logs"].get(m["id"])


def rows():
    return req(f"{S}/dump")["rows"]


def row(name, d=None):
    return next(r for r in (d or rows()) if r["a"] == name)


def edit(r, c, vals):
    req(f"{S}/edit", "POST", {"row": r, "col": c, "values": vals})


def put_setup(m, **kw):
    req(f"{B}/api/members/{m['id']}", "PUT", {"name": kw.get("name", m["name"]), "cats": kw.get("cats", m["cats"]), "tasks": kw.get("tasks", m["tasks"])})


# E1 board deletes a whole section -> sheet; must not come back
m, _ = bm("ORLANDO")
att = next(c for c in m["cats"] if c["name"] == "Attention activity")
put_setup(m, cats=[c for c in m["cats"] if c["id"] != att["id"]], tasks=[t for t in m["tasks"] if t["catId"] != att["id"]])
check("E1a board delete section -> its input struck in sheet", wait(lambda: [r for r in rows() if r["a"] == "Attention activity"][-1]["strike"]))
edit(row("AI Research")["row"], 2, [["The business runs without me!"]])   # any sheet edit in the block re-sends the setup
settle()
m, _ = bm("ORLANDO")
check("E1b deleted section stays deleted on the board after sheet edits + catch-up",
      not any(c["name"] == "Attention activity" for c in m["cats"]), [c["name"] for c in m["cats"]])
check("E1c the sheet edit itself arrived (goal)", next(t for t in m["tasks"] if t["name"] == "AI Research")["goal"] == "The business runs without me!")

# E2 board moves an input to another section -> sheet row moves under it
m, _ = bm("ORLANDO")
habits = next(c for c in m["cats"] if c["name"] == "Habits")
tasks = [dict(t, catId=habits["id"]) if t["name"] == "YouTube video" else t for t in m["tasks"]]
put_setup(m, tasks=tasks)
def moved():
    names = [r["a"] for r in rows()]
    return names.index("YouTube video") > names.index("Habits")
check("E2a board move input to other section -> sheet row moved", wait(moved))
settle()
m, _ = bm("ORLANDO")
check("E2b still in the new section on the board after catch-up",
      next(t for t in m["tasks"] if t["name"] == "YouTube video")["catId"] == habits["id"])

# E3 sheet moves an input row to another section (drag) -> board
rs = rows()
src = row("IG Stories", rs)["row"]
dest = row("Habits", rs)["row"] + 1
req(f"{S}/moveRow", "POST", {"row": src, "dest": dest + 1})
check("E3a sheet move row to other section -> board", wait(lambda: (lambda m: next(t for t in m["tasks"] if t["name"] == "IG Stories")["catId"] == next(c for c in m["cats"] if c["name"] == "Habits")["id"])(bm("ORLANDO")[0])))
settle()

# E4 sheet deletes an input row -> board drops it; stays dropped
req(f"{S}/deleteRow", "POST", {"row": row("Personal brand research")["row"]})
check("E4a sheet delete row -> board drops input", wait(lambda: not any(t["name"] == "Personal brand research" for t in bm("ORLANDO")[0]["tasks"])))
settle()
check("E4b stays dropped after catch-up", not any(t["name"] == "Personal brand research" for t in bm("ORLANDO")[0]["tasks"]))

# E5 multi-cell paste across two people + clearing cells
rs = rows()
rn = row("RedNote posts", rs)["row"]
wl = row("Workout", rs)["row"]
edit(rn, W8 + 1, [[3, 2, 1]])
check("E5a pasted 3 cells -> board", wait(lambda: (lambda m, lg: lg["values"][next(t["id"] for t in m["tasks"] if t["name"] == "RedNote posts")][:3] == [3, 2, 1])(*bm("ORLANDO"))))
edit(rn, W8 + 1, [["", "", ""]])
check("E5b cleared cells -> board zeros", wait(lambda: (lambda m, lg: lg["values"][next(t["id"] for t in m["tasks"] if t["name"] == "RedNote posts")][:3] == [0, 0, 0])(*bm("ORLANDO"))))

# E6 tick marks typed as text in the sheet ("x", "✓", TRUE) count as 1 on the board
edit(wl, W8 + 1, [["x", "✓", "yes"]])
check("E6a 'x' / '✓' / 'yes' in sheet -> 1 on board", wait(lambda: (lambda m, lg: lg["values"][next(t["id"] for t in m["tasks"] if t["name"] == "Workout")][:3] == [1, 1, 1])(*bm("ORLANDO"))))
settle()
check("E6b sheet keeps the user's marks (not overwritten with 1)", row("Workout")["cells"][W8:W8 + 3] == ["x", "✓", "yes"], row("Workout")["cells"][W8:W8 + 3])

# E7 member rename both ways
m, _ = bm("BONCAP")
put_setup(m, name="BONCAP Co")
check("E7a board rename person -> sheet", wait(lambda: any(r["a"] == "BONCAP Co" for r in rows())))
edit(row("BONCAP Co")["row"], 1, [["BONCAP"]])
check("E7b sheet rename person -> board", wait(lambda: any(x["name"] == "BONCAP" for x in req(f"{B}/api/state?week={WK}")["members"])))

# E8 long names: the board's length limits must not cut the sheet's text
long_goal = "A very long goal description that keeps going " * 4
edit(row("Mandarin 30 min")["row"], 2, [[long_goal.strip()]])
check("E8a long goal arrives on board", wait(lambda: bm("WILL")[0]["tasks"] and any(t["name"] == "Mandarin 30 min" and t["goal"] for t in bm("WILL")[0]["tasks"])))
m, _ = bm("WILL")
put_setup(m)  # an unrelated board save of WILL's inputs
time.sleep(6)
check("E8b board save doesn't truncate the sheet's long goal", row("Mandarin 30 min")["cells"][1] == long_goal.strip(), len(str(row("Mandarin 30 min")["cells"][1])))

# E9 past week edit on the board (week 1) -> sheet; and sheet past-week KPI -> board week log
m, lg = bm("ORLANDO", "2026-08-31")
t = next(x for x in (lg or m)["tasks"] if x["name"] == "AI Research")
base = lg or {"cats": m["cats"], "tasks": m["tasks"], "values": {}}
vals = dict(base["values"]); vals[t["id"]] = [2, 0, 0, 0, 0, 0, 0]
req(f"{B}/api/logs/{m['id']}/2026-08-31", "PUT", {"cats": base["cats"], "tasks": base["tasks"], "values": vals})
check("E9a board past-week value -> sheet week 1", wait(lambda: row("AI Research")["cells"][4] == 2))
edit(row("AI Research")["row"], 4, [[9]])
check("E9b sheet past-week KPI -> board week-1 log only", wait(lambda: next(x for x in bm("ORLANDO", "2026-08-31")[1]["tasks"] if x["id"] == t["id"])["target"] == 9)
      and next(x for x in bm("ORLANDO")[0]["tasks"] if x["id"] == t["id"])["target"] != 9)

# E10 everything converged
settle()
st = req(f"{B}/api/sync/status")
check("E10 converged: nothing queued, no errors", st["outbox"] == 0 and st["lastError"] is None, st)
print("FAILURES:", fails or "none")

# ---------------- more cases (reviewer findings) ----------------
fails2 = []
# R2 board adds an input while the sheet is unreachable; meanwhile the sheet edits that person -> input survives both sides
req(f"{S}/down?v=1")
m, _ = bm("WILL")
habits = next(c for c in m["cats"] if c["name"] == "Habits")
put_setup(m, tasks=m["tasks"] + [{"id": "pendingt01", "catId": habits["id"], "name": "Journal", "target": 7, "unit": "count", "type": "count"}])
time.sleep(2)
edit(row("Reading")["row"], 2, [["Any book (edited in sheet)"]])        # sheet re-sends WILL's setup (without Journal)
time.sleep(3)
check("R2a board keeps its new input while the sheet's older list arrives", any(t["name"] == "Journal" for t in bm("WILL")[0]["tasks"]))
req(f"{S}/down?v=0")
check("R2b ...and it reaches the sheet once it's back", wait(lambda: any(r["a"] == "Journal" for r in rows()), 60))
check("R2c the sheet's goal edit is kept too", next(t for t in bm("WILL")[0]["tasks"] if t["name"] == "Reading")["goal"] == "Any book (edited in sheet)")
# R3 KPI saved while viewing next week only changes next week
req(f"{S}/week?to=2026-10-26")
m, _ = bm("ORLANDO")
tasks = [dict(t, target=11) if t["name"] == "Copywriting and scripting" else t for t in m["tasks"]]
req(f"{B}/api/members/{m['id']}", "PUT", {"name": m["name"], "cats": m["cats"], "tasks": tasks, "week": "2026-10-26"})
cs = row("Copywriting and scripting")["row"]
check("R3a next week's KPI updated in sheet", wait(lambda: req(f"{S}/cell?r={cs}&c={W8 + 10}") == 11))
check("R3b this week's KPI untouched in sheet", req(f"{S}/cell?r={cs}&c={W8}") == 6, req(f"{S}/cell?r={cs}&c={W8}"))
# R4 the board sets a KPI for a week the sheet doesn't have yet; creating that week keeps it
m, lg = bm("ORLANDO", "2026-11-02")
base = {"cats": m["cats"], "tasks": [dict(t, target=13) if t["name"] == "AI Research" else t for t in m["tasks"]], "values": {}}
req(f"{B}/api/logs/{m['id']}/2026-11-02", "PUT", base)
req(f"{S}/week?to=2026-11-02")
ai = row("AI Research")["row"]
settle()
check("R4 a new week doesn't overwrite the KPI the board set for it", req(f"{S}/cell?r={ai}&c={W8 + 20}") == 13
      and next(t for t in bm("ORLANDO", "2026-11-02")[1]["tasks"] if t["name"] == "AI Research")["target"] == 13, req(f"{S}/cell?r={ai}&c={W8 + 20}"))
# R4b KPI changed in "Edit inputs" while viewing a week the sheet doesn't have yet -> applied when created
m, _ = bm("ORLANDO")
tasks = [dict(t, target=21) if t["name"] == "Mino's Content Academy" else t for t in m["tasks"]]
req(f"{B}/api/members/{m['id']}", "PUT", {"name": m["name"], "cats": m["cats"], "tasks": tasks, "week": "2026-11-09"})
time.sleep(5)
req(f"{S}/week?to=2026-11-09")
mino = row("Mino's Content Academy")["row"]
check("R4b editor KPI for a future week lands when the week is created", req(f"{S}/cell?r={mino}&c={W8 + 30}") == 21
      and req(f"{S}/cell?r={mino}&c={W8 + 20}") != 21, (req(f"{S}/cell?r={mino}&c={W8 + 20}"), req(f"{S}/cell?r={mino}&c={W8 + 30}")))
# R7 month row typed short ("Oct 2026") doesn't create a fake person
names_before = sorted(x["name"] for x in req(f"{B}/api/state?week={WK}")["members"])
edit(row("September 2026")["row"], 1, [["Oct 2026"]])
settle()
names_after = sorted(x["name"] for x in req(f"{B}/api/state?week={WK}")["members"])
check("R7 short month row doesn't create a new person", names_before == names_after, (names_before, names_after))
st = req(f"{B}/api/sync/status")
check("R-final converged", st["outbox"] == 0 and st["lastError"] is None, st)
print("ALL FAILURES:", fails or "none")

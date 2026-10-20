import json, time, urllib.request, subprocess, sys
B, S = "http://localhost:" + __import__("os").getenv("BOARD_PORT", "8099"), "http://localhost:" + __import__("os").getenv("MOCK_PORT", "8090")
RESTART = sys.argv[1]
fails = []


def req(url, method="GET", body=None, headers=None):
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(r, timeout=30) as res:
        return json.loads(res.read() or b"{}")


def state(wk): return req(f"{B}/api/state?week={wk}")
def dump(): return req(f"{S}/dump")


def member(name, wk="2026-10-19"):
    s = state(wk)
    m = next(m for m in s["members"] if m["name"].startswith(name))
    return m, s["logs"].get(m["id"])


def row_of(name, d=None):
    d = d or dump()
    return next(r for r in d["rows"] if r["a"] == name)


def wait(cond, secs=25):
    for _ in range(secs * 4):
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label, flush=True)
    if not ok:
        fails.append(label)


W8 = 4 + 10 * 7   # week 8 target column (D=4 + 7 blocks)
def col(day): return W8 + 1 + day


# A. board edit -> sheet
m, lg = member("ORLANDO")
ai = next(t for t in m["tasks"] if t["name"] == "AI Research")
base = lg or {"cats": m["cats"], "tasks": m["tasks"], "values": {}}
vals = dict(base.get("values") or {})
vals[ai["id"]] = [0, 2, 0, 0, 0, 0, 0]
req(f"{B}/api/logs/{m['id']}/2026-10-19", "PUT", {"cats": base["cats"], "tasks": base["tasks"], "values": vals})
r = row_of("AI Research")["row"]
check("A board cell -> sheet", wait(lambda: req(f"{S}/cell?r={r}&c={col(1)}") == 2))

# B. sheet edit -> board
req(f"{S}/edit", "POST", {"row": r, "col": col(2), "value": 1.5})
m, lg = member("ORLANDO")
check("B sheet cell -> board", lg["values"][ai["id"]] == [0, 2, 1.5, 0, 0, 0, 0])
check("B no echo (outbox empty)", wait(lambda: req(f"{B}/api/sync/status")["outbox"] == 0))

# B2. sheet target edit -> board (current week also updates the member default)
req(f"{S}/edit", "POST", {"row": r, "col": W8, "value": 5})
m, lg = member("ORLANDO")
check("B2 sheet target -> board log + setup",
      next(t for t in lg["tasks"] if t["id"] == ai["id"])["target"] == 5
      and next(t for t in m["tasks"] if t["id"] == ai["id"])["target"] == 5)

# C. board setup: rename + new input in Content + remove LinkedIn
m, lg = member("ORLANDO")
content = next(c for c in m["cats"] if c["name"] == "Content")
tasks = [t for t in m["tasks"] if t["name"] != "LinkedIn posts"] + [
    {"id": "newtask01", "catId": content["id"], "name": "Threads posts", "target": 10, "unit": "count", "type": "count"}]
req(f"{B}/api/members/{m['id']}", "PUT", {"name": "ORLANDO R", "cats": m["cats"], "tasks": tasks})


def setup_ok():
    d = dump()
    return any(x["a"] == "Threads posts" for x in d["rows"]) and any(x["a"] == "ORLANDO R" for x in d["rows"])


check("C rename + new row in sheet", wait(setup_ok))
d = dump()
names = [x["a"] for x in d["rows"]]
check("C new row sits at end of Content", names.index("Threads posts") == names.index("YouTube video") + 1)
check("C removed input struck through", row_of("LinkedIn posts", d)["strike"])
check("C removed input's current-week days cleared, past weeks kept",
      all(str(c).strip() in ("", "0") for c in row_of("LinkedIn posts", d)["cells"][W8:W8 + 7]) and row_of("LinkedIn posts", d)["cells"][13] == 7)
tr = row_of("Threads posts", d); print("   Threads row:", tr["row"], tr["md"], "KPI", tr["cells"][W8 - 1])
check("C new row target in week 8", tr["cells"][W8 - 1] == 10)
check("C new row tagged with board id", "itb_task=newtask01" in tr["md"])
time.sleep(12)
m, lg = member("ORLANDO R")
check("C reconcile keeps board setup (LinkedIn stays removed)", not any(t["name"] == "LinkedIn posts" for t in m["tasks"]))

# D. LWW: an older sheet change must not overwrite a newer board value
req(f"{B}/api/sync/inbound", "POST", {"currentWeek": "2026-10-19", "changes": [
    {"kind": "cell", "member": m["id"], "week": "2026-10-19", "task": ai["id"], "day": 1, "value": 9, "ts": 1000}]},
    {"X-Sync-Secret": "s3cret"})
m, lg = member("ORLANDO R")
check("D stale change ignored", lg["values"][ai["id"]][1] == 2)

# E. sheet edits while the board is down, then backlog retry
subprocess.run(["bash", RESTART, "stopboard"], capture_output=True)
time.sleep(1)
req(f"{S}/edit", "POST", {"row": r, "col": col(3), "value": 4})
check("E backlog recorded", "BACKLOG_SINCE" in dump()["props"])
subprocess.run(["bash", RESTART, "keep", "boardonly"])
req(f"{S}/tick", "POST", {})
m, lg = member("ORLANDO R")
check("E backlog delivered after restart", lg["values"][ai["id"]][3] == 4 and "BACKLOG_SINCE" not in dump()["props"])

# F. sheet unreachable: board edit waits in the outbox, delivered when back
req(f"{S}/down?v=1")
vals = lg["values"]
vals[ai["id"]][4] = 3
req(f"{B}/api/logs/{m['id']}/2026-10-19", "PUT", {"cats": lg["cats"], "tasks": lg["tasks"], "values": vals})
time.sleep(3)
check("F change queued while sheet down", req(f"{B}/api/sync/status")["outbox"] >= 1)
req(f"{S}/down?v=0")
check("F delivered when sheet back", wait(lambda: req(f"{S}/cell?r={r}&c={col(4)}") == 3, 40))

# G. new member on the board -> new block in the sheet; then delete it
nina = {"cats": [{"id": "cat0001", "name": "Habits"}],
        "tasks": [{"id": "task0001", "catId": "cat0001", "name": "Walk", "target": 7, "unit": "count", "type": "check"}]}
req(f"{B}/api/members/newmember01", "PUT", dict(nina, name="NINA"))
check("G new block created", wait(lambda: any(x["a"] == "Walk" for x in dump()["rows"])))
names = [x["a"] for x in dump()["rows"]]
i = names.index("NINA")
check("G block layout name/month/header/section/input", names[i:i + 5] == ["NINA", "October 2026", "Input", "Habits", "Walk"])
req(f"{B}/api/logs/newmember01/2026-10-19", "PUT", dict(nina, values={"task0001": [1, 0, 0, 0, 0, 0, 0]}))
check("G new member's cell in sheet", wait(lambda: row_of("Walk")["cells"][col(0) - 1] == 1))
urllib.request.urlopen(urllib.request.Request(f"{B}/api/members/newmember01", method="DELETE"))
check("G deleted member removed from sheet", wait(lambda: not any(x["a"] in ("NINA", "Walk") for x in dump()["rows"])))
time.sleep(12)
check("G deleted member not resurrected by reconcile", not any(x["name"] == "NINA" for x in state("2026-10-19")["members"]))

# H. new week is idempotent
hdr_cells = lambda: next(x for x in dump()["rows"] if x["a"] == "Input")["cells"]
before = hdr_cells()
req(f"{S}/week", "POST", {})
req(f"{S}/week", "POST", {})
check("H ensureCurrentWeek idempotent", hdr_cells() == before and len([c for c in before if str(c).startswith("Mon ") or (str(c)[:4] == "2026" and "T" in str(c))]) >= 8)
d = dump()
hdr = next(x for x in d["rows"] if x["a"] == "Input")
check("H week 8 header labels", hdr["cells"][col(0) - 1] == "Mon 10/19" and hdr["cells"][col(6) - 1] == "Sun 10/25")
check("H week 8 targets carried over", row_of("Workout", d)["cells"][W8 - 1] == 7)

# I. user inserts a row and types a new input in the sheet
yt = row_of("Threads posts")["row"]
req(f"{S}/insertRow", "POST", {"after": yt})
req(f"{S}/edit", "POST", {"row": yt + 1, "col": 1, "values": [["Pinterest pins", "Reach", "count"]]})
m, lg = member("ORLANDO R")
check("I typed row becomes a board input", any(t["name"] == "Pinterest pins" for t in m["tasks"])
      and any(t["name"] == "Pinterest pins" for t in lg["tasks"]))

# J. everything settles
time.sleep(12)
st = req(f"{B}/api/sync/status")
check("J converged (outbox empty)", st["outbox"] == 0)
print(st)
print("FAILURES:", fails or "none")

"""Section headings vs inputs: a row needs a unit, a goal or a KPI to be an input (one the board counts).
Headings with an old "input" tag (Content, Habits) or a stray logged number (Finance) become sections;
"Reading (30 Mins)" (KPI, no unit) and "Attention activity" (unit + goal) stay inputs; a bare input the
board adds stays an input; values keep syncing both ways around the changed rows."""
import json, time, urllib.request
B, S = "http://localhost:" + __import__("os").getenv("BOARD_PORT", "8099"), "http://localhost:" + __import__("os").getenv("MOCK_PORT", "8090")
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


def wait(cond, secs=30):
    for _ in range(secs * 2):
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def member(name):
    return next(m for m in req(f"{B}/api/state?week={CW}")["members"] if m["name"] == name)


def names(name):
    m = member(name)
    return [t["name"] for t in m["tasks"]], [c["name"] for c in m["cats"]]


def rows():
    return req(f"{S}/dump")["rows"]


def row_of(name, nth=0):
    return [r for r in rows() if r["a"] == name][nth]


# The real sheet's heading rows carry an old "input" tag from earlier versions: recreate that.
for nm, nth in (("Content", 0), ("Habits", 0), ("Finance", 0)):
    r = row_of(nm, nth)
    req(f"{S}/setMd", "POST", {"row": r["row"], "key": "itb_task", "value": "tstale%s%d" % (nm.lower(), nth)})
fin = row_of("Finance")
req(f"{S}/edit", "POST", {"row": fin["row"], "col": 4 + 50 + 7, "value": 1, "noTrigger": True})  # stray 1 logged, no KPI
req(f"{S}/tick")

tasks, cats = [], []
ok = wait(lambda: (lambda t, c: not {"Content", "Habits", "Finance"} & set(t) and {"Content", "Habits", "Finance"} <= set(c))(*names("ORLANDO")))
tasks, cats = names("ORLANDO")
check("tagged headings without unit/goal/KPI -> sections on the board (Content, Habits, Finance)", ok, (tasks, cats))
check("KPI but no unit stays an input (Reading (30 Mins))", "Reading (30 Mins)" in tasks, tasks)
check("unit + goal stays an input (Attention activity)", "Attention activity" in tasks and "Attention activity" in cats, (tasks, cats))
check("old input tags removed from the heading rows in the sheet",
      not any(m.startswith("itb_task=") for nm in ("Content", "Habits", "Finance") for m in row_of(nm)["md"]))
m = member("ORLANDO")
ig = next(t for t in m["tasks"] if t["name"] == "IG Reels posted")
check("inputs under 'Content' now belong to the Content section",
      next(c["name"] for c in m["cats"] if c["id"] == ig["catId"]) == "Content")

# A bare input from the board (no unit, no goal, KPI 5) must stay an input
tasks2 = m["tasks"] + [{"id": "tbareinput1", "catId": ig["catId"], "name": "Bare input", "target": 5, "unit": "", "goal": ""}]
req(f"{B}/api/members/{m['id']}", "PUT", {"name": m["name"], "cats": m["cats"], "tasks": tasks2, "week": CW})
check("board bare input -> sheet row", wait(lambda: any(r["a"] == "Bare input" for r in rows())))
req(f"{S}/tick")
time.sleep(12)
check("board bare input stays an input after catch-up (board and sheet)",
      "Bare input" in names("ORLANDO")[0] and any(m.startswith("itb_task=") for m in row_of("Bare input")["md"]))

# Values still sync both ways next to the changed rows
hdr = next(r for r in rows() if r["a"] == "Input")["cells"]
cw_t = hdr.index("Mon 10/19")  # 0-based Mon index == 1-based KPI column
igr = row_of("IG Reels posted")["row"]
req(f"{S}/edit", "POST", {"row": igr, "col": cw_t + 2, "value": 3})
check("sheet value under a converted heading -> board",
      wait(lambda: req(f"{B}/api/state?week={CW}")["logs"][m["id"]]["values"][ig["id"]][1] == 3))
s = req(f"{B}/api/state?week={CW}")
lg = s["logs"][m["id"]]
vals = dict(lg["values"]); vals["tbareinput1"] = [0, 0, 2, 0, 0, 0, 0]
req(f"{B}/api/logs/{m['id']}/{CW}", "PUT", {"cats": lg["cats"], "tasks": lg["tasks"], "values": vals})
check("board value in the bare input -> sheet", wait(lambda: req(f"{S}/cell?r={row_of('Bare input')['row']}&c={cw_t + 3}") == 2))
time.sleep(12)
tasks, cats = names("ORLANDO")
check("after another catch-up nothing flipped back",
      not {"Content", "Habits", "Finance"} & set(tasks) and "Bare input" in tasks and "Reading (30 Mins)" in tasks, (tasks, cats))
print("FAILURES:", fails or "none")

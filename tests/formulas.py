"""Week total / Win-Loss formulas: header-row array formulas (like the real tracker) get replaced by one
relative formula per input row, for existing people, a person added on the board, a person added by hand
in the sheet, a new input, and a new week. Also: a new person's KPI is only written from this week on."""
import json, re, time, urllib.request
B, S = "http://localhost:8099", "http://localhost:8090"
TOTAL = '=IF(RC[-8]="","",SUM(RC[-7]:RC[-1])+COUNTIF(RC[-7]:RC[-1],TRUE))'
WL = '=IF(RC[-9]="","",IF(RC[-1]>=RC[-9],"Win",TEXT(IFERROR(RC[-1]/RC[-9],0),"0%")))'
ARR_T = '={"Week total";ARRAYFORMULA(IF($BL$29:$BL$32="","",MMULT(N($BM$29:$BS$32),SEQUENCE(7,1,1,0))))}'
ARR_W = '={"Win / Loss";ARRAYFORMULA($BT$29:$BT$32)}'
CW = "2026-10-19"
fails = []


def req(url, method="GET", body=None):
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as res:
        return json.loads(res.read() or b"{}")


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ("  -- " + detail if detail and not ok else ""), flush=True)
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


def layout():
    rows = req(f"{S}/dump")["rows"]
    hdr = next(r for r in rows if r["a"] == "Input")["cells"]
    targets = [i for i, v in enumerate(hdr) if re.match(r"^Mon \d", str(v))]  # 0-based Mon index == 1-based target col
    return rows, targets


def cells(r):
    return list(r["cells"]) + [""] * 400


def problems(rows, targets, only=None):
    """Input rows missing the formulas, and header cells still holding a formula."""
    bad = []
    for r in rows:
        if only is not None and r["row"] not in only:
            continue
        c = cells(r)
        if any(m.startswith("itb_task=") for m in r["md"]):
            for t in targets:
                if c[t + 7] != TOTAL or c[t + 8] != WL:
                    bad.append("row %d col %d: %r / %r" % (r["row"], t + 8, c[t + 7], c[t + 8]))
        if r["a"] == "Input":
            for t in targets:
                if str(c[t + 7]).startswith("=") or str(c[t + 8]).startswith("="):
                    bad.append("header row %d col %d still a formula" % (r["row"], t + 8))
    return bad


def block_rows(rows, name):
    start = next(r["row"] for r in rows if r["a"] == name)
    after = [r["row"] for r in rows if r["row"] > start and any(m.startswith("itb_member=") for m in r["md"])]
    end = min(after) - 1 if after else 10 ** 6
    return [r["row"] for r in rows if start <= r["row"] <= end]


def task_rows(rows, name):
    br = block_rows(rows, name)
    return [r for r in rows if r["row"] in br and any(m.startswith("itb_task=") for m in r["md"])]


rows, targets = layout()
bad = problems(rows, targets)
check("existing people: header array formulas replaced, every input row has Total + Win/Loss", not bad, "; ".join(bad[:4]))
hdr = cells(next(r for r in rows if r["a"] == "Input"))
check("header labels read 'Week total' / 'Win / Loss'", hdr[targets[0] + 7] == "Week total" and hdr[targets[0] + 8] == "Win / Loss",
      "%r %r" % (hdr[targets[0] + 7], hdr[targets[0] + 8]))

# 1) new person from the board
mid = "mformtest01"
req(f"{B}/api/members/{mid}", "PUT", {"name": "FORMI", "week": CW, "cats": [{"id": "cformtest01", "name": "Sales"}],
                                       "tasks": [{"id": "tformtest01", "catId": "cformtest01", "name": "Calls", "target": 5, "unit": "calls"},
                                                 {"id": "tformtest02", "catId": "cformtest01", "name": "Emails", "target": 3, "unit": "emails"}]})
ok = wait(lambda: len(task_rows(req(f"{S}/dump")["rows"], "FORMI")) == 2)
check("board new person -> sheet block with both inputs", ok)
rows, targets = layout()
fb = block_rows(rows, "FORMI")
bad = problems(rows, targets, fb)
check("board new person: Total + Win/Loss formulas in every week (no #REF!)", ok and not bad, "; ".join(bad[:4]))
calls = cells(next(r for r in rows if r["row"] in fb and r["a"] == "Calls"))
cw_t = next(t for t in targets if hdr[t] == "Mon 10/19")
past = [calls[t - 1] for t in targets if t < cw_t]
check("new person's KPI written from this week, past weeks left blank",
      calls[cw_t - 1] == 5 and all(v in ("", None) for v in past), "this week %r, past %r" % (calls[cw_t - 1], past))

# 2) new input for an existing person, from the board
s = req(f"{B}/api/state?week={CW}")
m = next(x for x in s["members"] if x["name"] == "ORLANDO")
tasks = m["tasks"] + [{"id": "tformtest03", "catId": m["tasks"][0]["catId"], "name": "Formula probe", "target": 2, "unit": "count"}]
req(f"{B}/api/members/{m['id']}", "PUT", {"name": m["name"], "cats": m["cats"], "tasks": tasks, "week": CW})
check("board new input -> sheet row", wait(lambda: any(r["a"] == "Formula probe" for r in req(f"{S}/dump")["rows"])))
rows, targets = layout()
bad = problems(rows, targets, [r["row"] for r in rows if r["a"] == "Formula probe"])
check("new input row has the formulas in every week", not bad, "; ".join(bad[:4]))

# 3) person added by hand in the sheet, copying the header row with its array formulas
rows, targets = layout()
last = max(r["row"] for r in rows)
for i in range(5):
    req(f"{S}/insertRow", "POST", {"after": last + i})
hdr_copy = cells(next(r for r in rows if r["a"] == "Input"))[:targets[-1] + 9]
for t in targets:
    hdr_copy[t + 7], hdr_copy[t + 8] = ARR_T, ARR_W
lines = [["HANDY"], ["October 2026"], hdr_copy, ["Habits"], ["Walk", "Health", "count"]]
for i, vals in enumerate(lines):
    req(f"{S}/edit", "POST", {"row": last + 1 + i, "col": 1, "values": [vals]})
check("hand-added person reaches the board", wait(lambda: any(x["name"] == "HANDY" for x in req(f"{B}/api/state?week={CW}")["members"])))
rows, targets = layout()
bad = problems(rows, targets, block_rows(rows, "HANDY"))
check("hand-added person: header formulas replaced, input row gets formulas", not bad, "; ".join(bad[:4]))

# 4) a new week appended
n_before = len(targets)
req(f"{S}/week?to=2026-10-26")
rows, targets = layout()
check("new week block added", len(targets) == n_before + 1, "%d -> %d" % (n_before, len(targets)))
bad = problems(rows, targets)
check("new week: every input row of every person has the formulas", not bad, "; ".join(bad[:4]))

# 5) both directions still sync in the new person's row
s = req(f"{B}/api/state?week={CW}")
mm = next(x for x in s["members"] if x["id"] == mid)
lg = s["logs"].get(mid) or {"cats": mm["cats"], "tasks": mm["tasks"], "values": {}}
vals = dict(lg.get("values") or {}); vals["tformtest01"] = [2, 0, 0, 0, 0, 0, 0]
req(f"{B}/api/logs/{mid}/{CW}", "PUT", {"cats": lg["cats"], "tasks": lg["tasks"], "values": vals})
crow = next(r["row"] for r in task_rows(rows, "FORMI") if r["a"] == "Calls")
check("board value in the new person's row -> sheet", wait(lambda: req(f"{S}/cell?r={crow}&c={cw_t + 1}") == 2))
req(f"{S}/edit", "POST", {"row": crow, "col": cw_t + 2, "value": 4})
check("sheet value in the new person's row -> board",
      wait(lambda: req(f"{B}/api/state?week={CW}")["logs"][mid]["values"]["tformtest01"][1] == 4))
rows, targets = layout()
bad = problems(rows, targets)
check("formulas intact after the edits", not bad, "; ".join(bad[:4]))
print("FAILURES:", fails or "none")

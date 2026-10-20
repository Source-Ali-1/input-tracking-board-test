import json, time, urllib.request
B, S = "http://localhost:" + __import__("os").getenv("BOARD_PORT", "8099"), "http://localhost:" + __import__("os").getenv("MOCK_PORT", "8090")
fails = []
W8 = 74


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


def btask(name):
    m = next(x for x in req(f"{B}/api/state?week=2026-10-19")["members"] if x["name"] == "ORLANDO")
    return m, next((t for t in m["tasks"] if t["name"] == name), None)


dv = row("Daily vlog")
check("installing the sync kept existing rows' look; Daily vlog got the standard Total / Win-Loss formulas",
      dv["fmt"] == "" and dv["cells"][11].startswith("=SUM(RC[-7]") and dv["cells"][12].startswith("=IF(RC[-9]"), dv["cells"][:20])

yt = row("YouTube video")["row"]
req(f"{S}/insertRow", "POST", {"after": yt})
req(f"{S}/edit", "POST", {"row": yt + 1, "col": 1, "values": [["Threads posts", "Reach", "count"]]})
r = row("Threads posts")
check("new sheet row: Week total + Win/Loss formulas in every week (incl. week 8)",
      all(str(r["cells"][c + 7]).startswith("=") and str(r["cells"][c + 8]).startswith("=") for c in range(4, 84, 10)),
      [r["cells"][c + 7: c + 9] for c in range(4, 84, 10)])
check("new sheet row: formatting copied from the neighbouring input", r["fmt"] == "task")
check("new sheet row: KPI left empty for the user", r["cells"][W8 - 1] in ("", None))
check("unit 'count' -> number input on the board", wait(lambda: btask("Threads posts")[1]["type"] == "count"))

req(f"{S}/edit", "POST", {"row": r["row"], "col": 3, "value": "check"})
check("sheet unit changed to 'check' -> tick box on the board", wait(lambda: btask("Threads posts")[1]["type"] == "check"))

# vice versa: choose the type on the board, then the sheet re-sends the input (goal edited) -> board keeps its choice
m, t = btask("Threads posts")
tasks = [dict(x, type="count") if x["id"] == t["id"] else x for x in m["tasks"]]
req(f"{B}/api/members/{m['id']}", "PUT", {"name": m["name"], "cats": m["cats"], "tasks": tasks})
time.sleep(4)
req(f"{S}/edit", "POST", {"row": r["row"], "col": 2, "value": "Reach (edited)"})
check("board-chosen type survives a sheet edit that doesn't touch the unit",
      wait(lambda: btask("Threads posts")[1]["goal"] == "Reach (edited)") and btask("Threads posts")[1]["type"] == "count",
      btask("Threads posts")[1])
req(f"{S}/edit", "POST", {"row": r["row"], "col": 3, "value": "done"})
check("changing the unit in the sheet again switches it ('done' -> tick box)", wait(lambda: btask("Threads posts")[1]["type"] == "check"))

# board-added input still gets everything (vice versa of the sheet-added row)
m, _ = btask("Threads posts")
content = next(c for c in m["cats"] if c["name"] == "Content")
req(f"{B}/api/members/{m['id']}", "PUT", {"name": m["name"], "cats": m["cats"],
    "tasks": m["tasks"] + [{"id": "boardrow01", "catId": content["id"], "name": "Pins", "target": 3, "unit": "count", "type": "count"}]})
check("board-added row in the sheet has formulas + format + KPI",
      wait(lambda: (lambda x: x["fmt"] == "task" and str(x["cells"][W8 + 7]).startswith("=") and x["cells"][W8 - 1] == 3)(row("Pins"))))
print("FAILURES:", fails or "none")

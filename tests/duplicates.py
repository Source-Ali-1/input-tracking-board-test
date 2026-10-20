"""No duplicate people: when the sheet stops having a person the board still has (their block now
carries a new id, or it was deleted in the sheet), the board drops its stale copy instead of keeping
it (and re-sending it) forever. A person added on the board still reaches the sheet."""
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


def wait(cond, secs=40):
    for _ in range(secs * 2):
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def board_names():
    return sorted(m["name"] for m in req(f"{B}/api/state?week={CW}")["members"])


def sheet_names():
    return sorted(m["name"] for m in req(f"{S}/exec?secret=s3cret")["members"])


def rows():
    return req(f"{S}/dump")["rows"]


start = board_names()
check("board starts with the sheet's people, once each", start == sheet_names() and len(start) == len(set(start)), (start, sheet_names()))
last = start[-1]  # the bottom person (WILL in the fixture)

# 1) the same block now carries a new id (what made BONCAP / WILL appear twice)
title = next(r for r in rows() if r["a"] == last and any(m.startswith("itb_member=") for m in r["md"]))
old_id = next(m.split("=", 1)[1] for m in title["md"] if m.startswith("itb_member="))
req(f"{S}/setMd", "POST", {"row": title["row"], "key": "itb_member", "value": "mreidentified01"})
req(f"{S}/tick")
ok = wait(lambda: board_names() == start and any(m["id"] == "mreidentified01" for m in req(f"{B}/api/state?week={CW}")["members"]))
names = board_names()
check("re-identified person: board shows them once, under the sheet's new id", ok, names)
check("re-identified person: the old copy is gone from the board",
      not any(m["id"] == old_id for m in req(f"{B}/api/state?week={CW}")["members"]))
time.sleep(12)
st = req(f"{B}/api/sync/status")
check("no endless re-sending afterwards (outbox empty, no error)", st["outbox"] == 0 and not st["lastError"], st)
check("sheet still has one block for them", sheet_names().count(last) == 1, sheet_names())

# 2) a person added on the board still reaches the sheet (not mistaken for a stale copy)
req(f"{B}/api/members/mboardnew0001", "PUT", {"name": "NEWBIE", "week": CW, "cats": [{"id": "cboardnew01", "name": "Work"}],
                                             "tasks": [{"id": "tboardnew01", "catId": "cboardnew01", "name": "Calls", "target": 3, "unit": "calls"}]})
check("board-added person -> sheet block", wait(lambda: "NEWBIE" in sheet_names()))
time.sleep(12)
check("board-added person kept on the board", board_names().count("NEWBIE") == 1, board_names())

# 3) a person's block deleted by hand in the sheet -> removed from the board too
rs = rows()
top = next(r["row"] for r in rs if r["a"] == "NEWBIE")
after = [r["row"] for r in rs if r["row"] > top and any(m.startswith("itb_member=") for m in r["md"])]
end = (min(after) - 1) if after else max(r["row"] for r in rs)
for _ in range(end - top + 1):
    req(f"{S}/deleteRow", "POST", {"row": top})
check("sheet block deleted by hand", "NEWBIE" not in sheet_names())
check("person deleted in the sheet -> removed from the board", wait(lambda: "NEWBIE" not in board_names()), board_names())
time.sleep(12)
check("and not put back on either side", "NEWBIE" not in board_names() and "NEWBIE" not in sheet_names())

# 4) the person's id tag ends up on their month row ("September 2026"): what turned BONCAP / WILL
#    into "Person 2" / "Person 3" with new ids. Same person, same id, real name, no duplicate.
def block_rows_of(name):
    rs = rows()
    name_row = next(r for r in rs if r["a"] == name)
    return rs, name_row, next(r for r in rs if r["row"] == name_row["row"] + 1)


before = board_names()
rs, name_row, month_row = block_rows_of(last)
req(f"{S}/setMd", "POST", {"row": name_row["row"], "key": "other", "value": "x"})          # tag off the name row
req(f"{S}/setMd", "POST", {"row": month_row["row"], "key": "itb_member", "value": "mreidentified01"})  # onto the month row
req(f"{S}/tick")
time.sleep(14)
ms = req(f"{B}/api/state?week={CW}")["members"]
check("tag on the month row: same person, same id, real name (no 'Person N')",
      board_names() == before and any(m["id"] == "mreidentified01" and m["name"] == last for m in ms), [(m["id"], m["name"]) for m in ms])
check("tag on the month row: sheet reports them by name, once", sheet_names().count(last) == 1 and not any(n.startswith("Person ") for n in sheet_names()), sheet_names())

# 5) two different id tags in one person's name/month rows: one id is kept, the stray one is dropped
req(f"{S}/setMd", "POST", {"row": name_row["row"], "key": "itb_member", "value": "mstraytag0001"})
req(f"{S}/tick")
time.sleep(14)
ms = req(f"{B}/api/state?week={CW}")["members"]
check("two tags in one person: still shown once on the board, under the id it already had",
      board_names() == before and any(m["id"] == "mreidentified01" for m in ms), [(m["id"], m["name"]) for m in ms])
check("two tags in one person: the stray tag is removed from the sheet",
      sum(1 for r in rows() if r["row"] in (name_row["row"], month_row["row"]) for x in r["md"] if x.startswith("itb_member=")) == 1)
last_id = next(m["id"] for m in ms if m["name"] == last)

# values still flow both ways for the re-identified person
s = req(f"{B}/api/state?week={CW}")
m = next(x for x in s["members"] if x["id"] == "mreidentified01")
t = next(x for x in m["tasks"] if x.get("type") != "check")
lg = s["logs"].get(m["id"]) or {"cats": m["cats"], "tasks": m["tasks"], "values": {}}
vals = dict(lg.get("values") or {}); vals[t["id"]] = [0, 0, 0, 0, 0, 0, 6]
req(f"{B}/api/logs/{m['id']}/{CW}", "PUT", {"cats": lg["cats"], "tasks": lg["tasks"], "values": vals})
hdr = next(r for r in rows() if r["a"] == "Input")["cells"]
col = hdr.index("Mon 10/19") + 1  # 1-based Monday column of this week
trow = next(r["row"] for r in rows() if any(x == "itb_task=" + t["id"] for x in r["md"]))
check("board -> sheet for the re-identified person", wait(lambda: req(f"{S}/cell?r={trow}&c={col + 6}") == 6))
req(f"{S}/edit", "POST", {"row": trow, "col": col + 5, "value": 2})
check("sheet -> board for the re-identified person",
      wait(lambda: req(f"{B}/api/state?week={CW}")["logs"][m["id"]]["values"][t["id"]][5] == 2))
print("FAILURES:", fails or "none")

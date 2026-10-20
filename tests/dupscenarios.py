"""Every way a person (or an input) could end up twice: after each scenario the board must list exactly
the people the sheet has (same ids, same names, each once), with the same inputs, nothing left to send,
no error - and still so after a catch-up round. Run with restart.sh (it restarts the board itself)."""
import collections, json, os, subprocess, sys, time, urllib.request
B, S = "http://localhost:" + __import__("os").getenv("BOARD_PORT", "8099"), "http://localhost:" + __import__("os").getenv("MOCK_PORT", "8090")
RESTART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restart.sh")
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


def sheet():
    return req(f"{S}/exec?secret=s3cret")


def board():
    return req(f"{B}/api/state?week={CW}")


def problems():
    s, b, st = sheet(), board(), req(f"{B}/api/sync/status")
    sm, bm = {m["id"]: m for m in s["members"]}, {m["id"]: m for m in b["members"]}
    out = []
    sn, bn = collections.Counter(m["name"] for m in sm.values()), collections.Counter(m["name"] for m in bm.values())
    if sn != bn:
        out.append("people differ: sheet %s / board %s" % (dict(sn), dict(bn)))
    if set(sm) != set(bm):
        out.append("ids differ: only sheet %s, only board %s" % (sorted(set(sm) - set(bm)), sorted(set(bm) - set(sm))))
    for mid in set(sm) & set(bm):
        si = sorted(t["name"] for t in sm[mid]["tasks"])
        bi = sorted(t["name"] for t in bm[mid]["tasks"])
        if si != bi:
            out.append("%s inputs differ: sheet %s / board %s" % (sm[mid]["name"], si, bi))
        dup = [n for n, c in collections.Counter(bi).items() if c > 1]
        if dup:
            out.append("%s has inputs twice: %s" % (bm[mid]["name"], dup))
    if any(n.startswith("Person ") for n in bn):
        out.append("a person lost their name: %s" % sorted(bn))
    if st["outbox"] or st["lastError"]:
        out.append("sync not settled: outbox %s, error %r" % (st["outbox"], st["lastError"]))
    return out


def settle(label, expect=None):
    """The scenario passes when the two sides match (and `expect` holds), and still match after a catch-up."""
    req(f"{S}/tick")
    end, p = time.time() + 45, ["not checked"]
    while time.time() < end:
        try:
            p = problems() + ([] if expect is None or expect() else ["expected result not reached"])
        except Exception as e:
            p = [repr(e)]
        if not p:
            break
        time.sleep(1)
    if p:
        check(label, False, "; ".join(p))
        return
    time.sleep(12)  # one catch-up round (SYNC_INTERVAL 10 s)
    req(f"{S}/tick")
    p = problems() + ([] if expect is None or expect() else ["expected result lost after catch-up"])
    check(label, not p, "; ".join(p))


def rows():
    return req(f"{S}/dump")["rows"]


def name_row(name):
    """The row holding a person's id tag: their name row, or the row right under it."""
    rs = rows()
    tagged = lambda r: any(m.startswith("itb_member=") for m in r["md"])
    for i, r in enumerate(rs):
        if r["a"] == name and (tagged(r) or (i + 1 < len(rs) and tagged(rs[i + 1]))):
            return r if tagged(r) else rs[i + 1]
    raise StopIteration(name)


def block(name):
    """First and last row of a person's block."""
    rs = rows()
    top = name_row(name)["row"]
    nxt = [r["row"] for r in rs if r["row"] > top and any(m.startswith("itb_member=") for m in r["md"])]
    return top, (min(nxt) - 1 if nxt else max(r["row"] for r in rs))


def type_person(name, inputs):
    """Types a new person block at the bottom of the sheet, line by line, like a person would."""
    rs = rows()
    last = max(r["row"] for r in rs)
    hdr = next(r for r in rs if r["a"] == "Input")["cells"]
    hdr = hdr[:max(i for i, v in enumerate(hdr) if v) + 1]
    lines = [[name], ["October 2026"], hdr, ["Habits"]] + [[i, "", "count"] for i in inputs]
    for i in range(len(lines)):
        req(f"{S}/insertRow", "POST", {"after": last + i})
    for i, vals in enumerate(lines):
        req(f"{S}/edit", "POST", {"row": last + 1 + i, "col": 1, "values": [vals]})


def names():
    return [m["name"] for m in board()["members"]]


def board_person(name, inputs):
    mid = "m" + name.lower().ljust(8, "x")[:8] + "001"
    req(f"{B}/api/members/{mid}", "PUT", {"name": name, "week": CW, "cats": [{"id": "c" + mid[1:], "name": "Work"}],
                                         "tasks": [{"id": "t%s%d" % (mid[1:], i), "catId": "c" + mid[1:], "name": n, "target": 3, "unit": "count"}
                                                   for i, n in enumerate(inputs)]})
    return mid


start = names()
last = start[-1]
settle("1. fresh install: everyone imported once")

req(f"{S}/install")
settle("2. sheet script re-installed (rows tagged again): same people, same ids")

req(f"{S}/week?to=2026-10-26")
settle("3. a new week added: no one doubled")

mid = board_person("ALPHA", ["Calls", "Emails"])
settle("4. person added on the board: once on both sides", lambda: names().count("ALPHA") == 1)

type_person("BRAVO", ["Walk", "Read"])
settle("5. person typed into the sheet by hand: once on both sides", lambda: names().count("BRAVO") == 1)

m = next(x for x in board()["members"] if x["id"] == mid)
req(f"{B}/api/members/{mid}", "PUT", {"name": "ALPHA2", "cats": m["cats"], "tasks": m["tasks"], "week": CW})
settle("6. person renamed on the board: renamed, not added", lambda: "ALPHA2" in names() and "ALPHA" not in names())

r = name_row("BRAVO")["row"]
req(f"{S}/edit", "POST", {"row": r, "col": 1, "value": "BRAVO2"})
settle("7. person renamed in the sheet: renamed, not added", lambda: "BRAVO2" in names() and "BRAVO" not in names())

top, _ = block("BRAVO2")
req(f"{S}/insertRow", "POST", {"after": top - 1})
settle("8. row inserted above a person's name row: same person", lambda: names().count("BRAVO2") == 1)

top, _ = block("BRAVO2")
req(f"{S}/insertRow", "POST", {"after": top})
settle("9. blank row inserted between the name row and the month row: same person, same name",
       lambda: names().count("BRAVO2") == 1)
req(f"{S}/deleteRow", "POST", {"row": top + 1})

top, _ = block("BRAVO2")
req(f"{S}/deleteRow", "POST", {"row": top + 1})  # the month row
settle("10. month row deleted: same person", lambda: names().count("BRAVO2") == 1)

r = name_row(last)
req(f"{S}/setMd", "POST", {"row": r["row"], "key": "other", "value": "x"})
settle("11. a person's hidden id tag lost (e.g. row cut and pasted): shown once", lambda: names().count(last) == 1)

r = name_row(last)
idv = next(m.split("=", 1)[1] for m in r["md"] if m.startswith("itb_member="))
req(f"{S}/setMd", "POST", {"row": r["row"], "key": "other", "value": "x"})
req(f"{S}/setMd", "POST", {"row": r["row"] + 1, "key": "itb_member", "value": idv})
settle("12. id tag on the month row instead of the name row: same person, real name",
       lambda: names().count(last) == 1 and any(m["id"] == idv for m in board()["members"]))

mr = next(x for x in rows() if any(m == "itb_member=" + idv for m in x["md"]))
req(f"{S}/setMd", "POST", {"row": mr["row"] - 1, "key": "itb_member", "value": "mstraytag0002"})
settle("13. two id tags on one person: shown once, under the id the board already had",
       lambda: names().count(last) == 1 and any(m["id"] == idv for m in board()["members"]))

r = name_row(last)
req(f"{S}/setMd", "POST", {"row": r["row"], "key": "itb_member", "value": "mbrandnewid01"})
settle("14. a person's block re-identified with a new id: shown once (old copy dropped)",
       lambda: names().count(last) == 1 and any(m["id"] == "mbrandnewid01" for m in board()["members"]))

top, end = block("BRAVO2")
for _ in range(end - top + 1):
    req(f"{S}/deleteRow", "POST", {"row": top})
settle("15. person's block deleted in the sheet: removed from the board, not put back", lambda: "BRAVO2" not in names())

req(f"{B}/api/members/{mid}", "DELETE")
settle("16. person deleted on the board: removed from the sheet, not put back", lambda: "ALPHA2" not in names())

subprocess.run(["bash", RESTART, "stopboard"], capture_output=True)
type_person("CHARLIE", ["Run"])
subprocess.run(["bash", RESTART, "keep", "boardonly"], capture_output=True)
settle("17. person typed into the sheet while the board was down: once after it's back", lambda: names().count("CHARLIE") == 1)

req(f"{S}/down?v=1")
mid2 = board_person("DELTA", ["Plan"])
time.sleep(4)
req(f"{S}/down?v=0")
settle("18. person added on the board while the sheet was unreachable: once after it's back", lambda: names().count("DELTA") == 1)

req(f"{S}/down?v=1")
req(f"{B}/api/members/{mid2}", "DELETE")
time.sleep(4)
req(f"{S}/down?v=0")
settle("19. person deleted on the board while the sheet was unreachable: gone on both sides after", lambda: "DELTA" not in names())

subprocess.run(["bash", RESTART, "fresh", "boardonly"], capture_output=True)  # board data wiped
settle("20. board started from empty against the existing sheet: everyone imported once")

ch = name_row("CHARLIE")
task_row = next(x for x in rows() if x["row"] > ch["row"] and x["a"] == "Run")
req(f"{S}/setMd", "POST", {"row": task_row["row"], "key": "other", "value": "x"})
settle("21. an input's hidden id tag lost: input shown once", lambda: True)

top, end = block("CHARLIE")
src = [x for x in rows() if top <= x["row"] <= end]
last_row = max(x["row"] for x in rows())
for i in range(len(src)):
    req(f"{S}/insertRow", "POST", {"after": last_row + i})
for i, x in enumerate(src):
    vals = [v for v in x["cells"]]
    while vals and vals[-1] in ("", None):
        vals.pop()
    if vals:
        req(f"{S}/edit", "POST", {"row": last_row + 1 + i, "col": 1, "values": [vals]})
cid = next(m.split("=", 1)[1] for m in ch["md"] if m.startswith("itb_member="))
req(f"{S}/setMd", "POST", {"row": last_row + 1, "key": "itb_member", "value": cid})  # the copy carries the same id
settle("22. a person's block copied in the sheet (with its id): the copy becomes its own person, the original keeps its id",
       lambda: names().count("CHARLIE") == 2 and any(m["id"] == cid for m in board()["members"]))
print("FAILURES:", fails or "none")

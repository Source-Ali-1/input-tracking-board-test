"""Checks a real connection, both ways: your board <-> your copy of the sheet (see TESTING.md, part B).

    python tests/live_check.py --exec <web app /exec URL> --secret <SYNC_SECRET> [--board http://localhost:8080]

1. the board is up and its sync is on        4. board -> sheet: a value set on the board appears in the sheet
2. the sheet's web app answers               5. sheet -> board: you type a number in the sheet, it appears on the board
3. both sides list the same people           6. both cells are put back, and stay matched after a catch-up
Only one cell of this week is touched, and it is restored at the end. Use it on copies, not the real tracker.
"""
import argparse, datetime as dt, json, os, sys, time, urllib.parse, urllib.request

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--board", default=os.getenv("BOARD_URL", "http://localhost:8080"))
ap.add_argument("--exec", dest="exec_url", default=os.getenv("SHEET_SCRIPT_URL", ""))
ap.add_argument("--secret", default=os.getenv("SYNC_SECRET", ""))
ap.add_argument("--code", default=os.getenv("BOARD_CODE", ""), help="board code, if the board has one")
ap.add_argument("--no-typing", action="store_true", help="skip step 5 (typing in the sheet)")
a = ap.parse_args()
if not a.exec_url or not a.secret:
    sys.exit("Give --exec and --secret (or set SHEET_SCRIPT_URL and SYNC_SECRET).")
B = a.board.rstrip("/")
fails = []
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def req(url, method="GET", body=None, timeout=90):
    h = {"Content-Type": "application/json"}
    if a.code:
        h["X-Board-Code"] = a.code
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None, headers=h)
    with urllib.request.urlopen(r, timeout=timeout) as res:
        return json.loads(res.read() or b"{}")


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ("  -- " + str(detail) if detail and not ok else ""), flush=True)
    if not ok:
        fails.append(label)
    return ok


def wait(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        try:
            r = cond()
            if r:
                return r
        except Exception:
            pass
        time.sleep(2)
    return None


def done():
    ok = ("All checks passed: the board and the sheet are connected both ways." if not a.no_typing else
          "All checks passed. Sheet -> board typing was skipped (--no-typing), so only board -> sheet is confirmed.")
    print("\n%s" % (ok if not fails else "%d check(s) failed: %s" % (len(fails), "; ".join(fails))))
    sys.exit(1 if fails else 0)


def sheet(week):
    s = req(a.exec_url + ("&" if "?" in a.exec_url else "?") + urllib.parse.urlencode({"secret": a.secret, "since": week}))
    if not s.get("ok"):
        raise RuntimeError(s.get("error") or ("sheet busy" if s.get("busy") else "no snapshot"))
    return s


def sheet_value(week, mid, tid, day):
    for m, w, t, d, v, _ in sheet(week)["cells"]:
        if (m, w, t, d) == (mid, week, tid, day):
            return v
    return None


def board_value(week, mid, tid, day):
    lg = req(f"{B}/api/state?week={week}")["logs"].get(mid) or {}
    arr = (lg.get("values") or {}).get(tid) or []
    return arr[day] if day < len(arr) else 0


def set_board(week, mid, tid, day, value):
    st = req(f"{B}/api/state?week={week}")
    m = next(x for x in st["members"] if x["id"] == mid)
    lg = st["logs"].get(mid) or {"cats": m["cats"], "tasks": m["tasks"], "values": {}}
    vals = dict(lg.get("values") or {})
    arr = list(vals.get(tid) or [0] * 7)
    arr[day] = value
    vals[tid] = arr
    req(f"{B}/api/logs/{mid}/{week}", "PUT", {"cats": lg["cats"], "tasks": lg["tasks"], "values": vals})


# 1. board
try:
    req(f"{B}/health")
    st = req(f"{B}/api/sync/status")
except Exception as e:
    check("board reachable at " + B, False, e)
    done()
check("board reachable at " + B, True)
check("board sync is on (SHEET_SCRIPT_URL and SYNC_SECRET set)", st.get("enabled"), st)
fresh = st.get("lastPull") and time.time() - st["lastPull"] < 180
check("board has pulled from the sheet in the last 3 min, no errors", fresh and not st.get("lastError"),
      "lastPull %s, lastError %r" % (st.get("lastPull"), st.get("lastError")))

# 2. sheet web app
today = dt.date.today()
week = (today - dt.timedelta(days=today.weekday())).isoformat()
try:
    snap = sheet(week)
    week = snap.get("currentWeek") or week
    check("sheet web app answers with the secret", True)
except Exception as e:
    check("sheet web app answers with the secret", False, e)
    done()

# 3. same people
board_members = req(f"{B}/api/state?week={week}")["members"]
s_names = sorted(m["name"] for m in snap["members"])
b_names = sorted(m["name"] for m in board_members)
check("same people on both sides", s_names == b_names, "sheet %s / board %s" % (s_names, b_names))

# pick one number input of this week that both sides know
pick = None
for m in snap["members"]:
    if any(x["id"] == m["id"] for x in board_members):
        t = next((t for t in m["tasks"] if t.get("type") != "check"), None)
        if t:
            pick = (m, t)
            break
if not pick:
    check("found a number input to test with", False, "no person with a number input on both sides")
    done()
m, t = pick
mid, tid = m["id"], t["id"]
day = 6  # Sunday of this week: the least likely to be in use
label = "%s %d/%d" % (DAYS[day], *(lambda d: (d.month, d.day))(dt.date.fromisoformat(week) + dt.timedelta(days=day)))
orig_b, orig_s = board_value(week, mid, tid, day), sheet_value(week, mid, tid, day)
print("Testing with %s / %s, cell %s (now: board %s, sheet %s)" % (m["name"], t["name"], label, orig_b, orig_s))

# 4. board -> sheet
probe = 7 if orig_b != 7 else 8
set_board(week, mid, tid, day, probe)
t0 = time.time()
ok = wait(lambda: sheet_value(week, mid, tid, day) == probe, 60)
check("board -> sheet: %s set on the board reached the sheet (%.0f s)" % (probe, time.time() - t0), ok)

# 5. sheet -> board (a person has to type: Google only fires the edit trigger for real edits)
if not a.no_typing:
    typed = 3 if probe != 3 else 4
    print("\nNow, in your sheet copy: in %s's block, row \"%s\", column \"%s\" (this week), type %d and press Enter."
          % (m["name"], t["name"], label, typed))
    print("Waiting up to 3 minutes...", flush=True)
    t0 = time.time()
    ok = wait(lambda: board_value(week, mid, tid, day) == typed, 180)
    check("sheet -> board: %d typed in the sheet reached the board (%.0f s)" % (typed, time.time() - t0), ok)

# 6. put it back and confirm the two sides stay matched after a catch-up
restore = orig_b or 0
set_board(week, mid, tid, day, restore)
ok = wait(lambda: sheet_value(week, mid, tid, day) == restore and board_value(week, mid, tid, day) == restore, 60)
check("cell put back to %s on both sides" % restore, ok)
time.sleep(25)  # one catch-up round (SYNC_INTERVAL 10 s; the default 60 s only delays it)
bv, sv = board_value(week, mid, tid, day), sheet_value(week, mid, tid, day)
check("still matched after a catch-up (board %s, sheet %s)" % (bv, sv), bv == sv)
done()

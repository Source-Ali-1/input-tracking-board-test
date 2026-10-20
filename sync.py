"""Two-way sync between the board and the team's Google Sheet.

The sheet side is an Apps Script web app (apps-script/Code.gs). Traffic:

  sheet edit -> script onEdit -> POST /api/sync/inbound              (instant)
  board edit -> outbox table  -> sender thread -> script doPost      (instant, retried)
  every SYNC_INTERVAL seconds -> reconciler GETs the script's doGet snapshot
                                 and settles any difference both ways, so the
                                 two sides converge after either was unreachable.

Conflicts are last-write-wins per cell: every day cell and every week's target
carries the millisecond timestamp of its last edit on both sides (cell_meta
here, the hidden _sync tab in the sheet). Member setups (name, sections,
inputs) are last-write-wins on members.updated_at vs the sheet's setup stamp.

Sync is off unless SHEET_SCRIPT_URL and SYNC_SECRET are both set.
"""

import copy
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

SCRIPT_URL = os.getenv("SHEET_SCRIPT_URL", "").strip()
SECRET = os.getenv("SYNC_SECRET", "").strip()
ENABLED = bool(SCRIPT_URL and SECRET)
INTERVAL = max(float(os.getenv("SYNC_INTERVAL", "10")), 5)
RECENT_WEEKS = int(os.getenv("SYNC_WEEKS", "8"))
FULL_EVERY = 6 * 3600       # also re-check every week in the sheet this often
MAX_ATTEMPTS = 5            # drop a change the script keeps rejecting

ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TARGET = -1                 # cell_meta.day used for a week's target

# Serialises read-modify-write of members/logs between requests and the sync threads.
LOCK = threading.RLock()
log = logging.getLogger("itb.sync")
state = {"lastPush": None, "lastPull": None, "lastError": None, "recent": []}
_wake = threading.Event()
_cfg = {}
_started = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS cell_meta (
    member_id TEXT NOT NULL,
    week TEXT NOT NULL,
    task_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (member_id, week, task_id, day)
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tombstones (
    member_id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    member_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    PRIMARY KEY (member_id, item_id)
);
"""


def configure(db_path, clean_setup):
    _cfg.update(db=db_path, clean=clean_setup)


def connect():
    conn = sqlite3.connect(_cfg["db"], timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def now_ms():
    return int(time.time() * 1000)


def monday(weeks_back=0):
    d = dt.date.today()
    return (d - dt.timedelta(days=d.weekday() + 7 * weeks_back)).isoformat()


def kick():
    _wake.set()


def _num(v):
    try:
        return max(round(float(v), 2), 0)
    except (TypeError, ValueError):
        return 0


def _same(a, b):
    return abs(_num(a) - _num(b)) < 1e-9


# ---------- storage helpers ----------

def _member(conn, mid):
    row = conn.execute("SELECT data, updated_at FROM members WHERE id = ?", (mid,)).fetchone()
    return (json.loads(row["data"]), round(row["updated_at"] * 1000)) if row else None


def _log(conn, mid, week):
    row = conn.execute("SELECT data FROM logs WHERE member_id = ? AND week = ?", (mid, week)).fetchone()
    return json.loads(row["data"]) if row else None


def _save_log(conn, mid, week, data):
    conn.execute(
        "INSERT INTO logs (member_id, week, data, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(member_id, week) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
        (mid, week, json.dumps(data), time.time()),
    )


def _ts(conn, mid, week, tid, day):
    row = conn.execute(
        "SELECT ts FROM cell_meta WHERE member_id = ? AND week = ? AND task_id = ? AND day = ?",
        (mid, week, tid, day),
    ).fetchone()
    return row[0] if row else 0


def _set_ts(conn, mid, week, tid, day, ts):
    conn.execute(
        "INSERT INTO cell_meta (member_id, week, task_id, day, ts) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(member_id, week, task_id, day) DO UPDATE SET ts = excluded.ts",
        (mid, week, tid, day, ts),
    )


def _tombstoned(conn, mid):
    return conn.execute("SELECT 1 FROM tombstones WHERE member_id = ?", (mid,)).fetchone() is not None


def _enqueue(conn, change):
    if ENABLED:
        conn.execute("INSERT INTO outbox (payload) VALUES (?)", (json.dumps(change),))


def _setup_change(mid, data, ts):
    return {"kind": "setup", "member": mid, "name": data.get("name", ""),
            "cats": data.get("cats", []), "tasks": data.get("tasks", []), "ts": ts}


# ---------- board -> sheet (called from the API routes, inside LOCK) ----------

def on_member_saved(conn, mid, data, ts, from_week=None, changed_targets=None):
    conn.execute("DELETE FROM tombstones WHERE member_id = ?", (mid,))
    change = _setup_change(mid, data, ts)
    if from_week:
        change["fromWeek"] = from_week
    if changed_targets is not None:
        change["changedTargets"] = changed_targets  # the sheet writes only these KPIs
    _enqueue(conn, change)


def on_member_deleted(conn, mid, ts):
    conn.execute(
        "INSERT INTO tombstones (member_id, ts) VALUES (?, ?) ON CONFLICT(member_id) DO UPDATE SET ts = excluded.ts",
        (mid, ts),
    )
    conn.execute("DELETE FROM cell_meta WHERE member_id = ?", (mid,))
    _enqueue(conn, {"kind": "delete", "member": mid, "ts": ts})


def on_log_saved(conn, mid, week, old, new, ts):
    """Stamp and queue every day cell and target that this save changed."""
    old_vals = (old or {}).get("values") or {}
    for tid, arr in new["values"].items():
        prev = old_vals.get(tid) or []
        for d, v in enumerate(arr):
            if not _same(v, prev[d] if d < len(prev) else 0):
                _set_ts(conn, mid, week, tid, d, ts)
                _enqueue(conn, {"kind": "cell", "member": mid, "week": week, "task": tid,
                                "day": d, "value": v, "ts": ts})
    if old:
        old_targets = {t["id"]: t.get("target", 0) for t in old.get("tasks", [])}
    else:
        # A week's first save starts from the person's default KPIs; only KPIs set differently
        # for this week are edits (the sheet's own KPIs for the week stay otherwise).
        m = _member(conn, mid)
        old_targets = {t["id"]: t.get("target", 0) for t in (m[0]["tasks"] if m else [])}
    if old_targets:
        for t in new["tasks"]:
            if t["id"] in old_targets and not _same(t["target"], old_targets[t["id"]]):
                _set_ts(conn, mid, week, t["id"], TARGET, ts)
                _enqueue(conn, {"kind": "target", "member": mid, "week": week, "task": t["id"],
                                "value": t["target"], "ts": ts})


# ---------- sheet -> board ----------

def apply_changes(conn, changes, current_week=None):
    """Apply changes pushed by the sheet. Never re-queues them (no echo)."""
    cw = current_week if WEEK_RE.match(str(current_week or "")) else monday()
    applied = 0
    for ch in changes:
        if not isinstance(ch, dict):
            continue
        kind = ch.get("kind")
        if kind == "setup":
            applied += apply_setup(conn, ch, cw)
        elif kind == "cell":
            applied += apply_cell(conn, ch)
        elif kind == "target":
            applied += apply_target(conn, ch, cw)
    conn.commit()
    return applied


def apply_setup(conn, ch, current_week, force=False, created=None):
    mid = str(ch.get("member", ""))
    if not ID_RE.match(mid) or _tombstoned(conn, mid):
        return 0
    ts = int(ch.get("ts") or 0)
    cur = _member(conn, mid)
    if cur and not force and ts <= cur[1]:
        return 0
    cats, tasks = _cfg["clean"](ch)
    # Sections/inputs added on the board that the sheet hasn't received yet are still on their
    # way there: keep them. Anything the sheet has seen before and no longer lists was removed.
    seen = {r[0] for r in conn.execute("SELECT item_id FROM seen WHERE member_id = ?", (mid,))}
    sheet_ids = {c["id"] for c in cats} | {t["id"] for t in tasks}
    pending = False
    if cur:
        for c in cur[0].get("cats", []):
            if c["id"] not in sheet_ids and c["id"] not in seen:
                cats.append(c)
                pending = True
        cat_ids = {c["id"] for c in cats}
        for t in cur[0].get("tasks", []):
            if t["id"] not in sheet_ids and t["id"] not in seen and t.get("catId") in cat_ids:
                tasks.append(t)
                pending = True
    _mark_seen(conn, mid, sheet_ids)
    if pending:
        ts += 1  # newer than the sheet's setup, so the sheet accepts the merged list
    old_tasks = {t["id"]: t for t in (cur[0].get("tasks", []) if cur else [])}
    for t in tasks:
        # The sheet's type comes from the Unit column ("check" = tick box). A type chosen on
        # the board is kept until someone changes that input's unit.
        old = old_tasks.get(t["id"])
        if old and old.get("type") and old.get("unit", "") == t["unit"]:
            t["type"] = old["type"]
    name = str(ch.get("name", "")).strip()[:120] or "Unnamed"
    data = {"name": name, "cats": cats, "tasks": tasks}
    conn.execute(
        "INSERT INTO members (id, data, created_at, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
        (mid, json.dumps(data), created or time.time(), ts / 1000),
    )
    setup_to_logs(conn, mid, cats, tasks, current_week)
    if pending:
        _enqueue(conn, _setup_change(mid, data, ts))
    return 1


def setup_to_logs(conn, mid, cats, tasks, from_week, retarget=()):
    """The weeks from from_week on show the new setup; their per-week targets and values stay.

    retarget: inputs whose KPIs were just set, so those weeks take the setup's KPI for them.
    """
    ids = {t["id"] for t in tasks}
    rows = conn.execute("SELECT week, data FROM logs WHERE member_id = ? AND week >= ?", (mid, from_week)).fetchall()
    for row in rows:
        lg = json.loads(row["data"])
        old_t = {t["id"]: t for t in lg.get("tasks", [])}
        new_tasks = []
        for t in tasks:
            nt = dict(t)
            if t["id"] in old_t and t["id"] not in retarget:
                nt["target"] = old_t[t["id"]].get("target", t["target"])
            new_tasks.append(nt)
        values = {k: v for k, v in (lg.get("values") or {}).items() if k in ids}
        _save_log(conn, mid, row["week"], {"cats": copy.deepcopy(cats), "tasks": new_tasks, "values": values})


def _mark_seen(conn, mid, ids):
    conn.executemany("INSERT OR IGNORE INTO seen (member_id, item_id) VALUES (?, ?)", [(mid, i) for i in ids])


def _valid_ref(ch):
    mid, week, tid = str(ch.get("member", "")), str(ch.get("week", "")), str(ch.get("task", ""))
    if ID_RE.match(mid) and WEEK_RE.match(week) and ID_RE.match(tid):
        return mid, week, tid
    return None


def _log_for_write(conn, mid, week, tid, target=None):
    """The member's log for that week (created from their setup if needed), containing task tid."""
    lg = _log(conn, mid, week)
    m = None
    if lg is None:
        m = _member(conn, mid)
        if m is None:
            return None
        lg = {"cats": copy.deepcopy(m[0]["cats"]), "tasks": copy.deepcopy(m[0]["tasks"]), "values": {}}
        if target is not None:
            for t in lg["tasks"]:
                if t["id"] == tid:
                    t["target"] = _num(target)
    lg.setdefault("values", {})
    if any(t["id"] == tid for t in lg["tasks"]):
        return lg
    m = m or _member(conn, mid)
    t = next((t for t in m[0]["tasks"] if t["id"] == tid), None) if m else None
    if not t:
        return None
    if not any(c["id"] == t["catId"] for c in lg["cats"]):
        c = next((c for c in m[0]["cats"] if c["id"] == t["catId"]), None)
        if not c:
            return None
        lg["cats"].append(dict(c))
    t = dict(t)
    if target is not None:
        t["target"] = _num(target)
    lg["tasks"].append(t)
    return lg


def apply_cell(conn, ch, force=False):
    ref = _valid_ref(ch)
    try:
        d = int(ch.get("day"))
    except (TypeError, ValueError):
        return 0
    if not ref or not 0 <= d <= 6 or _tombstoned(conn, ref[0]):
        return 0
    mid, week, tid = ref
    ts = int(ch.get("ts") or 0)
    if not force and ts <= _ts(conn, mid, week, tid, d):
        return 0
    lg = _log_for_write(conn, mid, week, tid, ch.get("target"))
    if lg is None:
        return 0
    arr = list(lg["values"].get(tid) or [])[:7]
    arr += [0] * (7 - len(arr))
    arr[d] = _num(ch.get("value"))
    lg["values"][tid] = arr
    _save_log(conn, mid, week, lg)
    _set_ts(conn, mid, week, tid, d, ts)
    return 1


def apply_target(conn, ch, current_week, force=False):
    ref = _valid_ref(ch)
    if not ref or _tombstoned(conn, ref[0]):
        return 0
    mid, week, tid = ref
    ts = int(ch.get("ts") or 0)
    if not force and ts <= _ts(conn, mid, week, tid, TARGET):
        return 0
    value = _num(ch.get("value"))
    lg = _log_for_write(conn, mid, week, tid)
    if lg is None:
        return 0
    for t in lg["tasks"]:
        if t["id"] == tid:
            t["target"] = value
    _save_log(conn, mid, week, lg)
    _set_ts(conn, mid, week, tid, TARGET, ts)
    if week == current_week:  # weeks not logged yet inherit the current target
        m = _member(conn, mid)
        if m:
            for t in m[0]["tasks"]:
                if t["id"] == tid:
                    t["target"] = value
            conn.execute("UPDATE members SET data = ? WHERE id = ?", (json.dumps(m[0]), mid))
    return 1


# ---------- reconciliation against a full snapshot ----------

def _event(text):
    """Keeps the last 40 sync events for /api/sync/status and prints them to the server log."""
    line = time.strftime("%H:%M:%S") + "  " + text
    state["recent"] = (state["recent"] + [line])[-40:]
    print("[sync] " + line, flush=True)


def note_push(changes, applied, diag):
    """Logs a push from the sheet: how many changes, and how long the sheet's script took."""
    cells = [c for c in changes if isinstance(c, dict) and c.get("kind") in ("cell", "target")]
    timing = ""
    if isinstance(diag, dict) and diag.get("started"):
        timing = ", script %.1f s, arrived %.1f s after the trigger started" % (
            (float(diag.get("sent", 0)) - float(diag["started"])) / 1000, time.time() - float(diag["started"]) / 1000)
    _event("sheet push: %d change(s) (%d values), %d applied%s" % (len(changes), len(cells), applied, timing))


def reconcile(conn, snap):
    """Settle every difference between the sheet snapshot and the board, newest edit wins."""
    caught, imported = [], set()  # imported: people first seen in this snapshot (their values aren't "missed")
    label = {t.get("id"): "%s / %s" % (m.get("name"), t.get("name")) for m in snap.get("members") or [] for t in m.get("tasks") or []}
    cw = snap.get("currentWeek") if WEEK_RE.match(str(snap.get("currentWeek", ""))) else monday()
    # Board changes still waiting in the outbox will be delivered by the sender;
    # don't queue duplicates of them while it catches up.
    queued = conn.execute("SELECT 1 FROM outbox LIMIT 1").fetchone() is not None
    tombs = {r[0] for r in conn.execute("SELECT member_id FROM tombstones")}

    seen, base = set(), time.time()
    for i, m in enumerate(snap.get("members") or []):
        mid = str(m.get("id", ""))
        if not ID_RE.match(mid):
            continue
        seen.add(mid)
        if mid in tombs:
            if not queued:
                _enqueue(conn, {"kind": "delete", "member": mid, "ts": now_ms()})
            continue
        cur = _member(conn, mid)
        sts = int(m.get("setupTs") or 0)
        change = dict(m, kind="setup", member=mid, ts=sts)
        if cur is not None and not sts > cur[1]:
            _mark_seen(conn, mid, [c.get("id") for c in m.get("cats") or []] + [t.get("id") for t in m.get("tasks") or []])
        if cur is None:
            apply_setup(conn, change, cw, force=True, created=base + i / 1000)
            imported.add(mid)
        elif sts > cur[1]:
            apply_setup(conn, change, cw)
        elif cur[1] > sts and not queued:
            _enqueue(conn, _setup_change(mid, cur[0], cur[1]))
    if not queued:
        for row in conn.execute("SELECT id, data, updated_at FROM members").fetchall():
            if row["id"] not in seen:  # added on the board, or its block is missing from the sheet
                _enqueue(conn, _setup_change(row["id"], json.loads(row["data"]), round(row["updated_at"] * 1000)))

    weeks = [w for w in snap.get("weeks") or [] if WEEK_RE.match(str(w))]
    if not weeks:
        return
    metas = {
        (r[0], r[1], r[2], r[3]): r[4]
        for r in conn.execute("SELECT member_id, week, task_id, day, ts FROM cell_meta WHERE week >= ?", (min(weeks),))
    }
    logs, members = {}, {}

    def board_log(mid, week):
        if (mid, week) not in logs:
            logs[(mid, week)] = _log(conn, mid, week)
        return logs[(mid, week)]

    def board_member(mid):
        if mid not in members:
            members[mid] = _member(conn, mid)
        return members[mid]

    for row in snap.get("cells") or []:
        try:
            mid, week, tid, d, sv, sts = row
            d, sv, sts = int(d), _num(sv), int(sts or 0)
        except (TypeError, ValueError):
            continue
        if mid in tombs or not 0 <= d <= 6 or not board_member(mid):
            continue
        arr = ((board_log(mid, week) or {}).get("values") or {}).get(tid) or []
        bv = _num(arr[d]) if d < len(arr) else 0
        if _same(sv, bv):
            continue
        bts = metas.get((mid, week, tid, d), 0)
        # Equal stamps but different values: the sheet cell changed after the last synced
        # write without being stamped (e.g. its edit trigger didn't run), so the sheet is newer.
        if sts >= bts:
            if apply_cell(conn, {"member": mid, "week": week, "task": tid, "day": d, "value": sv, "ts": sts}, force=True):
                logs.pop((mid, week), None)
                if mid not in imported:
                    caught.append("%s, %s %s = %s" % (label.get(tid, tid), week, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d], sv))
        elif not queued:
            _enqueue(conn, {"kind": "cell", "member": mid, "week": week, "task": tid, "day": d, "value": bv, "ts": bts})

    for row in snap.get("targets") or []:
        try:
            mid, week, tid, sv, sts = row
            sv, sts = _num(sv), int(sts or 0)
        except (TypeError, ValueError):
            continue
        m = board_member(mid)
        if mid in tombs or not m:
            continue
        lg = board_log(mid, week)
        source = lg["tasks"] if lg else m[0]["tasks"]
        bt = next((t for t in source if t["id"] == tid), None)
        if bt is None and lg:  # the log predates this input; its target comes from the setup
            bt = next((t for t in m[0]["tasks"] if t["id"] == tid), None)
        if bt is not None and _same(sv, bt.get("target", 0)):
            continue
        bts = metas.get((mid, week, tid, TARGET), 0)
        if bt is None or sts >= bts:
            if apply_target(conn, {"member": mid, "week": week, "task": tid, "value": sv, "ts": sts}, cw, force=True):
                logs.pop((mid, week), None)
                members.pop(mid, None)
        elif not queued:
            _enqueue(conn, {"kind": "target", "member": mid, "week": week, "task": tid,
                            "value": bt.get("target", 0), "ts": bts})
    _report_caught(caught, imported)


# ---------- background threads ----------

def _http(url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    # Apps Script answers with a 302 to googleusercontent.com; urllib follows it as a GET, which is what it expects.
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=60) as res:
        return json.loads(res.read().decode("utf-8"))


def _forget(conn, mid, sent_ts):
    """The sheet no longer has this person (deleted there, or re-created under a new id): drop the
    board's stale copy, unless it was edited on the board after the setup the sheet turned down."""
    cur = _member(conn, mid)
    if not cur or cur[1] > sent_ts:
        return
    for table in ("members", "logs", "cell_meta", "seen"):
        conn.execute("DELETE FROM %s WHERE %s = ?" % (table, "id" if table == "members" else "member_id"), (mid,))
    for row in conn.execute("SELECT id, payload FROM outbox").fetchall():
        if json.loads(row["payload"]).get("member") == mid:
            conn.execute("DELETE FROM outbox WHERE id = ?", (row["id"],))
    _event("removed %s from the board: the sheet no longer has this person" % (cur[0].get("name") or mid))


def _drain():
    """Send the outbox to the sheet in order. False when the sheet couldn't be reached."""
    conn = connect()
    try:
        while True:
            rows = conn.execute("SELECT id, payload FROM outbox ORDER BY id LIMIT 100").fetchall()
            if not rows:
                return True
            try:
                res = _http(SCRIPT_URL, {"secret": SECRET, "changes": [json.loads(r["payload"]) for r in rows]})
            except OSError as e:  # network, timeout, HTTP error
                state["lastError"] = "push: %s" % e
                return False
            except ValueError as e:
                res = {"ok": False, "error": "unreadable response (%s)" % e}
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids))
            sent = {}  # member -> newest setup stamp in this batch
            for r in rows:
                ch = json.loads(r["payload"])
                if ch.get("kind") == "setup":
                    sent[ch.get("member")] = max(sent.get(ch.get("member"), 0), int(ch.get("ts") or 0))
            with LOCK:
                if res.get("ok"):
                    conn.execute("DELETE FROM outbox WHERE id IN (%s)" % marks, ids)
                    for mid in res.get("gone") or []:
                        _forget(conn, mid, sent.get(mid, 0))
                    state["lastPush"] = time.time()
                    _clear_error("push")
                    for mid in res.get("refused") or []:  # e.g. the sheet's only person: keep them
                        conn.execute("DELETE FROM tombstones WHERE member_id = ?", (mid,))
                    if res.get("skipped"):
                        log.info("sheet skipped: %s", res["skipped"])
                else:
                    state["lastError"] = "push: %s" % res.get("error")
                    conn.execute("UPDATE outbox SET attempts = attempts + 1 WHERE id IN (%s)" % marks, ids)
                    dropped = conn.execute("DELETE FROM outbox WHERE attempts >= ?", (MAX_ATTEMPTS,)).rowcount
                    if dropped:
                        log.warning("dropped %d change(s) the sheet kept rejecting: %s", dropped, res.get("error"))
                conn.commit()
            if not res.get("ok"):
                return False
    finally:
        conn.close()


def _sender_loop():
    retry_at, backoff = 0, 2
    while True:
        _wake.wait(5)
        _wake.clear()
        if time.time() < retry_at:
            continue
        try:
            ok = _drain()
        except Exception as e:  # keep the thread alive whatever happens
            log.exception("sync sender")
            state["lastError"] = "push: %s" % e
            ok = False
        if ok:
            retry_at, backoff = 0, 2
        else:
            retry_at, backoff = time.time() + backoff, min(backoff * 2, 300)


def pull_once(full=False):
    params = {"secret": SECRET}
    if not full:
        params["since"] = monday(RECENT_WEEKS)
    snap = _http(SCRIPT_URL + ("&" if "?" in SCRIPT_URL else "?") + urllib.parse.urlencode(params))
    if snap.get("busy"):  # the sheet was busy with edits; this round is skipped, the next one follows
        return
    if not snap.get("ok"):
        raise RuntimeError(snap.get("error") or "snapshot failed")
    conn = connect()
    try:
        with LOCK:
            reconcile(conn, snap)
            conn.commit()
    finally:
        conn.close()
    state["lastPull"] = time.time()
    _clear_error("pull")
    kick()


def _clear_error(kind):
    if str(state["lastError"] or "").startswith(kind):
        state["lastError"] = None


def _reconcile_loop():
    last_full = 0
    while True:
        full = time.time() - last_full > FULL_EVERY
        try:
            pull_once(full)
            if full:
                last_full = time.time()
        except Exception as e:
            log.warning("sync pull failed: %s", e)
            state["lastError"] = "pull: %s" % e
        time.sleep(INTERVAL)


def start():
    global _started
    if not ENABLED or _started:
        return
    _started = True
    threading.Thread(target=_sender_loop, name="sync-sender", daemon=True).start()
    threading.Thread(target=_reconcile_loop, name="sync-reconcile", daemon=True).start()


def _report_caught(caught, imported=()):
    if imported:
        _event("imported %d person(s) from the sheet" % len(imported))
    if caught:
        _event("catch-up fetch applied %d sheet value(s) the instant push missed: %s" % (len(caught), "; ".join(caught[:8])))


def status(conn):
    return dict(state, enabled=ENABLED,
                outbox=conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

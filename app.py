#!/usr/bin/env python3
"""Input Tracking Board - shared weekly input tracker for the community.

Members add themselves, set their own inputs and weekly KPIs, and log
them day by day. Editing is trust-based: anyone with the link (and the
board code, when BOARD_CODE is set) can edit any sheet.

Storage is SQLite. On Railway, mount a volume and point DATA_DIR at it
(e.g. /data) so the board survives redeploys.

Set SHEET_SCRIPT_URL and SYNC_SECRET to keep the board in two-way sync with
the team's Google Sheet (see sync.py and apps-script/SETUP.md).
"""

import hmac
import json
import os
import re
import sqlite3

from flask import Flask, abort, g, jsonify, request

import sync

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_PATH = os.path.join(BASE_DIR, "board.html")
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "board.sqlite3")
BOARD_CODE = os.getenv("BOARD_CODE", "").strip()

ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def get_db():
    if "db" not in g:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    with app.app_context():
        conn = get_db()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS logs (
                member_id TEXT NOT NULL,
                week TEXT NOT NULL,
                data TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (member_id, week)
            );
            CREATE INDEX IF NOT EXISTS logs_week ON logs(week);
            """
        )
        conn.executescript(sync.SCHEMA)
        conn.commit()


def check_code():
    if BOARD_CODE and request.headers.get("X-Board-Code", "") != BOARD_CODE:
        abort(401)


def clean_setup(body):
    """Validate and normalise {cats, tasks} from a request body."""
    cats_in = body.get("cats") if isinstance(body.get("cats"), list) else []
    tasks_in = body.get("tasks") if isinstance(body.get("tasks"), list) else []
    cats, cat_ids = [], set()
    for c in cats_in[:30]:
        if not isinstance(c, dict) or not ID_RE.match(str(c.get("id", ""))):
            continue
        cats.append({"id": c["id"], "name": str(c.get("name", ""))[:40]})
        cat_ids.add(c["id"])
    tasks = []
    for t in tasks_in[:100]:
        if not isinstance(t, dict) or not ID_RE.match(str(t.get("id", ""))) or t.get("catId") not in cat_ids:
            continue
        try:
            target = max(float(t.get("target", 0)), 0)
        except (TypeError, ValueError):
            target = 0
        tasks.append({
            "id": t["id"],
            "catId": t["catId"],
            "name": str(t.get("name", ""))[:80],
            "target": target,
            "unit": str(t.get("unit", ""))[:20],
            "goal": str(t.get("goal", ""))[:120],
            "type": "check" if t.get("type") == "check" else "count",
        })
    return cats, tasks


@app.route("/")
def board():
    with open(PAGE_PATH, "r", encoding="utf-8") as f:
        return f.read()


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "input-tracking-board"})


@app.route("/api/state")
def state():
    check_code()
    week = request.args.get("week", "")
    if not WEEK_RE.match(week):
        abort(400)
    conn = get_db()
    members = []
    for row in conn.execute("SELECT id, data, created_at FROM members ORDER BY created_at"):
        m = json.loads(row["data"])
        m["id"] = row["id"]
        m["createdAt"] = row["created_at"]
        members.append(m)
    logs = {}
    for row in conn.execute("SELECT member_id, data FROM logs WHERE week = ?", (week,)):
        logs[row["member_id"]] = json.loads(row["data"])
    return jsonify({"members": members, "logs": logs, "codeRequired": bool(BOARD_CODE)})


@app.route("/api/members/<member_id>", methods=["PUT"])
def put_member(member_id):
    check_code()
    if not ID_RE.match(member_id):
        abort(400)
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()[:60] or "Unnamed"
    cats, tasks = clean_setup(body)
    doc = {"name": name, "cats": cats, "tasks": tasks}
    ts = sync.now_ms()
    now = ts / 1000
    conn = get_db()
    with sync.LOCK:
        conn.execute(
            "INSERT INTO members (id, data, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (member_id, json.dumps(doc), now, now),
        )
        sync.on_member_saved(conn, member_id, doc, ts)
        conn.commit()
    sync.kick()
    return jsonify({"ok": True})


@app.route("/api/members/<member_id>", methods=["DELETE"])
def delete_member(member_id):
    check_code()
    if not ID_RE.match(member_id):
        abort(400)
    conn = get_db()
    with sync.LOCK:
        conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
        conn.execute("DELETE FROM logs WHERE member_id = ?", (member_id,))
        sync.on_member_deleted(conn, member_id, sync.now_ms())
        conn.commit()
    sync.kick()
    return jsonify({"ok": True})


@app.route("/api/logs/<member_id>/<week>", methods=["PUT"])
def put_log(member_id, week):
    check_code()
    if not ID_RE.match(member_id) or not WEEK_RE.match(week):
        abort(400)
    conn = get_db()
    if not conn.execute("SELECT 1 FROM members WHERE id = ?", (member_id,)).fetchone():
        abort(404)
    body = request.get_json(silent=True) or {}
    cats, tasks = clean_setup(body)
    task_ids = {t["id"] for t in tasks}
    values = {}
    raw = body.get("values") if isinstance(body.get("values"), dict) else {}
    for tid, arr in raw.items():
        if tid not in task_ids or not isinstance(arr, list):
            continue
        row = []
        for v in arr[:7]:
            try:
                row.append(max(round(float(v), 2), 0))
            except (TypeError, ValueError):
                row.append(0)
        values[tid] = row + [0] * (7 - len(row))
    doc = {"cats": cats, "tasks": tasks, "values": values}
    ts = sync.now_ms()
    with sync.LOCK:
        old = conn.execute("SELECT data FROM logs WHERE member_id = ? AND week = ?", (member_id, week)).fetchone()
        conn.execute(
            "INSERT INTO logs (member_id, week, data, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(member_id, week) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (member_id, week, json.dumps(doc), ts / 1000),
        )
        sync.on_log_saved(conn, member_id, week, json.loads(old["data"]) if old else None, doc, ts)
        conn.commit()
    sync.kick()
    return jsonify({"ok": True})


@app.route("/api/sync/inbound", methods=["POST"])
def sync_inbound():
    """Changes pushed by the sheet's Apps Script (onEdit, retries, new weeks)."""
    if not sync.SECRET or not hmac.compare_digest(request.headers.get("X-Sync-Secret", ""), sync.SECRET):
        abort(401)
    body = request.get_json(silent=True) or {}
    changes = body.get("changes") if isinstance(body.get("changes"), list) else []
    conn = get_db()
    with sync.LOCK:
        applied = sync.apply_changes(conn, changes, body.get("currentWeek"))
    return jsonify({"ok": True, "applied": applied})


@app.route("/api/sync/status")
def sync_status():
    check_code()
    return jsonify(sync.status(get_db()))


init_db()
sync.configure(DB_PATH, clean_setup)
sync.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

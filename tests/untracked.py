import json, time, urllib.request
B, S = "http://localhost:8099", "http://localhost:8090"
def req(url, method="GET", body=None):
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as res: return json.loads(res.read() or b"{}")
s = req(f"{B}/api/state?week=2026-10-19"); m = next(x for x in s["members"] if x["name"] == "ORLANDO")
t = next(x for x in m["tasks"] if x["name"] == "AI Research")
req(f"{B}/api/logs/{m['id']}/2026-10-19", "PUT", {"cats": m["cats"], "tasks": m["tasks"], "values": {t["id"]: [0, 9, 0, 0, 0, 0, 0]}})
row = next(r for r in req(f"{S}/dump")["rows"] if r["a"] == "AI Research")["row"]
col = 74 + 2
for _ in range(60):
    if req(f"{S}/cell?r={row}&c={col}") == 9: break
    time.sleep(0.5)
print("board value reached sheet:", req(f"{S}/cell?r={row}&c={col}") == 9)
req(f"{S}/edit", "POST", {"row": row, "col": col, "value": 33, "noTrigger": True})   # sheet edit, trigger doesn't fire
for _ in range(60):
    v = req(f"{B}/api/state?week=2026-10-19")["logs"][m["id"]]["values"][t["id"]][1]
    if v == 33: break
    time.sleep(0.5)
print("untracked sheet edit reached board via catch-up:", v == 33, "| sheet still 33:", req(f"{S}/cell?r={row}&c={col}") == 33)

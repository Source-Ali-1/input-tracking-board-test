import json, sys, time, urllib.request
B, S = "http://localhost:" + __import__("os").getenv("BOARD_PORT", "8099"), "http://localhost:" + __import__("os").getenv("MOCK_PORT", "8090")
def req(url, method="GET", body=None):
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as res: return json.loads(res.read() or b"{}")
mode = sys.argv[1]
rows = req(f"{S}/dump")["rows"]
hdrs = [r["row"] for r in rows if r["a"] == "Input"]
ai = next(r for r in rows if r["a"] == "AI Research")["row"]
labels = [d + " 10/" + str(26 + i) if 26 + i <= 31 else d + " 11/" + str(26 + i - 31) for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])]
if mode == "odd":
    labels = ["Monday 26 Oct", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
for h in hdrs:   # hand-made week 9 at columns 84 (KPI) .. 93
    req(f"{S}/edit", "POST", {"row": h, "col": 85, "values": [labels], "noTrigger": True})
req(f"{S}/edit", "POST", {"row": ai, "col": 84, "values": [[3, 1, 1]], "noTrigger": True})   # KPI 3, Mon 1, Tue 1
s = req(f"{B}/api/state?week=2026-10-26"); m = next(x for x in s["members"] if x["name"] == "ORLANDO")
t = next(x for x in m["tasks"] if x["name"] == "AI Research")
req(f"{B}/api/logs/{m['id']}/2026-10-26", "PUT", {"cats": m["cats"], "tasks": m["tasks"], "values": {t["id"]: [1, 1, 5, 0, 0, 0, 0]}})
time.sleep(8)
d = req(f"{S}/dump"); h0 = next(r for r in d["rows"] if r["a"] == "Input")["cells"]
row = next(r for r in d["rows"] if r["a"] == "AI Research")["cells"]
print(mode, "| header cols 85-86:", h0[84:86], "| any block at 94+:", any(str(c).strip() for c in h0[93:110]),
      "| AI Research week-9 KPI/Mon/Tue/Wed:", row[83:87])

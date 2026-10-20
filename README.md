# Input Tracking Board ⇄ Google Sheet sync (test version)

A weekly inputs board (Flask + SQLite) that keeps a two-way sync with a Google Sheet tracker:

- **Sheet → board:** an edit in the sheet shows on the board within seconds.
- **Board → sheet:** an edit on the board is written into the sheet.

This repo is for **trying the sync on copies**. It never touches the real tracker or the live board.

**Start here: [TESTING.md](TESTING.md)**

- **No Google account:** run `bash tests/runall.sh`. It runs the real sheet script against a mock sheet and a local board, and checks every change in both directions. Expected: `TOTAL: 157 passed, 0 failed`.
- **Your own copy of the sheet:** set up the script and a local board, then run `python tests/live_check.py` to check board → sheet and sheet → board.

| Path | What it is |
|---|---|
| `app.py`, `board.html` | the board (`python app.py`) |
| `sync.py` | the board's side of the sync |
| `apps-script/Code.gs` | the sheet's side: paste into Extensions → Apps Script |
| `apps-script/SETUP.md` | full setup of the sheet script |
| `tests/` | the simulation and the live connection check |

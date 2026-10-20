# Input Tracking Board ⇄ Google Sheet sync (test version)

A weekly inputs board (Flask + SQLite) that keeps a two-way sync with a Google Sheet tracker:

- **Sheet → board:** an edit in the sheet shows on the board within seconds.
- **Board → sheet:** an edit on the board is written into the sheet.

This repo is for **trying the sync on copies**. It never touches the real tracker or the live board.

**Start here: [TESTING.md](TESTING.md).** You'll need your own copy of the tracker (ask for the original) and a blank board on your computer.

| Path | What it is |
|---|---|
| `app.py`, `board.html` | the board (`python app.py`) |
| `sync.py` | the board's side of the sync |
| `requirements.txt` | what the board needs (Flask) |
| `apps-script/Code.gs` | the sheet's side: paste into Extensions → Apps Script |
| `apps-script/SETUP.md` | full setup of the sheet script |
| `Procfile`, `railway.json` | how Railway runs the live board (not needed for testing) |

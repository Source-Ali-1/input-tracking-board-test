# Testing the board ⇄ Google Sheet sync on copies

Everything here runs on **copies**: a mock sheet or your own copy of the tracker, plus a board on your computer.
Nothing touches the real tracker or the live board.

There are two ways to test:

- **A. The simulation.** About 12 minutes. No Google account needed. This is the quickest way to check that everything still works.
- **B. Your own copy of the sheet.** About 20 minutes. This lets you click around in a real Google Sheet and a real board.

## You need

- **Python 3.10+.** Install Flask with `pip install -r requirements.txt`, ideally in a `.venv` in the repo folder.
- **Node.js 18+** (runs the sheet script in the mock).
- **bash and curl.** On Windows, use **Git Bash**, which has both.

---

## A. The simulation (no Google account)

`tests/runall.sh` loads the real `apps-script/Code.gs` into a mock Google Sheet (`tests/mock.js`) with the tracker's layout (`tests/now.csv`). It then starts a local board that syncs with it and runs 13 test groups, checking every change in **both directions** (board → sheet and sheet → board).

```bash
git checkout test-copies
pip install -r requirements.txt
bash tests/runall.sh
```

Expected result: one line per group and `TOTAL: 141 passed, 0 failed`.

```
Main scenarios                   passes         (26 checks)
...
Headings vs inputs               passes         (10 checks)
TOTAL: 141 passed, 0 failed
```

**Run a single test group:**

```bash
bash tests/restart.sh                       # fresh mock sheet (port 8090) + board (port 8099)
python tests/headings.py                    # or any other tests/*.py
HEADER_ARRAY=1 bash tests/restart.sh && python tests/formulas.py   # formulas.py needs this mode
bash tests/restart.sh stopall               # stop both when done
```

While it runs, open the test board at <http://localhost:8099>. Logs and data go to `tests/.run/`, which git ignores.

| Test group | What it checks |
|---|---|
| Main scenarios (+ with dates) | Values, KPIs, inputs, people and new weeks syncing both ways; the later edit winning; edits made while the board is down |
| Edge cases | Odd values, empty cells, renames, moves, removals |
| Stale board page | An old open board page doesn't undo newer edits |
| Late trigger race | A slow sheet trigger doesn't overwrite a newer board edit |
| Rows added by hand | A row inserted in the sheet becomes an input with formatting and formulas; the unit decides tick box vs number |
| Person added in sheet | A person block typed into the sheet appears on the board, and back |
| Speed and missed pushes | How fast edits arrive; catch-up when a push is lost |
| Edit without trigger | A sheet change no trigger reported still reaches the board |
| Week total / Win-Loss formulas | No `#REF!`: every input row gets its own formulas for existing people, a new person, a new input, a person copied by hand, and a new week |
| Headings vs inputs | Rows with no unit, no goal and no KPI (Content, Habits) are headings and don't count as KPIs |
| Week added by hand (2) | A week added by hand isn't duplicated or overwritten |

---

## B. With your own copy of the sheet

1. **Copy the sheet.** Open the tracker, go to **File → Make a copy**, and open your copy.
2. **Find its ids.** The copy's URL looks like `docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit#gid=<TAB_GID>`.
3. **Add the script.** Go to **Extensions → Apps Script** and paste in `apps-script/Code.gs`. At the top, set `SPREADSHEET_ID` and `TAB_GID` to **your copy's** values.
4. **Start a board on your computer:**

   ```bash
   SYNC_SECRET=pick-any-long-random-string PORT=8080 python app.py
   ```

5. **Let Google reach it.** Google can't call `localhost`, so open a free tunnel in a second terminal:

   ```bash
   cloudflared tunnel --url http://localhost:8080
   ```

   It prints an `https://….trycloudflare.com` address. `ngrok http 8080` works too.
6. **Follow [apps-script/SETUP.md](apps-script/SETUP.md) steps 1, 2 and 4,** with these changes:
   - `BOARD_URL` = the tunnel address.
   - Skip step 3 (Railway). Instead, restart the board with the web app URL from step 2:

     ```bash
     SHEET_SCRIPT_URL=<the /exec URL> SYNC_SECRET=<same secret> SYNC_INTERVAL=10 PORT=8080 python app.py
     ```

7. **Run `fixFormulas`** once from the Apps Script editor. It gives every input row its own Week total / Win-Loss formula.
8. **Check it's working.** Open <http://localhost:8080>. Within about a minute, everyone in your copy appears. `http://localhost:8080/api/sync/status` should show `"enabled": true` and `"lastError": null`.
9. **Test the connection both ways** with the checker:

   ```bash
   python tests/live_check.py --exec <the /exec URL> --secret <same secret> --board http://localhost:8080
   ```

   - It checks the board and the sheet's web app can reach each other and list the same people.
   - It sets a value on the board and waits for it to arrive in your sheet (**board → sheet**).
   - It then asks you to type a number into one named cell in your sheet and waits for it on the board (**sheet → board**).
   - Finally it puts that cell back and confirms both sides still match after a catch-up.

   It only touches Sunday of the current week for one input. Expected result: `All checks passed: the board and the sheet are connected both ways.`

Then try changes in **both directions** by hand:

| Try | Expected |
|---|---|
| Type a number in a Mon–Sun cell in the sheet | Shows on the board within a few seconds |
| Change a value on the board | Appears in the sheet; Week total and Win / Loss update |
| Add a person on the board | A new block appears in the sheet, with Week total and Win / Loss working (no `#REF!`) |
| Add an input on the board, or insert a row with name + unit in the sheet | Shows on the other side, with formulas in every week |
| Look at a section heading (e.g. "Content") | It isn't counted in "x of y KPIs hit" on the board |

When you're done, delete the script's triggers (**Triggers** page in Apps Script) and stop the board and the tunnel.

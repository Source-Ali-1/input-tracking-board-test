# Testing the board ⇄ Google Sheet sync on copies

Everything here runs on **copies**: your own copy of the tracker, plus a board on your computer.
Nothing touches the real tracker or the live board. About 20 minutes.

## You need

- **The tracker.** Ask for the original; you'll make your own copy of it.
- **Python 3.10+.** Install Flask with `pip install -r requirements.txt`, ideally in a `.venv` in the repo folder.
- **cloudflared** (a free tunnel, so Google can reach the board on your computer). `ngrok` works too.
- **bash.** On Windows, use **Git Bash**.

---

## Set it up from scratch

**Always start from scratch: a fresh copy of the tracker and a blank board.** What the repo keeps is the code: the script (`apps-script/Code.gs`) and the board. The board's data (`data/board.sqlite3`, never in git) holds the people and ids of one particular sheet copy; reusing it with another copy makes duplicates and mismatches. A blank board takes everyone from the sheet with the sheet's own ids, so nobody is doubled.

1. **Copy the sheet.** Open the **original** tracker (not a copy someone already synced), go to **File → Make a copy**, and open your copy.
2. **Add the script.** Go to **Extensions → Apps Script**, paste in `apps-script/Code.gs` and press **Save**. You don't need to change `SPREADSHEET_ID` or `TAB_GID` at the top: the setup wizard (step 5) uses the copy it's opened in.
3. **Start a blank board on your computer.** Delete the `data` folder first if it exists from an earlier run, so the board starts with nobody on it:

   ```bash
   rm -rf data
   SYNC_SECRET=pick-any-long-random-string PORT=8080 python app.py
   ```

   Open http://localhost:8080: it must show nobody. Don't add anyone on the board until the first sync has brought everyone over (brand-new people are fine after that).

4. **Let Google reach it.** Google can't call `localhost`, so open a free tunnel in a second terminal:

   ```bash
   cloudflared tunnel --url http://localhost:8080
   ```

   It prints an `https://….trycloudflare.com` address. Keep this terminal open: closing it changes the address.
5. **Run the setup wizard.** Reload your copy of the sheet, then choose **Board sync → Set up the sync** (allow the permissions the first time and open it again). Pick the tracker tab, type the tunnel address and the same secret, and click **Check**. It must say the board has **0 people** and list everyone in your copy. Then click **Install the sync**.
   If it shows a stop or a warning about people on the board, your board isn't blank: see "Starting over" below.
6. **Let the board write to the sheet.** Deploy the script as a web app ([apps-script/SETUP.md](apps-script/SETUP.md) step 3, items 1 to 3), then restart the board with the web app URL:

   ```bash
   SHEET_SCRIPT_URL=<the /exec URL> SYNC_SECRET=<same secret> SYNC_INTERVAL=10 PORT=8080 python app.py
   ```

7. **Check it's working.** Within about a minute, everyone in your copy appears on <http://localhost:8080>, **once each**. In the sidebar, **Check the connection** should say the sync is running (or open `http://localhost:8080/api/sync/status`: `"enabled": true`, `"outbox": 0`, `"lastError": null`).

Then try changes in **both directions** by hand:

| Try | Expected |
|---|---|
| Type a number in a Mon–Sun cell in the sheet | Shows on the board within a few seconds (if you type fast, a few may take up to the next catch-up, about 10 s here) |
| Change a value on the board | Appears in the sheet; Week total and Win / Loss update |
| Change a weekly KPI on the board, or in a week's Target cell in the sheet | The other side shows the new KPI |
| Add a person on the board | A new block appears in the sheet, with Week total and Win / Loss working (no `#REF!`) |
| Delete that person on the board | Their block disappears from the sheet and doesn't come back |
| Add an input on the board, or insert a row with name + unit in the sheet | Shows on the other side, with formulas in every week |
| Give that new input a KPI (on the board, or in this week's Target cell in the sheet) | The KPI shows in this and the later weeks on both sides; Week total and Win / Loss work |
| Log a few days for it on the board | They appear in the sheet; the board shows the total, progress and Win when the KPI is reached |
| Rename an input on either side | Renamed on the other side |
| Remove an input on the board | Its row is struck through in the sheet (past weeks keep their numbers) |
| Type a number or a new name in that struck row | The strike goes and the input is back on the board |
| Look at a section heading (e.g. "Content") | It isn't counted in "x of y KPIs hit" on the board |
| Wait 2 minutes, reload both | Still one of each person on both sides |

When you're done, delete the script's triggers (**Triggers** page in Apps Script) and stop the board and the tunnel.

---

## Starting over

To test again from scratch, or if anything looks doubled or mismatched:

1. In Apps Script, **Triggers** page: delete all the script's triggers.
2. Stop the board (Ctrl+C) and the tunnel, then delete the board's `data` folder.
3. Make a **new** copy of the original tracker (don't reuse the old copy: it carries the old hidden ids).
4. Follow "Set it up from scratch" again from step 1.

---

## Already set up? Getting an update

1. Get the new code:

   ```bash
   git pull
   ```

2. Stop the board and start it again with the same command as before.
3. In Apps Script, replace all of `Code.gs` with the new `apps-script/Code.gs` and press **Save**.
4. Go to **Deploy → Manage deployments → ✏️ Edit → Version: New version → Deploy**. The `/exec` URL stays the same.
5. Run **`fixFormulas`** once from the function dropdown (or run **Board sync → Set up the sync** and **Install the sync** again, which does the same).

Within about a minute, the board drops any stale copies of people (for example the same person listed twice).
Check step 7 above again to confirm it works.

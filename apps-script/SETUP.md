# Google Sheet ⇄ board sync: switch-on walkthrough

`Code.gs` runs inside the tracker spreadsheet and keeps it in two-way sync with the board:

- A sheet edit reaches the board instantly through an installable `onEdit` trigger.
- A board edit reaches the sheet instantly: the board POSTs it to this script's web app.
- If either side is unreachable, the board pulls a full snapshot every minute and the script re-sends missed edits every minute. Both sides catch up once the connection is back.
- When the same cell is edited on both sides, the later edit wins.
- Every Monday at about 00:05, the script appends the new week's 10-column block (Target, Mon–Sun, Total, Win / Loss) to the right of the last one, with the targets carried over. It also checks every minute, so a missed trigger fixes itself.

`Code.gs` points at the tracker:

| Setting | Value |
|---|---|
| `SPREADSHEET_ID` | `1B5wWCAhUaPApbTRGekl6QuFzd5BUl0qtYJ0UOj85MC0` |
| `TAB_GID` | `1594333729` |

You need **editor access** to the spreadsheet. The script and its triggers run as whoever installs them.

---

## Step 0: Before you start (about 2 min)

1. **Take a restore point.** In the spreadsheet, go to **File → Version history → Name current version** and call it, for example, "Before board sync". If anything goes wrong, you can restore it in one click.
2. **Check who is on the live board.** The board imports everyone in the sheet, and anyone who is *only* on the board gets a new block at the bottom of the sheet. If the live board has test or old people you don't want in the sheet, delete them on the board first.
3. **Pick a secret,** a long random string, for example from a password manager. The script and the board must both use this exact value. It's called **the secret** below.

## Step 1: Put the script in the spreadsheet (about 5 min)

1. Open the tracker, then go to **Extensions → Apps Script**.
2. Delete what is in `Code.gs`, paste in the whole of this folder's `Code.gs`, and press **Save** (💾).
3. Click **Project Settings** (⚙️ on the left). Under **Script properties**, add:
   - `BOARD_URL`: the live board's URL, e.g. `https://input-tracking-board.up.railway.app` (no `/` at the end).
   - `SYNC_SECRET`: the secret.
4. Go back to **Editor** (`<>`). In the function dropdown at the top, choose **`installTriggers`** and click **Run**.
   - Google asks for permission: click **Review permissions**, choose your account, then **Advanced → Go to (project name) → Allow**.
   - This creates four triggers: on edit, on change, every minute, and Monday 00:05. It also tags every person, section and input row with a hidden id.
   - It does **not** change anything you can see in the sheet. The first visible change is Week 8 being added (see step 4).
5. Choose **`checkSetup`** and click **Run**. In the **Execution log** you should see:
   - `weeks: W1 2026-08-31 @col 4, … W7 2026-10-12 @col 64`
   - `ORLANDO rows 1-24: 4 sections, 18 inputs`, plus lines for `BONCAP` and `WILL`
   - the board's health reply. An error here is fine until step 3 is done.

## Step 2: Deploy the script as a web app (about 2 min)

1. Click **Deploy → New deployment**, then the ⚙️ next to *Select type*, then **Web app**.
2. Set **Execute as** to **Me** and **Who has access** to **Anyone**. The script still rejects any request without the secret.
3. Click **Deploy** and copy the **Web app URL** (it ends in `/exec`).

## Step 3: Connect the board (about 3 min)

1. **Ship the sync code to the live board:** commit and push `sync.py`, `app.py` and `apps-script/` to `main`. Railway redeploys automatically.
2. On the Railway service, open **Variables** and add:

   | Variable | Value |
   |---|---|
   | `SHEET_SCRIPT_URL` | the `/exec` URL from step 2 |
   | `SYNC_SECRET` | the secret |

3. Make sure `DATA_DIR` points at a mounted **volume**, e.g. `/data`. Otherwise the board's data and sync state are wiped on every redeploy.

Railway redeploys. Within about a minute the board pulls the sheet, and ORLANDO, BONCAP and WILL appear with all their weeks.

## Step 4: Check it works (about 5 min)

Open `https://<board>/api/sync/status`. You should see `"enabled": true`, a recent `lastPull`, `"outbox": 0` and `"lastError": null`.

Then try each of these:

| Try | Expected |
|---|---|
| Look at the right-hand end of the sheet | **Week 8, 10/19 to 10/25** has been added, with the targets carried over |
| Type a number into a Mon–Sun cell | It shows on the board within about 5 s |
| Change a value on the board | It appears in the sheet within a few seconds |
| Change a week's Target in the sheet | The board's target updates |
| In the sheet, insert a row in a section and type a name and unit | It appears on the board as a new input |

If something looks wrong:
- **Diagnose:** check the script's **Executions** page (left sidebar) and the `lastError` field of `/api/sync/status`.
- **Stop syncing at once:** delete the script's triggers (⏰ **Triggers** page), or remove `SHEET_SCRIPT_URL` on Railway.
- **Roll back the sheet:** restore the version you named in step 0.

## Later changes to `Code.gs`

Paste the new code, then go to **Deploy → Manage deployments → ✏️ Edit → Version: New version → Deploy**. This keeps the same `/exec` URL. Re-run `installTriggers` only if trigger code changed.

---

## How the sheet maps to the board

| Sheet | Board |
|---|---|
| Person block (name row such as `ORLANDO September 2026` or `WILL`, then the `Input` header row) | member |
| Section row (a name with no unit, no goal and no KPI in any week, e.g. *Skill stacking*, *Content*, *Habits*) | section; not counted as a KPI |
| Input row: A = name, B = Goal served / Notes, C = Unit | input (name, goal, unit) |
| Week block's **Target** cell | that week's target |
| Mon–Sun cells | the daily values |
| Total, Win / Loss | not synced; the sheet's formulas and the board each compute their own |

Notes:
- **Tick box or number:** the **Unit** column decides. Unit `check` (or `done`, `yes/no`, `✓`) makes the input a daily tick box ("Done / not done") on the board; any other unit (`count`, `hours`, `posts`…) makes it a number. Changing the unit in the sheet changes the type; a type picked on the board in *Edit inputs* is kept until the unit changes.
- **Adding an input in the sheet:** insert a row in the section and type the name, goal and unit. The script gives the new row the formatting of the input row next to it and the Week total / Win-Loss formulas in every week. Type its KPI in this week's Target cell: it's copied into the input's empty Target cells in the later weeks (earlier weeks and KPIs already there are left alone), and the board gets them too. A KPI set on the board carries forward the same way. Every input row has its own formulas (run `fixFormulas` once after installing to convert older sheets).
- **Week total and Win / Loss:** the Week total always shows the sum of the days, as on the board; Win / Loss shows once the week has a KPI.
- **Removing an input on the board:** the row is **struck through** in the sheet rather than deleted, so past weeks keep their numbers. To bring it back from the sheet, just type in that row (a number, or a new name): the strike goes and the input is back on the board. To get rid of it for good, delete the row in the sheet.
- **Removing a person:** do it on the board, which deletes their block in the sheet. If you delete a block in the sheet by hand, the board removes that person too (unless someone edited them on the board since).
- **Reordering inputs:** changing the order on the board doesn't reorder sheet rows.
- **Week labels:** keep the week labels in row 1 in the form `Week 8, 10/19 to 10/25`. The script finds the weeks by those labels.
- **Hidden `_sync` tab:** the script uses a hidden `_sync` tab to store edit timestamps. Don't edit or delete it.

## Board variables

| Variable | Meaning |
|---|---|
| `SHEET_SCRIPT_URL` | the script's `/exec` URL; sync is off when unset |
| `SYNC_SECRET` | shared secret, the same as the script property |
| `SYNC_INTERVAL` | optional; seconds between catch-up pulls (default 60) |

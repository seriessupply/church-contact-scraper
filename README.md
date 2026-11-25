# Series Supply church contact scraper

Every Sun–Thu night this finds 50 new Tier A contacts (a named youth/student pastor with their own published email). Youth-ministry inboxes with no name go to a separate Tier B tab. They go into your Google Sheet, with a CSV backup kept as a private download on each GitHub Actions run (never in the repo, which is public).

## Setup (one time, about 10 minutes)

### 1. Create the repo
1. Go to **github.com/new**.
2. Owner: **seriessupply**. Name: **church-contact-scraper**. Choose **Private**. Click **Create repository**.
3. Click **uploading an existing file**. Drag in **everything** from this folder, including the hidden `.github` folder. (On a Mac, press **Cmd + Shift + .** in Finder to show hidden folders.) Click **Commit changes**.

### 2. Make the Google Sheet
1. Go to **sheets.new** and name the sheet **Series Supply Contacts**.
2. Click **Extensions → Apps Script**. Delete what's there, then paste in all of `apps_script/Code.gs`.
3. On line 8, replace `PASTE_THE_SAME_TOKEN_AS_YOUR_SHEET_TOKEN_SECRET` with your token, keeping the quotes. Click **Save**.
4. Click **Deploy → New deployment**. Click the gear and choose **Web app**.
   Set **Execute as: Me** and **Who has access: Anyone**. Click **Deploy**, then **Authorize access** and allow it.
5. Copy the **Web app URL** (it ends in `/exec`).

### 3. Add the two secrets
In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:
- `SHEET_URL`: the Web app URL from step 2
- `SHEET_TOKEN`: the same token you pasted into the Apps Script

### 4. Test it
Go to **Actions → Nightly contact scrape → Run workflow**, type **5** in the box, and click **Run workflow**.
After a few minutes, rows should show up in the sheet. Click into the run to see a summary.

That's it. From now on it runs by itself Sun–Thu nights.

## Every day: send contacts to Instantly
1. Go to **Actions → Export to Instantly → Run workflow** and set how many contacts you want (Tier A only unless you tick Tier B).
2. When it finishes, open the run and download **instantly-csv** under **Artifacts**.
3. Upload the CSV to your Instantly campaign. Those rows get marked **Instantly** in the sheet, so nobody gets exported twice.

## What the columns mean
- **Contacts tab = Tier A only**: a named youth/student pastor with their own email. This is what gets exported to Instantly.
- **Tier B tab**: a youth/student inbox (youth@, students@) or a youth role with no name. Saved for later; tick the box in the export to include them.
- **Tier C (old) tab**: general church emails from before Nov 10, 2026. No longer collected.
- **status**: change it by hand as things happen: new / Instantly / replied / free series / paid / skip.
- **email_source_url**: the page where the church published that email.

## Notes
- Only emails a church published on its own site are collected. Nothing is guessed.
- It skips churches whose site shows no youth or student ministry.
- It works through the US state by state (alphabetical), then Canada. Progress is in `state/progress.json`.
- Dedupe files (`state/contacted.txt`, `state/domains.txt`, `state/exported.txt`) hold SHA-256 fingerprints, never plain emails.

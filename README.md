# Tender Processing

A FastAPI web app that lists your subscribed tenders, and for the ones you select: downloads the documents,
unpacks every zip (nested zips too), reads each file, and checks the company's eligibility with the LLM
pipeline in [ext.py](ext.py). Tenders are processed one at a time in the background while the page shows
live progress. Each row names the criteria a tender fails or could not determine, and the team can mark a
tender shortlisted or dropped. The company profile the tenders are checked against is kept as markdown in
[company_details.md](company_details.md) and is edited on the page as a form (**Edit profile** in the app bar).

## Project structure

```
app.py                       Application factory (create_app) and uvicorn entry point
config.py                    Settings, each overridable by an environment variable
ext.py                       Existing helpers: the TenderTiger login session, tender_list(), prequalification_criteria(),
                             extract_zip_recursive(), LLM summarising, company_details() / save_company_details()
company_details.md           The company profile (markdown) every tender is checked against
routes/
  pages.py                   Router "pages": GET /
  api.py                     Router "api": the JSON endpoints and the {"error": ...} handlers
services/
  tender_service.py          Loads json_response["TenderList"], caches it, maps rows for the table
  download_service.py        Downloads a tender's documents into TARGET_DIRECTORY/<tcno>/ and extracts zips
  analysis_service.py        process_tender_files(tcno_folder, prequalification_link)
                             -> {"verdict", "details", "score", "criteria", ...}; saves the criteria found to
                             eligibility_criteria.md; recheck_saved_criteria(tcno_folder) checks that file again
  company_service.py         Company profile markdown <-> the editor's form blocks; company name
  processing_service.py      Background job: runs queued tenders one by one, tracks progress and results;
                             saves each result to eligibility_result.json and loads those at startup
  decision_service.py        Shortlisted / dropped decisions, saved to decisions.json
utils/
  files.py                   Safe folder/file names, file name from a download response
  logging_setup.py           Console + rotating file logging (logs/app.log)
templates/
  base.html                  Layout and app bar; IBM Plex, Bootstrap 5 (modal, toasts) and Bootstrap Icons
  index.html                 Verdict tabs, run panel, tender table, selection bar, help, details modal,
                             company profile editor; Tom Select for the searchable organisation filter
static/
  css/style.css              The visual system from design/: tokens, table grid, pills, responsive cards
  js/app.js                  Vanilla JS: loading, tabs/search/sort, selection, polling, decisions, modal
design/
  Tender Processing.dc.html  Source design (Claude Design file); open via a local web server to view
test_app.py                  Tests for the API, download/extract, verdict and score parsing
test_ext.py                  Existing tests for ext.py
```

## Setup

Python 3.10 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Put your TenderTiger login and ollama.com API key in `.env` (the file already exists):

```
TENDERTIGER_EMAIL=you@example.com
TENDERTIGER_PASSWORD=your-password
OLLAMA_API_KEY=your-key
```

The app logs in to TenderTiger on first use and keeps the session, logging in again when the site ends it.
The tender list shows the live tenders of the search in `TENDER_SEARCH` in [ext.py](ext.py) (keyword "india");
change it there. The list has pages of 20 tenders, or 50, 100 or 500 as chosen in **Per page** under the table
(remembered by the browser); TenderTiger sends any of them in one request. **Previous** and **Next** move between
pages, with a selection kept from page to page. The **Value** and **Closes** headers have
TenderTiger sort the whole list (closing soonest first by default) and start again from page 1. TenderTiger gives no total, so the last page is
the first one that is not full (or, when the last is exactly full, Next finds an empty page and says so). **Filters** in the toolbar narrows that search with the filters of TenderTiger's
own search page (word, organisation, location, TID, company industry, quantity, value, EMD, publish and closing
dates, GeM, NCB / ICB, funding, MSME, bid documents, startups): TenderTiger applies them, and the browser
remembers the last ones applied. Without the Ollama key the tender list still loads, but every tender fails at
the analysis step with a clear message.

## Run

Development:

```powershell
python app.py
```

Open http://127.0.0.1:5000. The interactive API docs are at http://127.0.0.1:5000/docs.

Production (Windows or Linux), with uvicorn:

```powershell
uvicorn app:create_app --factory --host 0.0.0.0 --port 5000
```

Behind a reverse proxy on another host, add `--proxy-headers --forwarded-allow-ips <proxy-ip>` so the page's
links use the proxy's address and scheme.

**Run a single process.** Job progress and results are kept in memory, so do not use several worker
processes (uvicorn's `--workers`, `gunicorn -w 4`) or `--reload`. The endpoints run in uvicorn's thread pool, so
requests are still served while a job runs. Job progress is lost on restart, but each tender's last result is
saved as `eligibility_result.json` in its folder and loaded again at startup, and its saved criteria are kept, so it
can be re-checked quickly; the downloaded files stay in `downloads/`, and shortlisted / dropped decisions stay in
`decisions.json`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TENDERTIGER_EMAIL` | (none) | TenderTiger login; the tender list, documents and pre-qualification use it |
| `TENDERTIGER_PASSWORD` | (none) | Password of that login |
| `TENDERTIGER_REQUEST_GAP` | `3` | Seconds between two requests to TenderTiger; its firewall blocks bursts with a browser check |
| `OLLAMA_API_KEY` | (none) | ollama.com key used by the LLM analysis |
| `TARGET_DIRECTORY` | `./downloads` | Documents are stored in `TARGET_DIRECTORY/<tcno>/` |
| `DOWNLOAD_TIMEOUT` | `120` | Seconds to wait for the document server |
| `MAX_DOWNLOAD_MB` | `500` | Larger downloads are aborted |
| `DECISIONS_FILE` | `TARGET_DIRECTORY/decisions.json` | Where shortlisted / dropped decisions are saved |
| `COMPANY_DETAILS_FILE` | `./company_details.md` | The company profile; edited on the page or by hand |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, ... |
| `LOG_DIRECTORY` | `./logs` | `app.log` is written here, rotated at 5 MB |
| `PORT` | `5000` | Port for `python app.py` |

## Company profile

Every tender is checked against the markdown in `COMPANY_DETAILS_FILE` (`company_details.md`), and only
against that: a criterion whose company figure is missing becomes Cannot determine. Click **Edit profile** in
the app bar to change it. The editor is a form built from the file: the `#` heading is the profile title, every
`##` heading and `###` sub-heading is a field you can rename, and every markdown table becomes rows of fields.
Any other text appears in a text box. Save (or Ctrl+S) writes the form back to the file as markdown; rows left
completely empty are dropped.

Tables can be restructured in the form too: column names are fields you can rename, **Add row** and **Add
column** sit under each table, a column's × (shown when you point at or tab to its name) removes it, and
**Remove table** removes the table. Removing a table's last column removes the table. Anything that holds
values asks before it is removed. **Add table** at the end of each section adds an empty Item / Value table
there.

**Add heading** (at the end of the form) adds a `##` heading numbered after the last one ("9. "), and **Add
sub-heading** (at the end of each section) adds a `###` sub-heading to it; each comes with an empty Item / Value
table. The trash button next to a heading removes it with everything under it, after asking if any of that has
values. Every heading, sub-heading and column needs a name: a blank one, or a heading that is only its number,
stops the save. The file can still be edited by hand; the form picks up the new structure the next time it
opens. The file is read when each tender's analysis starts, so a change
applies to the tenders checked after it, with no restart. Verdicts already given are not re-checked; select
those tenders and run **Check eligibility** again to update them.

An empty or missing profile fails every analysis straight away, before any LLM call; the editor then starts from a title and a
Company Name row.

## How a tender is processed

1. The browser posts the selected TCNOs. The server looks each one up in the cached tender list, so the
   browser never sees or sends TenderTiger's internal IDs. If a run is in progress, the tenders are added
   to its queue.
2. `TARGET_DIRECTORY/<tcno>/` is emptied, so a re-run never mixes old and new files.
3. The documents are downloaded with the logged-in TenderTiger session, the way the tender page's Download
   All button does it: open the tender's detail page (`Tenderinformation`; without it the site reports no
   documents), ask `DownloadAllFiles` with the TCNO, then fetch the zip with `mode=download`. An ended
   session is logged in again once. The zip is saved in a staging folder and extracted with
   `extract_zip_recursive()` into the TCNO folder; an inner `a.zip` becomes the folder `a/`. A file that is
   not a zip (checked by content, not name) is moved into the TCNO folder. A tender whose `isdocument` is not
   `1` skips this step and is checked on its pre-qualification criteria alone.
4. `process_tender_files()` reads the company profile and fetches the tender's `GetPreQualification` page:
   the text the page passes to `displayText($(".contentPreQua"), "...")` is checked as written, under the
   source name `TenderTiger Pre-Qualification` (many tenders have none). It then converts
   each file to text, keeps what is relevant to eligibility, and asks the LLM to check the company against the
   criteria from all of these sources, listing a criterion stated in several of them once. If the link cannot
   be fetched, `TenderTiger Pre-Qualification` is listed with the files that could not be read. With no files
   and no pre-qualification criteria there is nothing to check, and the tender fails.

   Before that last LLM call, the criteria found are saved as `eligibility_criteria.md` in the TCNO folder:
   a `## <source>` section per file (and one for `TenderTiger Pre-Qualification`), and a
   `## Sources that could not be read` list. A failed final call therefore does not lose the per-file work.
5. The verdict is read from the model's "overall verdict" line: Eligible, Not Eligible, or Requires Review
   (for "Cannot determine", an unreadable answer, or no criteria found). An Eligible verdict becomes
   Requires Review if any file, or the pre-qualification criteria, could not be read, because the check was
   incomplete.
6. The rows of the criteria table (every markdown table with a Status column) become `criteria`, each with
   status `met`, `not_met` or `unknown`. The tender table shows the names of the criteria not met or not
   determined; the score is the percentage of rows whose Status is Met.
7. The verdict and score are for the company bidding alone. The model also writes a `## Joint venture`
   section: whether a JV or consortium may bid (`JV allowed: Yes / No / Not stated`), the maximum number of
   members (lead member included), a table with a row for every criterion of the first table (same name, same
   order) and any JV-only requirement, saying what the lead member, the other members and the JV as a whole
   must meet, with the company checked as lead member, and the other JV conditions.
8. `compare_routes()` lines the two tables up into `comparison`, one row per criterion: what bidding alone
   asks and its status, what the lead member must meet and its status, and the other members' and the JV's
   share. Rows are matched by criterion name; a criterion the JV table leaves out counts as undetermined for
   the lead member. `jv.verdict` is the outcome as JV lead member: Not Eligible if any lead member requirement
   is not met, Eligible if all are met and every source was read, otherwise Requires Review; `null` when no JV
   may bid or the documents do not say. The tender table shows it under the verdict ("As JV lead: Eligible ·
   up to 3 members"). The analysis view puts the two outcomes side by side above the criterion table, and
   highlights in green the criteria the company misses alone but meets as lead member.

   Summarising keeps JV clauses for this. Criteria files saved before JV checking was added may lack them:
   regenerate those to check JV terms. When the model corrects its own table in a revised one, the revised
   rows replace the ones they correct.

**Re-checking with saved criteria.** When a tender's folder already has `eligibility_criteria.md`, checking
it again skips steps 2 to 4: no download, no TenderTiger request, no summarising. The current company profile
is compared with the file's content in one LLM call. Edits to the file are used, apart from the `#` title and
the `>` notes under it. The sources the file lists as unreadable still make the check incomplete, so an
Eligible verdict there still becomes Requires Review. To read the documents again and write a new file,
queue the tender with `"regenerate": true`. On the page, that is the **Regenerate criteria** switch in the
selection bar, or the button of that name in the analysis view, next to **Re-check**. Regenerating empties
the folder first (step 2), so if the download then fails the old file is gone too, and the next check reads
the documents again.

Expect several minutes per tender whose documents are read: each document is summarised in chunks, about 90
LLM calls for a typical tender. A re-check with saved criteria takes one call. A download or analysis error marks only that tender as failed, with a plain-language
`error_title` and `error_action`; the job moves on. The exception is TenderTiger's firewall blocking the app:
then the queued tenders that need TenderTiger are cancelled, and only re-checks with saved criteria go on. Retrying a failed tender reloads the tender list first.
Stopping a run cancels the queued tenders; the one in progress
finishes.

**Saved results.** Every finished check, failed ones included, is saved as `eligibility_result.json` in the TCNO
folder, replacing the previous one; it holds the same fields as `GET /api/result/<tcno>`. On startup the app
loads every such file, so the table, the analysis view and the time estimates come back after a restart; a file
it cannot read is skipped with a warning in the log. Regenerating empties the folder, so the old result is gone
until the new one is saved.

## API

All errors have the shape `{"error": "<message>"}`. A request body that is not valid JSON or does not match
the documented shape is a `400` (not FastAPI's usual `422`).

### GET /api/tenders

Fetches one page of the tender list via `tender_list()` and returns its table rows. `result` is the last result
for tenders already processed, otherwise `null`. `decision` is the team's decision, or `null`.

`page` (from 1, default 1) picks the page and `page_size` (20, 50, 100 or 500; default 20) how many tenders it
holds, asked of TenderTiger as `total` and the offset of the first; another size is a `400`. TenderTiger gives no total, so `has_more` only says the page was full; the next one may still be empty
(`count` 0). TenderTiger also sorts the whole list before paging it: `sort` is `closing` (the default) or `value`,
sent as `sortcolumnname` `closingdate` or `tendervalue`, and `order` is `asc` (the default) or `desc`, sent as
`sortingby`. Its data shows at the ends: by value ascending, the tenders without a value (0) come first; by
value or closing date descending, placeholder figures and dates (₹99,999 Cr, the year 9096) do.

Optional query parameters narrow the search on TenderTiger, as the filters of its search page; omit a
parameter to leave that filter off. `tender_service.search_fields()` turns them into the page's form fields.

| Parameter | Meaning |
|---|---|
| `word`, `organisation`, `location`, `tid`, `industry` | Word search, organisation name, location, TID or tender number, company industry |
| `published_from`, `published_to`, `closing_from`, `closing_to` | Dates, `YYYY-MM-DD`; a range may be open at one end |
| `qty`, `value`, `emd` | An amount (value and EMD in rupees), compared by `qty_op`, `value_op`, `emd_op`: `gt` (default), `gte`, `lt`, `lte`, `eq`, or `between` it and `qty_to`, `value_to`, `emd_to` |
| `gem` | `gem` or `non_gem` |
| `bidding` | `ncb` or `icb` |
| `funding` | `funded` or `non_funded` |
| `msme`, `bid_doc`, `startup` | `true` for MSME tenders, those with bid documents, startup tenders |
| `sort`, `order` | Sort by `closing` (default) or `value`, `asc` (default) or `desc`; not a filter |
| `page`, `page_size` | The page, from 1 (default), of 20 (default), 50, 100 or 500 tenders |

For example `/api/tenders?location=kolkata&value_op=between&value=10000000&value_to=500000000&gem=non_gem&page=2`.
An unknown parameter, a bad value, or a range with one end missing or ending before it starts is a `400`,
before TenderTiger is asked. Every tender listed stays known to the server, so one listed under other filters
(in another browser, say) can still be processed.

```json
{
  "count": 20,
  "page": 1,
  "page_size": 20,
  "has_more": true,
  "sort": "closing",
  "order": "asc",
  "tenders": [
    {
      "tcno": "100091540",
      "name": "Providing Project General Management Services for various construction sites ...",
      "reference": "NWR-SC-PGMS-HQ-2026-27",
      "organisation": "North Western Railway",
      "location": "Multi Location, Multi State",
      "published_date": "01-Sep-2026",
      "closing_date": "18-Nov-2026",
      "days_left": 16,
      "tender_value": 5117971516.64,
      "currency": "INR",
      "has_document": true,
      "has_prequalification": true,
      "has_saved_criteria": true,
      "result": {
        "status": "completed", "verdict": "Not Eligible", "score": 33,
        "error": null, "error_title": null, "error_action": null,
        "checks": {"total": 6, "met": 2, "not_met": ["Annual turnover", "Bid capacity"],
                   "unknown": ["Solvency", "Key personnel"], "unreadable_files": 0,
                   "jv": {"allowed": "yes", "max_members": 3, "verdict": "Not Eligible",
                          "lead": {"total": 2, "met": 1, "not_met": ["Annual turnover"], "unknown": []}}},
        "processed_at": "2026-11-02T07:05:12+00:00", "rechecked": false,
        "criteria_file": "C:\\projects\\tender\\downloads\\100091540\\eligibility_criteria.md"
      },
      "decision": {"decision": "dropped", "decided_at": "2026-11-02T08:10:00+00:00"}
    }
  ]
}
```

The dates come from TenderTiger as `DD-MM-YYYY` and are shown as `18-Nov-2026`; `days_left` counts from
today to the closing date. `has_document` is false when the tender's `isdocument` is not `1`; such a tender
can still be checked, on its pre-qualification criteria alone, while `has_prequalification` (it has the IDs
for a `GetPreQualification` link) is true. `has_saved_criteria` is true when the tender's folder has
`eligibility_criteria.md`, so a check re-uses it. `502` if the tender API or the TenderTiger login fails.

### POST /api/process-tenders

Request:

```json
{"tcnos": ["100091540", "102309372"], "regenerate": false}
```

Response `202`: the job's progress (same shape as `/api/progress`). Starts a job, or adds the tenders to
the running one; tenders already queued or processing are skipped. A tender with saved criteria is checked
against them; `regenerate` (optional, default `false`) reads the documents of every posted tender again and
saves new criteria. `400` for an empty list or an unknown TCNO.

### POST /api/stop

Cancels the queued tenders of the running job; the tender being processed finishes. Response `202` with
the job's progress, `409` if no job is running.

### GET /api/progress

```json
{
  "job_id": "3f6c1e0f8a2b4d7e9c1a5b2d4e6f8a0b",
  "status": "running",
  "total": 2,
  "completed": 0,
  "failed": 0,
  "cancelled": 0,
  "current": {
    "tcno": "100091540",
    "name": "Providing Project General Management Services ...",
    "stage": "Analysing file 2 of 5: NIT/Annexure-II.pdf",
    "step": 2,
    "steps": 6,
    "elapsed_seconds": 74
  },
  "started_at": "2026-11-02T06:58:00+00:00",
  "finished_at": null,
  "stop_requested": false,
  "items": [
    {"tcno": "100091540", "status": "processing", "verdict": null, "score": null, "error": null, "rechecked": false},
    {"tcno": "102309372", "status": "queued", "verdict": null, "score": null, "error": null, "rechecked": true}
  ],
  "processed": 0,
  "percent": 0,
  "average_seconds": 412.6,
  "average_recheck_seconds": 38.2
}
```

`status` is `idle` (nothing has run since the server started), `running` or `completed`.
Item `status` is `queued`, `processing`, `completed`, `failed` or `cancelled`; finished items carry the same
fields as `result` in `/api/tenders`. `step` / `steps` count each file plus the final check (null while
downloading; 1 of 1 for a re-check). An item's `rechecked` says whether it is (or was) checked against its
saved criteria. `average_seconds` is the average time of the tenders whose documents were read (by this server or,
in the saved results, an earlier one), and `average_recheck_seconds` that of the re-checks, used for time estimates
(null before the first one).

### GET /api/result/&lt;tcno&gt;

```json
{
  "tcno": "100091540",
  "name": "Providing Project General Management Services ...",
  "status": "completed",
  "verdict": "Not Eligible",
  "details": "| Criterion | Requirement | Company | Status | Source |\n|---|---|---|---|---|\n| Turnover | ... | ... | Not met | NIT.pdf |\n...\n\n**Overall verdict:** Not eligible ...",
  "score": 33,
  "criteria": [
    {"criterion": "Annual turnover", "requirement": "... ₹767.70 Cr", "company": "₹159.33 Cr",
     "status": "not_met", "source": "NIT/NIT_PGMS.pdf"}
  ],
  "unreadable_files": [],
  "jv": {
    "allowed": "yes",
    "max_members": 3,
    "verdict": "Not Eligible",
    "conditions": ["The lead member holds at least 51% of the JV.", "A JV agreement is submitted with the bid."]
  },
  "comparison": [
    {"criterion": "Annual turnover", "company": "₹159.33 Cr", "source": "NIT/NIT_PGMS.pdf",
     "alone_requirement": "... ₹767.70 Cr", "alone_status": "not_met",
     "lead_member": "60% of ₹767.70 Cr = ₹460.62 Cr", "lead_company": "Not met (₹159.33 Cr)", "lead_status": "not_met",
     "other_members": "20% each", "combined": "₹767.70 Cr"}
  ],
  "checks": {"total": 6, "met": 2, "not_met": ["Annual turnover", "Bid capacity"], "unknown": ["Solvency", "Key personnel"], "unreadable_files": 0,
             "jv": {"allowed": "yes", "max_members": 3, "verdict": "Not Eligible",
                    "lead": {"total": 1, "met": 0, "not_met": ["Annual turnover"], "unknown": []}}},
  "error": null,
  "error_title": null,
  "error_action": null,
  "files": ["NIT.pdf", "Annexure/BOQ.xlsx"],
  "folder": "C:\\projects\\tender\\downloads\\100091540",
  "rechecked": false,
  "criteria_file": "C:\\projects\\tender\\downloads\\100091540\\eligibility_criteria.md",
  "duration_seconds": 412.6,
  "processed_at": "2026-11-02T07:05:12+00:00"
}
```

A failed tender has `"status": "failed"`, `verdict` and `details` set to `null`, the raw reason in `error`,
and what failed and what to do next in `error_title` and `error_action`. `rechecked` is true when the result
came from the saved criteria; `criteria_file` is the saved criteria file after the run, or `null`.
`jv.allowed` is `yes`, `no` or `unknown` (not stated); `max_members` is `null` when not stated or no JV is
allowed; `jv.verdict` is the outcome as JV lead member (see step 8), `null` without a JV. In `comparison`,
`alone_requirement` and `alone_status` are `null` for a requirement only a JV has, and the `lead_*`,
`other_members` and `combined` fields are `null` when no JV may bid. Statuses are `met`, `not_met` or `unknown`.
A failed tender has `"jv": null` and an empty `comparison`.
`404` if the tender has no result: not processed since the server started, and no saved result in its folder.

### PUT /api/decision/&lt;tcno&gt;

Records the team's decision on a tender. It is saved to `DECISIONS_FILE`, so it survives a restart.

```json
{"decision": "dropped"}
```

`decision` is `"shortlisted"`, `"dropped"` or `null` (clears it). Response `200`:
`{"tcno": "100091540", "decision": {"decision": "dropped", "decided_at": "..."}}`, with `decision` `null`
after clearing. `400` for a missing or unknown decision, `404` for an unknown TCNO.

### GET /api/company

Returns the company profile as markdown (`details`), as the blocks the editor form is built from, and the
company name shown in the app bar:

```json
{
  "details": "# Company Eligibility Assessment (Sample Template)\n\n## 1. Basic Information\n\n| Item | Value |\n...",
  "blocks": [
    {"type": "heading", "level": 1, "text": "Company Eligibility Assessment (Sample Template)"},
    {"type": "heading", "level": 2, "text": "1. Basic Information"},
    {"type": "table", "columns": ["Item", "Value"],
     "rows": [["Company Name", "ABC Infrastructure Ltd."], ["Constitution", "Public Limited Company"]]},
    {"type": "rule"}
  ],
  "name": "ABC Infrastructure Ltd."
}
```

Block `type` is `heading`, `table` (every row has one cell per column), `text` (anything else, such as notes)
or `rule` (a `---` line). `name` comes from the `| Company Name | ... |` table row. An empty or missing profile returns `"details": ""` and starter blocks.

### PUT /api/company

Saves the profile edited in the form. Send every block, in the shape `GET /api/company` returns:

```json
{"blocks": [{"type": "heading", "level": 1, "text": "Company Profile"},
            {"type": "table", "columns": ["Item", "Value"], "rows": [["Company Name", "ABC Infrastructure Ltd."]]}]}
```

The blocks are written to `COMPANY_DETAILS_FILE` as markdown. Rows with every cell empty are dropped, and a
cell is kept on one line with any `|` escaped, and a `rule` with nothing to separate is dropped. Response `200`:
the saved profile, in the same shape as `GET /api/company`. `400` for a body without valid blocks, a heading
below the title with no name (blank, or only a number such as `"9."`), a table column with no name, or blocks
that leave the profile empty.

## Tests

```powershell
python -m unittest test_app test_ext
```

The tests replace the tender API, the download and the LLM with fakes, so they run offline in about a second.

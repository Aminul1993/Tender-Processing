# Tender Processing

[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Groq](https://img.shields.io/badge/Groq-F55036)](https://groq.com)
[![Pinecone](https://img.shields.io/badge/Pinecone-000000)](https://www.pinecone.io)
[![Bootstrap 5](https://img.shields.io/badge/Bootstrap-5-7952B3?logo=bootstrap&logoColor=white)](https://getbootstrap.com)

A FastAPI web app that lists your subscribed tenders, and for the ones you select: downloads the documents,
unpacks every zip (nested zips too), reads each file, indexes all of them in the tender's own Pinecone namespace
(`tcno-<tcno>`), retrieves the passages on each kind of requirement from it, and checks the company's eligibility
with the LLM pipeline in [ext.py](ext.py). Tenders are processed one at a time in the background while the page shows
live progress. Each row names the criteria a tender fails or could not determine, and the team can mark a
tender shortlisted or dropped. The company profile the tenders are checked against is kept as markdown in
[company_details.md](company_details.md) and is edited on the page as a form (**Edit profile** in the app bar).

## Project structure

```
app.py                       Application factory (create_app) and uvicorn entry point
worker.py                    A worker process: processes queued tenders from the job in Redis (REDIS_URL)
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
  pinecone_service.py        One Pinecone namespace per tender (tcno-<tcno>) in one shared index: create, check,
                             chunk, embed, batch upsert, query, retrieve, read back everything, delete, rebuild
  analysis_service.py        process_tender_files(tcno_folder, index=...) reads every source, indexes them in the
                             tender's namespace, retrieves the criteria and returns {"verdict", "details", "score",
                             "criteria", ...}; recheck_saved_criteria(tcno, index=...) retrieves and checks again
  ocr_service.py             OCR of scans, PDF pages with no text layer and large images: PaddleOCR locally first;
                             unsure and table pages go to OCR_MODEL
  cache_service.py           SQLite cache of file texts (by SHA-256) and chunk vectors, so nothing is read or
                             embedded twice
  company_service.py         Company profile markdown <-> the editor's form blocks; company name; the table of
                             criteria statuses marked by hand (record_mark(), profile_for_tender())
  processing_service.py      The job: queues tenders, runs them on the workers (threads of the app, worker.py
                             processes), tracks progress and results; saves each result to
                             eligibility_result.json and loads those at startup
  job_state.py               The job, its items and the results in Redis (an in-process one without REDIS_URL)
  decision_service.py        Shortlisted / dropped decisions, saved to decisions.json
  deep_analysis_service.py   Deep analysis: five agents review a completed result, the criteria retrieved from
                             its Pinecone namespace, the company profile and the statuses marked by user; saved to
                             deep_analysis.json
  profile_gap_service.py     The monthly profile-gap report: the month's Cannot determine criteria grouped by the
                             fact they lacked, with whether the profile states it (no LLM call)
scripts/
  migrate_to_pinecone.py     One-off: indexes the tenders already downloaded and removes eligibility_criteria.md
docs/
  pinecone-architecture.md   The Pinecone design (a namespace per tender): diagram, flows, migration plan, risks
utils/
  files.py                   Safe folder/file names, file name from a download response
  logging_setup.py           Console + rotating file logging (logs/app.log)
templates/
  base.html                  Layout and app bar; IBM Plex, Bootstrap 5 (modal, toasts) and Bootstrap Icons
  index.html                 Verdict tabs, run panel, tender table, selection bar, details modal, company profile
                             editor, help modal (the ? in the app bar); Tom Select for the searchable organisation filter
static/
  css/style.css              The visual system from design/: tokens, table grid, pills, responsive cards
  js/app.js                  Vanilla JS: loading, tabs/search/sort, selection, polling, decisions, modal
design/
  Tender Processing.dc.html  Source design (Claude Design file); open via a local web server to view
test_app.py                  Tests for the API, download/extract, indexing, verdict and score parsing
test_ext.py                  Existing tests for ext.py
test_pinecone_service.py     Tests for the Pinecone service
test_profile_gap_service.py  Tests for the profile-gap report
pinecone_fake.py             In-memory stand-in for the Pinecone client, used by the tests
```

## Setup

Python 3.10 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The first scanned PDF downloads PaddleOCR's models (about 30 MB) to `~/.paddlex/official_models`. If that fails
with "No available model hosting platforms detected", set `PADDLE_PDX_MODEL_SOURCE=modelscope` (or `huggingface`,
`aistudio`, `bos`) in `.env`.

Copy [.env.example](.env.example) to `.env` and put your TenderTiger login, Groq API key and Pinecone API key in
it; the other settings in it are optional (see [Configuration](#configuration)). The keys are read from `.env` (or
the environment) only, never from code:

```powershell
Copy-Item .env.example .env
```

```
TENDERTIGER_EMAIL=you@example.com
TENDERTIGER_PASSWORD=your-password
GROQ_API_KEY=your-key
PINECONE_API_KEY=your-pinecone-key
PINECONE_INDEX_NAME=tender-processing
PINECONE_NAMESPACE_PREFIX=tcno-
```

If the tenders were checked by a version before Pinecone, run the migration once (see
[Migrating from eligibility_criteria.md](#migrating-from-eligibility_criteriamd)).

The app logs in to TenderTiger on first use and keeps the session, logging in again when the site ends it.
The tender list shows the live tenders of the search in `TENDER_SEARCH` in [ext.py](ext.py) (keyword "india");
change it there. The list has pages of 20 tenders, or 50, 100 or 500 as chosen in **Per page** under the table
(remembered by the browser); TenderTiger sends any of them in one request. **Previous** and **Next** move between
pages, with a selection kept from page to page. The **Value** and **Closes** headers have
TenderTiger sort the whole list (closing soonest first by default) and start again from page 1. TenderTiger gives no total, so the last page is
the first one that is not full (or, when the last is exactly full, Next finds an empty page and says so). **Filters** in the toolbar narrows that search with the filters of TenderTiger's
own search page (word, organisation, location, TID, company industry, quantity, value, EMD, publish and closing
dates, GeM, NCB / ICB, funding, MSME, bid documents, startups): TenderTiger applies them, and the browser
remembers the last ones applied. Without the Groq key or the Pinecone key the tender list still loads, but every
tender fails at the analysis step with a clear message.

Beside the organisation filter, two filters narrow the page shown: the decision (**Not dropped**,
**Shortlisted**, **No decision**, **Dropped**) and the closing date (**Open**,
**Closes in 7 days**, today included, and **Closed**; a tender without a closing date counts as open). The verdict
tabs count only the tenders these two filters leave, so with **Not dropped** the dropped tenders are left out of the
counts as well as the table. **Show all tenders**, when nothing matches, clears every filter of the page.

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

**Run one web process.** The tender list, the deep analyses and (without `REDIS_URL`) the job live in its memory,
so do not use uvicorn's `--workers`, `gunicorn -w 4` or `--reload`. The endpoints run in uvicorn's thread pool, so
requests are still served while a job runs. Each tender's last result is saved as `eligibility_result.json` in its
folder and loaded again at startup, and its Pinecone namespace is kept, so it can be re-checked quickly; the
downloaded files stay in `downloads/`, and shortlisted / dropped decisions stay in `decisions.json`.

**Several workers.** The processing job (the queue, each tender's status and stage) and the results are kept in
Redis ([job_state.py](services/job_state.py)), so tenders can be processed by several workers at once. Without
`REDIS_URL` an in-process Redis stands in: the web app processes `JOB_WORKERS` tenders at a time (default 1) and
the job is lost on restart, as before. With `REDIS_URL` (a Redis database: `redis://default:<password>@<host>:<port>`,
or `rediss://` for TLS; not a Redis Cloud account API key), the job survives a restart of the web app, and worker
processes can take part:

```powershell
python worker.py --threads 2     # in another terminal, or on another machine sharing TARGET_DIRECTORY
```

Each worker takes the oldest queued tender, processes it and records the result in Redis and in its folder.
Workers need the same `.env` as the web app and the same `TARGET_DIRECTORY` (the same machine, or a shared folder):
the documents, results and OCR cache are there. Set `JOB_WORKERS=0` to leave all the processing to workers; the
run panel then says "Waiting for a worker…" until one starts. A worker keeps a lease on the tender it processes,
renewed every 22 seconds; a worker that dies leaves its tender failed ("Processing stopped unexpectedly") once the
lease runs out after 90 seconds. Ctrl+C stops a worker after the tenders it is on. Each worker logs to
`LOG_DIRECTORY/worker-<pid>.log`. Every change is made under one Redis lock, held for milliseconds.

Several workers on one machine share its CPU: PaddleOCR already uses every core for one page, so OCR is no faster
with more of them (see `OCR_CONCURRENCY`), and each worker that OCRs loads its own models (about 1 GB). Workers
pay off on the rest: downloads, embedding, Pinecone and the LLM calls, and on machines of their own.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TENDERTIGER_EMAIL` | (none) | TenderTiger login; the tender list, documents and pre-qualification use it |
| `TENDERTIGER_PASSWORD` | (none) | Password of that login |
| `TENDERTIGER_REQUEST_GAP` | `3` | Seconds between two requests to TenderTiger; its firewall blocks bursts with a browser check |
| `GROQ_API_KEY` | (none) | Groq key used by the LLM analysis ([console.groq.com/keys](https://console.groq.com/keys)) |
| `PINECONE_API_KEY` | (none) | Pinecone key ([app.pinecone.io](https://app.pinecone.io)); every tender's documents are indexed with it |
| `PINECONE_INDEX_NAME` | `tender-processing` | The one shared index (lowercase letters, digits and `-`), created on first use |
| `PINECONE_NAMESPACE_PREFIX` | `tcno-` | Each tender's namespace is prefix + TCNO: `tcno-12345678` |
| `PINECONE_CLOUD`, `PINECONE_REGION` | `aws`, `us-east-1` | Where the index is created (serverless); the Starter plan allows only `aws` / `us-east-1` |
| `PINECONE_EMBED_MODEL` | `llama-text-embed-v2` | Pinecone-hosted embedding model; no other key is needed |
| `PINECONE_EMBED_DIMENSION` | `1024` | Its dimension (384, 512, 768, 1024 or 2048). Changing the model or dimension needs a new `PINECONE_INDEX_NAME` and every namespace rebuilt |
| `PINECONE_UPSERT_BATCH` | `50` | Vectors per upsert request (Pinecone caps a request at 2 MB) |
| `CHUNK_SIZE`, `CHUNK_OVERLAP` | `1600`, `200` | Characters per chunk, and how many repeat the end of the chunk before |
| `RETRIEVAL_TOP_K` | `8` | Chunks each retrieval query returns |
| `RETRIEVAL_MAX_CHARS` | `60000` | The most document text sent to the LLM for one check or deep analysis (about 13k tokens). Groq's free tier refuses a request over 8,000 tokens per minute (error 413), and it counts the expected answer too, which for an eligibility check can be 7k tokens on its own: lowering this does not make the free tier enough |
| `INGEST_WORKERS` | `4` | Files read, and chunk batches embedded and upserted, at the same time |
| `REDIS_URL` | (none) | The Redis database the job and results are kept in, shared with worker processes (see [Several workers](#run)); unset, an in-process Redis is used |
| `REDIS_PREFIX` | `tender-processing:` | Put before every Redis key, so one Redis can serve several installs |
| `JOB_WORKERS` | `1` | Tenders the web app processes at the same time itself; `0` leaves them to `worker.py` processes (needs `REDIS_URL`) |
| `OCR_CONCURRENCY` | `8` | Scanned pages and images sent to `OCR_MODEL` at the same time in a process, while PaddleOCR reads on, one page at a time |
| `SUMMARY_MODEL` | `openai/gpt-oss-20b` | No longer used by the pipeline: the documents are indexed instead of summarised |
| `ASSESSMENT_MODEL` | `openai/gpt-oss-120b` | Groq model that checks the company against the criteria, also on a re-check |
| `OCR_MODEL` | `qwen/qwen3.8-27b` | Groq model that reads the pages of scanned PDFs that PaddleOCR is unsure of or that hold a table; it must accept images (vision) |
| `PADDLEOCR_MODEL` | `PP-OCRv6_small` | PaddleOCR models that read scanned pages locally (`<name>_det` and `<name>_rec`). `PP-OCRv6_tiny` is about three times faster on a CPU but makes more mistakes |
| `OCR_MIN_CONFIDENCE` | `0.9` | A scanned page PaddleOCR is less sure of (its mean score per character, 0 to 1) is sent to `OCR_MODEL`; raise it to send more pages |
| `DEEP_PROFILER_MODEL` | `ASSESSMENT_MODEL` | Groq model of the deep analysis's Company Profiler agent |
| `DEEP_AUDIT_MODEL` | `ASSESSMENT_MODEL` | Groq model of its Eligibility Audit agent, also for re-examinations |
| `DEEP_STATUS_REVIEW_MODEL` | `ASSESSMENT_MODEL` | Groq model of its Discrepancy & Status Review agent |
| `DEEP_SUPERVISOR_MODEL` | `ASSESSMENT_MODEL` | Groq model of its Supervisor / Judge agent |
| `LLM_MAX_OUTPUT_TOKENS` | `16384` | Most tokens a model may write in one reply, reasoning included. Without it Groq stops replies at 3,072 tokens, too few for a full eligibility check; a reply cut off at this limit fails the check rather than giving a partial result |
| `TARGET_DIRECTORY` | `./downloads` | Documents are stored in `TARGET_DIRECTORY/<tcno>/` |
| `DOWNLOAD_TIMEOUT` | `120` | Seconds to wait for the document server |
| `MAX_DOWNLOAD_MB` | `500` | Larger downloads are aborted |
| `OCR_BUDGET_PAGES` | `50` | The most pages and images a tender's scans may need OCR for before the team is asked (files in the cache need none); 0 is no limit. See [OCR budget](#ocr-budget) |
| `OCR_BUDGET_USD` | `0.5` | The most that OCR could cost a tender, in US dollars, every page at `OCR_PAGE_COST`, before the team is asked; 0 is no limit |
| `OCR_PAGE_COST` | `0.0056` | The most one page sent to `OCR_MODEL` costs, in US dollars (2,644 input and 882 output tokens at qwen3.8-27b's $0.80 / $4.00 per million); update it when the model or its price changes |
| `MAX_FILE_CHARS` | `300000` | A tender file with more text is not indexed and is listed as not checked. A scanned PDF is judged from its page count before OCR, at about 2,000 characters a page (150 pages at the default), and its OCR stops at the page where the text passes the limit |
| `DECISIONS_FILE` | `TARGET_DIRECTORY/decisions.json` | Where shortlisted / dropped decisions are saved |
| `CACHE_FILE` | `TARGET_DIRECTORY/cache.sqlite3` | The texts read from tender files (by SHA-256) and their chunks' vectors, so regenerating reads and embeds only changed files; delete it to start afresh |
| `COMPANY_DETAILS_FILE` | `./company_details.md` | The company profile; edited on the page or by hand |
| `DEFAULT_FILTERS_FILE` | `./default_filters.json` | The TenderTiger filters that **Default filters** applies; edited by hand (see `GET /api/default-filters`) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, ... |
| `LOG_DIRECTORY` | `./logs` | `app.log` is written here, rotated at 5 MB |
| `PORT` | `5000` | Port for `python app.py` |

## Company profile

Every tender is checked against the markdown in `COMPANY_DETAILS_FILE` (`company_details.md`), and only
against that: a criterion whose company figure is missing becomes Cannot determine. Whether a project counts as
similar work is judged per tender, from what the project was against the tender's definition, so a "Similar Work:
Yes" label in the profile is ignored. Click **Edit profile** in
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

A criterion status changed by hand in the analysis view is also recorded in the profile, in a **Criteria marked
by the bid team** table (Criterion | Requirement | Status | TCNO | Marked on) at its end, one row per tender and
criterion: marking it again replaces the row, and setting it back to the AI's status removes it (and the section
with its last row). The model takes a row's status for a criterion of another tender with the same requirement,
and says so. A tender is always checked, and deep-analysed, without its own rows: its marks are carried over to
its new result by the app, so the model never returns them as its own finding.

**Standing declarations and certificates** is the section for what holds on every tender: the land border
declaration (GFR 144(xi)), the Make in India supplier class and local content, labour-code compliance, GST, PAN,
Udyam, ISO and BIS certificates, local offices and OEM authorisations, with a **Valid until** column (the deep
analysis flags a date there that has passed or is close). Leave a value empty until it is confirmed: the check
then marks the criterion Cannot determine instead of guessing, and the profile-gap report lists it as empty.

### Profile-gap report

**Download this month's profile-gap report** (in the profile editor, or `GET /api/profile-gaps/file`) gives the
month's checks' Cannot determine criteria grouped by the fact they lacked, most frequent first: land border
declaration, Make in India and local content, blacklisting and integrity, labour, GST/PAN/Udyam, ISO/BIS, local
office, OEM authorisation, registration with the buyer, financial figures, experience, and the reasons no profile
entry can fix (EMD, the OEM's own figures, the bid's offer and documents, an estimated cost the documents do not
give). Each reason shows how many criteria and tenders it cost, its most frequent criteria, and whether the profile
states that fact now: **Stated** (re-check the tenders checked before it was added), **Empty**, **Not in the
profile**, or n/a. It counts each tender's latest completed check processed in the month (UTC), uses the status the
model gave even where the bid team has marked it since, and finds the reasons by keywords (`TOPICS` in
[profile_gap_service.py](services/profile_gap_service.py)), with no LLM call.

## How a tender is processed

1. The browser posts the selected TCNOs. The server looks each one up in the cached tender list, so the
   browser never sees or sends TenderTiger's internal IDs. If a run is in progress, the tenders are added
   to its queue.
2. `TARGET_DIRECTORY/<tcno>/` is emptied, so a re-run never mixes old and new files. (A tender whose Pinecone
   index exists is re-checked instead and skips steps 2 to 4; see Re-checking below.)
3. The documents are downloaded with the logged-in TenderTiger session, the way the tender page's Download
   All button does it: open the tender's detail page (`Tenderinformation`; without it the site reports no
   documents), ask `DownloadAllFiles` with the TCNO, then fetch the zip with `mode=download`. An ended
   session is logged in again once. The zip is saved in a staging folder and extracted with
   `extract_zip_recursive()` into the TCNO folder; an inner `a.zip` becomes the folder `a/`. A file that is
   not a zip (checked by content, not name) is moved into the TCNO folder. A tender whose `isdocument` is not
   `1` skips this step and is checked on its pre-qualification criteria alone.
4. `process_tender_files()` reads the company profile and fetches the tender's `GetPreQualification` page:
   the text the page passes to `displayText($(".contentPreQua"), "...")` is indexed as written, under the
   source name `TenderTiger Pre-Qualification` (many tenders have none), beside `TenderTiger Tender Data` (the
   tender's name, reference, organisation, dates and the value TenderTiger lists). It then converts
   each file to text with MarkItDown, `INGEST_WORKERS` files at a time: PDF, Word `.docx` (including `.doc` files saved in the `.docx` format),
   PowerPoint `.pptx`, and Excel `.xlsx` and `.xls`, each sheet as a table. The older Word and PowerPoint 97-2003
   formats, `.doc` and `.ppt`, cannot be read and are listed as unreadable. A PDF MarkItDown gets no text from,
   such as a scan, is OCR'd page by page ([ocr_service.py](services/ocr_service.py)). So is what the text layer
   of any other PDF leaves out, added after its text as `## Page <n>` and `## Page <n>, image <k>` sections:
   - a page with no text layer, scanned or drawn as vector graphics, is OCR'd whole, not read as its logo;
   - on a page with text, an image at least 300 px on each side, both in its own pixels and as drawn at 200 DPI
     (a scanned form pasted into an RFP), unless at least 20 words of the text layer lie inside it (a scan that
     kept its OCR'd text). Logos, stamps and signatures are smaller; the TenderTiger watermark is drawn across
     the page but is only 350 x 166 pixels.

   Each page or image is rendered at 200 DPI and read on this machine with
   [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) (`PADDLEOCR_MODEL`), which costs no tokens. Only two
   kinds go to the vision model `OCR_MODEL` instead: one PaddleOCR is unsure of (mean score per character below
   `OCR_MIN_CONFIDENCE`), and one with a table (ruled lines crossing in at least 9 places, or 3 lines of text each
   in 3 or more separate pieces), which the model writes as a markdown table. Their tokens count towards the
   criteria step; the log says how many each read and why. Blank ones are left out. A scan with a page that cannot
   be read, or with no text, is listed as unreadable. A PDF with a text layer keeps its text and everything else
   OCR read, and is listed with only the pages or images that could not be read, such as
   `RFP.pdf (could not be read: page 132, image 1)`, so the verdict cannot be Eligible until someone checks them.
   If the link cannot be fetched,
   `TenderTiger Pre-Qualification` is listed with the files that could not be read. With no files and no
   pre-qualification criteria there is nothing to check, and the tender fails.

   Files with `drawing` in their name and `.csv` files (GeM item lists) are not read at all: they hold no
   criteria. A BOQ is still read, because it may give the estimated cost. A file whose text is longer than
   `MAX_FILE_CHARS` is not indexed either; it is listed as not checked, with its length, so the verdict cannot be
   Eligible. A scanned PDF is judged before OCR, from its page count at about 2,000 characters a page
   (`CHARS_PER_PAGE` in [ocr_service.py](services/ocr_service.py), the average of real tender scans), and a scan
   that passes that check is still OCR'd only up to the page where its text passes the limit, so no time or
   tokens go on pages that would be dropped with the file. It is listed with its page count and the estimate, or
   with the characters read and the page OCR stopped at. Raise the limit in `.env`, restart and regenerate the
   criteria to read it.

   **Indexing.** All the sources of the tender go into one Pinecone namespace of its own, `tcno-<tcno>`, in the
   shared index `PINECONE_INDEX_NAME` (serverless, cosine, `PINECONE_EMBED_DIMENSION`; created on first use). The
   namespace is made before the documents are downloaded (an earlier one of the tender is emptied), so a Pinecone
   plan with no room for another namespace fails the tender before TenderTiger is asked or any OCR is paid for.
   Each source is split into chunks of about
   `CHUNK_SIZE` characters that overlap by `CHUNK_OVERLAP`, the chunks are embedded with Pinecone's hosted
   `PINECONE_EMBED_MODEL` (96 per request) and upserted (`PINECONE_UPSERT_BATCH` per request), `INGEST_WORKERS`
   batches at a time, with retries on rate limits and server errors. Every chunk carries `tcno`, `source_file`,
   `document_type` (nit, boq, corrigendum, technical, form, prequalification, tender_data or other, from the file
   name), `chunk_id`, `chunk_index`, `created_at` and its `text`. Once searches see every chunk (Pinecone reports
   each write's log position and how far each search has caught up), a complete `__manifest__` record is written
   last (a long source list is split over `__manifest__-000`, ... parts written just before it): the sources with
   their chunk counts and the sources that could not be read. A namespace without it was left half-built; a failed
   run deletes it, and one left by a crash is rebuilt the next time the tender is checked. Files are read
   `INGEST_WORKERS` at a time. PaddleOCR reads one page at a time in a process (one engine already uses every
   core: 8 pages took 77 s in turn, 71 s on 8 threads and 77 s in 4 processes, and 8 processes ran out of memory),
   while the pages sent to `OCR_MODEL` go `OCR_CONCURRENCY` (8) at a time, all PDFs together, and PaddleOCR reads on
   meanwhile; the text keeps page order. Every read and write names the
   tender's namespace, so tenders never see each other's records.

   **Retrieval.** Before the LLM is called, the chunks on each kind of requirement are retrieved from the
   namespace:
   eligibility, financial, technical, experience, JV and compliance, two or three searches each
   (`ASSESSMENT_QUERIES` in [analysis_service.py](services/analysis_service.py)), `RETRIEVAL_TOP_K` chunks per
   search. The TenderTiger sources are always sent in full; the searches then take turns, best match first, until
   `RETRIEVAL_MAX_CHARS` of text is taken. The chunks go to the LLM in document order, as a `## <source>` section
   per source, and it checks the company against the criteria in them, listing a criterion stated in several
   sources once. A failed final call leaves the namespace in place, so a re-check can follow without reading the
   documents again.

   **Tender details.** The tender's row in the TenderTiger list (tender value, organisation, closing date, name,
   location) goes with the task as "Tender details", ahead of the company profile, taken from the list as it was
   when the check started, on a re-check too: a closing date moved since the documents were indexed is the one
   used, and the `TenderTiger Tender Data` copy in the namespace is left out of the excerpts. The model uses the
   tender value when a criterion depends on the estimated cost and the excerpts do not state one (a figure in the
   excerpts comes first), and counts periods such as "the last three financial years", and the validity of
   certificates, from the closing date unless the excerpts give another date. A value the list does not give is
   sent as "not listed".
5. The verdict is read from the model's "overall verdict" line: Eligible, Not Eligible, or Requires Review
   (for "Cannot determine", an unreadable answer, or no criteria found). An Eligible verdict becomes
   Requires Review if any file, or the pre-qualification criteria, could not be read, because the check was
   incomplete.
6. The rows of the criteria table (every markdown table with a Status column) become `criteria`, each with
   status `met`, `not_met` or `unknown`. The model is given fixed rules for each status (Met only when the
   profile states what the criterion needs; nothing inferred, such as the country from an address; exemptions
   applied to the criterion they relax; purchase preferences and payment terms left out), because without them
   the borderline criteria flipped between Met and Cannot determine from one re-check to the next. The tender table shows the names of the criteria not met or not
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

**Re-checking against the namespace.** When a tender's Pinecone namespace exists, checking it again skips steps
2 to 4: no download, no TenderTiger request, no reading. The criteria are retrieved from the namespace as above
and compared with the current company profile in one LLM call. The sources the namespace's manifest lists as
unreadable still make the check incomplete, so an Eligible verdict there still becomes Requires Review. A
namespace left half-built (no complete manifest) is not trusted: the tender's documents are downloaded and indexed
again. To read the documents again and rebuild the namespace, queue the tender with `"regenerate": true`. On the
page, that is the **Regenerate criteria** switch in the selection bar, or the button of that name in the analysis
view, next to **Re-check**. Regenerating deletes the namespace first, then empties the folder (step 2), downloads,
reads and indexes the documents again; if the download fails, the tender is left with no namespace, and the next
check reads the documents again.

**OCR budget.** Before any OCR, a tender's downloaded files are counted for the pages and images OCR would read
(files in the cache need none; see `ocr_estimate()` in [analysis_service.py](services/analysis_service.py)), and
what that could cost at most, every one sent to `OCR_MODEL` at `OCR_PAGE_COST`. Over `OCR_BUDGET_PAGES` or
`OCR_BUDGET_USD`, the tender stops there, before reading anything, and is saved with the status
`awaiting_approval` (not a failure: nothing went wrong), "Waiting for approval of the OCR" and its `ocr_estimate`
(`pages`, `cost`, `files`). The page lists it under its own **Needs approval** tab and run count, in a violet tone
of its own, with **Approve OCR**, which queues it again with `approve_ocr` (its documents are downloaded again). The cost is a
worst case: most scanned pages are read locally for nothing, and only unsure and table pages go to `OCR_MODEL`.
Before a bulk regenerate (the **Regenerate criteria** switch), the page asks `/api/ocr-estimate` and shows each
tender's pages and worst-case cost, counted from the documents already on disk, with **Regenerate** (tenders over
the budget stop and ask) or **Regenerate and approve OCR**.

**Cache.** Reading and embedding are cached in `CACHE_FILE` (SQLite, [cache_service.py](services/cache_service.py)),
so regenerating reads and embeds only the files that changed. A file's text, after MarkItDown and OCR, is kept by
the file's SHA-256 and the reading settings (the MarkItDown version, `PADDLEOCR_MODEL`, `OCR_MIN_CONFIDENCE`,
`OCR_MODEL`): a file downloaded again unchanged, or the same file in another tender, is not read or OCR'd again,
and spends no OCR tokens. Each chunk's vector is kept by the SHA-256 of the chunk's text and the embedding model
and dimension, so an unchanged file's chunks are never embedded twice, and changing `CHUNK_SIZE`,
`CHUNK_OVERLAP` or the model cannot serve a stale vector. The namespace is still rebuilt and every chunk upserted
again; only the embedding is skipped, and `index.embedding_tokens` counts only the chunks embedded. A reading
with pages or images OCR could not read is not kept, so the next run tries them again; nor is a PDF too long to
OCR. A cache that cannot be read or written is logged and passed over. Delete the file to read and embed
everything again.

**Namespace limits.** All tenders share one index, each in its own namespace, because Pinecone allows far more
namespaces per index (100 on Starter, 100,000 on Standard) than indexes per project (5 on Starter, 20 on
Standard). Once an index's namespaces run out, a new tender fails with "The Pinecone namespace limit is reached":
delete the namespaces of tenders no longer needed with `DELETE /api/namespace/<tcno>` (their results are kept;
checking them again rebuilds the namespace), or move to a larger plan. See
[docs/pinecone-architecture.md](docs/pinecone-architecture.md).

Expect a minute or two per tender whose documents are read: the documents are no longer summarised by the LLM, so
the time goes on the download, scanned PDFs (OCR: PaddleOCR takes 5 to 15 seconds a page on a CPU, so a long scan
adds minutes), creating the index (10 to 30 seconds) and embedding. A re-check
takes one LLM call. A download or analysis error marks only that tender as failed, with a plain-language
`error_title` and `error_action`; the job moves on. The exception is TenderTiger's firewall blocking the app:
then the queued tenders that need TenderTiger are cancelled, and only re-checks against an index go on. Retrying a failed tender reloads the tender list first.
Stopping a run cancels the queued tenders; the one in progress
finishes.

**Token usage.** Every LLM call a tender makes is counted, as the input and output tokens the model reports, in
two steps: `criteria`, reading the documents (now only the scanned pages sent to `OCR_MODEL`), and `assessment`, the final check
that gives the result. The counts are saved with the result as `tokens`, and logged. A failed tender keeps the
tokens it spent before failing; a re-check spends none on `criteria`. Embedding tokens are billed by Pinecone, not
counted here; the result's `index.embedding_tokens` gives them for the run that built the index.

**Saved results.** Every finished check, failed ones included, is saved as `eligibility_result.json` in the TCNO
folder, replacing the previous one; it holds the same fields as `GET /api/result/<tcno>`. On startup the app
loads every such file, so the table, the analysis view and the time estimates come back after a restart; a file
it cannot read is skipped with a warning in the log. A result saved by an earlier version loses its old
`criteria_file` (or `index_name`) field on loading and gets `namespace`. Regenerating empties the folder, so the old result is gone
until the new one is saved.

## Deep analysis

A second opinion on a checked tender. In the analysis view, **Run deep analysis** starts five agents that work
in turn on one shared state, each reading what the agents before it wrote. It reviews the eligibility criteria
retrieved from the tender's Pinecone namespace, the company profile, the existing result and the statuses marked by
user, and never changes the result or its verdict.

The criteria come straight from the index: the excerpts found for Eligibility Criteria, Financial Requirements,
Technical Requirements, Experience Requirements and JV Requirements, and, as Supporting Evidence, the excerpts
nearest each criterion of the result. Each excerpt is given once, numbered `[E1]`, `[E2]`, ..., followed by which
excerpts answer each kind of requirement and each criterion. Without an index (deleted, or never built), the
agents work from the requirements the result recorded, and the report says so in `inputs.warnings`.

| Agent | Role | What it adds |
|---|---|---|
| Ingestion & Context | Input parsing (no LLM) | Validates the result and the criteria retrieved from the index, normalises money to rupees and crore (`₹ 12.5 Cr`, `3 lakh`, bare figures in turnover / value / net worth fields), dates to ISO and `FY24` to its April–March dates, and flags validity dates that have passed and year-wise tables older than the last financial year to have ended |
| Company Profiler | Data verification | Flags company data that is missing, outdated, inconsistent with other figures or expired, above all what the criteria need. No external registry is connected: each flag names the source that would settle it (MCA, GST or Udyam portal) instead of claiming a lookup |
| Eligibility Audit | Criteria assessment | A Pass / Fail / Insufficient data / Not applicable matrix, one rule per criterion, judged afresh from the criteria and the company data. It is never shown the earlier statuses, so it cannot lean on them |
| Discrepancy & Status Review | User action bias correction | Compares every status marked by user (and unread files marked checked, and the shortlist decision) with the audit and the profiler, rates the confirmation bias risk, and says why someone might have marked it so (knowledge outside the profile, a relationship with the client, a relaxation read into the text). Skipped, with no LLM call, when nothing was marked and the audit agrees with every AI status |
| Supervisor / Judge | Final consensus | Resolves the disagreements and gives every criterion's final status and confidence, the determination, and the data gaps. It may send up to 6 disputed criteria back to the audit agent, with the question to settle, for up to 2 re-examination rounds |

The determination follows the final statuses as a verdict does (Not Eligible if any is not met, Eligible if all
are met and every source was read or checked, otherwise Requires Review). Where the supervisor states a different
determination, the agents have not settled it, so it is Requires Review. An agent's answer that is not valid JSON
is sent back once to be rewritten; a second bad answer fails the run.

The report is saved as `deep_analysis.json` in the TCNO folder, beside `eligibility_result.json`, and survives a
restart. It is never served or read as a tender document. It records what it was based on: once the result's
statuses, the unread files checked, or the company profile change, it is shown as out of date until it is run
again. Re-checking keeps the old report (marked out of date); regenerating empties the folder and removes it. A
failed run leaves the last report as it was. One deep analysis runs at a time; others wait their turn. A typical
run makes 4 to 8 LLM calls, counted by agent under `tokens`.

Each LLM agent runs on its own model, set in `.env` (`DEEP_PROFILER_MODEL`, `DEEP_AUDIT_MODEL`,
`DEEP_STATUS_REVIEW_MODEL`, `DEEP_SUPERVISOR_MODEL`; see Configuration), so a cheaper model can check the profile
while a stronger one audits and judges. Each defaults to `ASSESSMENT_MODEL`; the Ingestion & Context agent uses
none. The report records each agent's model under `models`, and each audit log step the model it ran on.

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
      "has_indexed_criteria": true,
      "result": {
        "status": "completed", "verdict": "Not Eligible", "score": 33,
        "error": null, "error_title": null, "error_action": null,
        "checks": {"total": 6, "met": 2, "not_met": ["Annual turnover", "Bid capacity"],
                   "unknown": ["Solvency", "Key personnel"], "unreadable_files": 0,
                   "jv": {"allowed": "yes", "max_members": 3, "verdict": "Not Eligible",
                          "lead": {"total": 2, "met": 1, "not_met": ["Annual turnover"], "unknown": []}}},
        "processed_at": "2026-11-02T07:05:12+00:00", "rechecked": false,
        "namespace": "tcno-100091540"
      },
      "decision": {"decision": "dropped", "decided_at": "2026-11-02T08:10:00+00:00"}
    }
  ]
}
```

The dates come from TenderTiger as `DD-MM-YYYY` and are shown as `18-Nov-2026`; `days_left` counts from
today to the closing date. `has_document` is false when the tender's `isdocument` is not `1`; such a tender
can still be checked, on its pre-qualification criteria alone, while `has_prequalification` (it has the IDs
for a `GetPreQualification` link) is true. `has_indexed_criteria` is true when the tender's Pinecone namespace
(`tcno-<tcno>`) exists, so a check re-uses it; it replaces `has_saved_criteria`. It is false
when Pinecone is not configured or cannot be reached, so the list still loads. `502` if the tender API or the
TenderTiger login fails.

### GET /api/default-filters

The filters in `DEFAULT_FILTERS_FILE` (`default_filters.json`), which **Default filters** under the page's Filters
puts into the form and loads the list with. The file holds one JSON object with the parameter names of
`GET /api/tenders`; a date may also be `"today"`, `"today+7"` or `"today-7"` (days), so the file does not go stale.
The one shipped lists the open tenders that have bid documents:

```json
{
  "closing_from": "today",
  "bid_doc": true
}
```

They are returned as the query parameters the form sends: strings, only the filters set (a `false` flag and a
comparison without its amount are left out), dates resolved, an amount always with its comparison:
`{"closing_from": "2026-11-11", "bid_doc": "true"}`. `404` without the file; `500` when it is not valid JSON or
holds an unknown filter or a bad value, naming it.

### POST /api/process-tenders

Request:

```json
{"tcnos": ["100091540", "102309372"], "regenerate": false, "approve_ocr": false}
```

Response `202`: the job's progress (same shape as `/api/progress`). Starts a job, or adds the tenders to
the running one; tenders already queued or processing are skipped. A tender with a Pinecone namespace is
re-checked against it; `regenerate` (optional, default `false`) deletes the namespace of every posted tender,
downloads and reads its documents again and rebuilds the namespace. `approve_ocr` (optional, default `false`)
reads every scanned page even when the OCR is over the tender's budget (see [OCR budget](#ocr-budget)). `400` for
an empty list or an unknown TCNO.

### POST /api/ocr-estimate

What regenerating the posted tenders, `{"tcnos": ["100091540", "102309372"]}`, would cost in OCR, counted from
the documents already downloaded, without reading or queuing anything. The page asks for it before a bulk
regenerate. Response `200`:

```json
{
  "tenders": [
    {"tcno": "100091540", "downloaded": true, "pages": 12, "cost": 0.0672,
     "files": [{"name": "scan.pdf", "pages": 12}], "over_budget": false},
    {"tcno": "102309372", "downloaded": false, "pages": 0, "cost": 0.0, "files": [], "over_budget": false}
  ],
  "pages": 12, "cost": 0.0672, "over_budget": 0,
  "budget": {"pages": 50, "usd": 0.5}, "page_cost": 0.0056
}
```

`pages` counts the pages and images OCR would read (files in the cache need none); `cost` is the most they could
cost in US dollars, every one at `page_cost`; `downloaded` is `false` for a tender with no documents on disk yet,
counted after its download; `over_budget` says the run will stop at that tender and ask.

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
  "awaiting_approval": 0,
  "cancelled": 0,
  "current": {
    "tcno": "100091540",
    "name": "Providing Project General Management Services ...",
    "stage": "Analysing file 2 of 5: NIT/Annexure-II.pdf",
    "step": 2,
    "steps": 6,
    "elapsed_seconds": 74
  },
  "active": [
    {"tcno": "100091540", "name": "Providing Project General Management Services ...",
     "stage": "Analysing file 2 of 5: NIT/Annexure-II.pdf", "step": 2, "steps": 6, "elapsed_seconds": 74}
  ],
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

`status` is `idle` (nothing has run since the server started), `running` or `completed`. `active` lists every
tender being processed (several, with several workers), and `current` is the first of them, or `null`.
Item `status` is `queued`, `processing`, `completed`, `failed`, `awaiting_approval` (stopped before any OCR, its
scans over the [OCR budget](#ocr-budget); counted in `awaiting_approval`, not `failed`) or `cancelled`; finished
items carry the same fields as `result` in `/api/tenders`. `step` / `steps` count each file plus the final check (null while
downloading; 1 of 1 for a re-check; the step before the last is indexing). An item's `rechecked` says whether it
is (or was) checked against its Pinecone namespace. `average_seconds` is the average time of the tenders whose documents were read (by this server or,
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
  "namespace": "tcno-100091540",
  "index": {"index": "tender-processing", "namespace": "tcno-100091540", "sources": 4, "chunks": 312,
            "embedding_tokens": 118230, "retrieved_chunks": 31, "retrieved_characters": 47210},
  "tokens": {
    "criteria": {"input": 312480, "output": 18342, "total": 330822},
    "assessment": {"input": 9120, "output": 2654, "total": 11774},
    "total": {"input": 321600, "output": 20996, "total": 342596}
  },
  "duration_seconds": 412.6,
  "processed_at": "2026-11-02T07:05:12+00:00"
}
```

A failed tender has `"status": "failed"`, `verdict` and `details` set to `null`, the raw reason in `error`,
and what failed and what to do next in `error_title` and `error_action`. `rechecked` is true when the result
came from the tender's Pinecone namespace without reading the documents; `namespace` is that namespace after the
run, or `null` when it has none (it replaces `criteria_file`). `index` says what the run indexed (`sources`, `chunks`,
`embedding_tokens`; absent on a re-check) and retrieved (`retrieved_chunks`, `retrieved_characters`); `null` for a
failed run.
`tokens` holds the LLM tokens of the run (see Token usage above); results saved before it was added have none.
`jv.allowed` is `yes`, `no` or `unknown` (not stated); `max_members` is `null` when not stated or no JV is
allowed; `jv.verdict` is the outcome as JV lead member (see step 8), `null` without a JV. In `comparison`,
`alone_requirement` and `alone_status` are `null` for a requirement only a JV has, and the `lead_*`,
`other_members` and `combined` fields are `null` when no JV may bid. Statuses are `met`, `not_met` or `unknown`.
A failed tender has `"jv": null` and an empty `comparison`.
`404` if the tender has no result: not processed since the server started, and no saved result in its folder.

### GET /api/files/&lt;tcno&gt;/&lt;file&gt;

Returns one of the tender's downloaded documents, `file` being its path as the result's `files` list it (for
example `102418294/doc6255363863059291992.pdf`). The analysis view links every file in its file list, and each
unreadable file, to this, in a new tab. PDFs, images and plain text open in the browser; any other type (Word,
Excel, a web page) is downloaded, so nothing in a tender's files runs as part of the app. Only a listed document
is served: `404` for any other name, including the saved result and deep analysis, or once the documents were
downloaded again.

### PUT /api/result/&lt;tcno&gt;/criteria/&lt;index&gt;

Sets the status of one criterion by hand, when the AI got it wrong; `index` is its position in `criteria`. The
analysis view does this from the status next to each criterion.

```json
{"status": "met"}
```

`status` is `"met"`, `"not_met"`, `"unknown"` or `null` (back to the AI's status, as is sending the AI's own).
The criterion gets `"marked_by_user": true` and keeps the AI's status as `ai_status`. While any criterion is
marked (or the unreadable files are checked, below), the verdict follows the statuses: Not Eligible if one is
not met, Eligible if all are met and no unreadable file is left unchecked, otherwise Requires Review. The AI's
verdict is kept as `ai_verdict`; the score, `checks` (with `user_marked`, the number marked) and each comparison
row's `alone_status` follow too. `details` stays the AI's text. The result is saved, so the marks survive a
restart, and a re-check or regenerate marks the same statuses again on the criteria of the new result with the
same name. The mark is also recorded in the company profile, or removed from it with the mark (see [Company
profile](#company-profile)); a profile that cannot be saved is logged, and the mark stands. Response `200` with
the updated result. `400` for an unknown status, `404` without a completed
result or such a criterion, `409` while the tender is queued or being checked.

### PUT /api/result/&lt;tcno&gt;/unreadable-checked

Records that the bid team checked the files the AI could not read (`unreadable_files`) by hand, so they no
longer keep the verdict from Eligible: a tender meeting every criterion becomes Eligible. The analysis view
does this with **I have checked these files** under those files, tagged "Checked by user" with an Undo.

```json
{"checked": true}
```

`false` takes it back. The result gets `unreadable_checked` (the files checked) and `checks.unreadable_checked`
(their number), and the verdict and the verdict as JV lead member are worked out again as above. A re-check or
regenerate keeps the check for the files that are still unreadable. Response `200` with the updated result;
`400` without `checked`, `404` without a completed result or unreadable files, `409` while the tender is
queued or being checked.

### POST /api/deep-analysis/&lt;tcno&gt;

Starts a deep analysis of the tender's completed result (see Deep analysis above). No body. Response `202` with
its state, as `GET` returns it. `404` without a completed result; `409` while the tender is queued or being
checked, or while a deep analysis of it is already queued or running.

### GET /api/deep-analysis/&lt;tcno&gt;

The tender's deep analysis. The analysis view polls it every 2 seconds while one runs.

```json
{
  "tcno": "100091540",
  "status": "running",
  "progress": {"agent": "audit", "round": 1, "done": ["ingestion", "profiler", "status_review", "supervisor"],
               "stage": "Eligibility Audit agent: re-examining 1 disputed criterion for the supervisor (round 1)",
               "queued_at": "...", "started_at": "...", "elapsed_seconds": 74},
  "error": null, "error_title": null, "error_action": null,
  "analysis": null,
  "stale": false
}
```

`status` is `none`, `queued`, `running`, `completed` or `failed` (then with `error`, `error_title` and
`error_action`). `progress` is `null` unless queued or running. `analysis` is the last saved report, also after a
failed run; `stale` is true when the result or the company profile changed since it was made. The report:

```json
{
  "version": 3, "tcno": "100091540", "status": "completed",
  "models": {"profiler": "gpt-oss:120b-cloud", "audit": "gpt-oss:120b-cloud",
             "status_review": "gpt-oss:120b-cloud", "supervisor": "gpt-oss:120b-cloud"},
  "started_at": "...", "finished_at": "...", "duration_seconds": 184.2, "rounds": 1,
  "based_on": {"processed_at": "...", "verdict": "Eligible", "fingerprint": "9c1f0e2ab4d37c55"},
  "inputs": {"criteria_source": "Pinecone namespace tcno-100091540", "excerpts": 28, "criteria": 4, "user_marked": 2,
             "unreadable": ["a.pdf"], "unreadable_checked": ["a.pdf"], "decision": "shortlisted",
             "normalised_facts": 22, "warnings": []},
  "final": {"determination": "Requires Review", "confidence": 0.72,
            "summary": "Two criteria hang on values the documents do not give ...",
            "deciding_criteria": ["Experience Criteria"], "current_verdict": "Eligible",
            "agrees_with_current": false, "supervisor_determination": "Requires Review"},
  "criteria": [
    {"ref": 0, "criterion": "Experience Criteria", "requirement": "...", "source": "NIT.pdf",
     "current_status": "met", "ai_status": "unknown", "marked_by_user": true,
     "audit_result": "insufficient_data", "audit_reason": "...", "audit_evidence": "...", "audit_missing": "...",
     "final_status": "unknown", "confidence": 0.75, "basis": "...", "resolution": "...",
     "conflict": true, "bias_risk": "high"}
  ],
  "data_gaps": [{"gap": "Required experience value", "needed_for": "Experience Criteria",
                 "how_to_close": "Read the ATC document", "severity": "high"}],
  "profile_flags": [{"field": "Bid Capacity Assessment > A (Highest Annual Turnover)", "issue": "mismatch",
                     "detail": "...", "needed_for": "Bid capacity", "severity": "high",
                     "verify_with": "CA-certified turnover certificate", "source": "profiler"}],
  "status_review": {"reviews": [{"ref": 0, "criterion": "Experience Criteria", "marked_by_user": true,
                                 "user_status": "met", "ai_status": "unknown", "audit_result": "insufficient_data",
                                 "agreement": "disagrees", "bias_risk": "high", "finding": "...",
                                 "possible_reasons": ["..."], "recommendation": "verify"}],
                    "overrides": [{"kind": "unreadable_checked", "detail": "...", "bias_risk": "medium",
                                   "finding": "..."}],
                    "summary": "...", "skipped": false},
  "agents": {"ingestion": {}, "profiler": {}, "audit": {"rules": [], "summary": "..."},
             "status_review": {}, "supervisor": {}},
  "audit_log": [{"at": "...", "agent": "ingestion", "round": 0, "model": null,
                 "event": "4 criteria from the result ...", "seconds": 0.0}],
  "tokens": {"profiler": {"input": 2410, "output": 380, "total": 2790}, "audit": {}, "status_review": {},
             "supervisor": {}, "total": {}}
}
```

`criteria` lines up every view of each criterion of the result, by its index (`ref`), followed by any the audit
alone found (`ref` `null`, `current_status` `null`). Statuses are `met`, `not_met`, `unknown` or (final only)
`not_applicable`; audit results `pass`, `fail`, `insufficient_data` or `not_applicable`; `conflict` is true when
the AI, the mark by user and the audit disagree or the audit left the criterion out. `confidence` runs from 0 to
1, `null` when not given. `profile_flags` with `"source": "ingestion"` are the date checks of the ingestion agent.
`agents` holds each agent's own output, such as the audit's full rule matrix.

`inputs.criteria_source` is the Pinecone namespace the criteria came from, or `result` when the tender had none
and the agents worked from the requirements the result recorded; `inputs.excerpts` counts the excerpts retrieved.
Reports of version 2 named `eligibility_criteria.md` there.

### GET /api/deep-analysis/&lt;tcno&gt;/file

Downloads the saved report, `deep_analysis.json`, as `deep_analysis_<tcno>.json`. `404` without one.

### GET /api/closed-downloads

What **Closed tenders** in the app bar shows: the tenders whose closing date has passed but whose documents are
still in `TARGET_DIRECTORY`, longest closed first, and under `unknown` those with documents but no closing date on
record. A tender folder holding only its result is left out.

```json
{"directory": "C:\\Tender-Processing\\downloads",
 "closed": [{"tcno": "102048481", "name": "Supply of packaged drinking water", "closing_date": "02-Oct-2026",
             "days_closed": 40, "files": 3, "bytes": 2400000}],
 "unknown": [{"tcno": "91289333", "name": "Hiring of vehicles", "closing_date": null, "days_closed": null,
              "files": 8, "bytes": 31000000}]}
```

A result keeps the tender's closing date (`closing_date`, `YYYY-MM-DD`), refreshed each time the tender is listed,
since TenderTiger can move it. A result saved before closing dates were kept gets its date the first time it is
needed, from the TenderTiger data indexed in its Pinecone namespace; one whose namespace predates that is `unknown`
until the tender is listed again.

### DELETE /api/downloads/&lt;tcno&gt;

**Delete** in that popup: deletes everything kept for the tender, so it reads as never checked: its Pinecone
namespace, its folder (the downloaded documents, the saved result and the deep analysis), all Redis holds of it
(its result, and its place in the last run, which is forgotten too when it checked no other tender) and the team's
decision.
Only the statuses marked by hand stay, as they are recorded in the company profile. Checking the tender again
starts from scratch.

Response `200`: `{"tcno": "102048481", "files": 3, "bytes": 2400000, "namespace_deleted": true}`, counting the
documents. The namespace goes first, so when Pinecone fails (`502`) nothing else has been deleted yet. `404` when
nothing is kept for the tender, `409` while the tender is queued or being checked, while a deep analysis of it runs,
or when a file cannot be deleted (open in another program, say).

### GET /api/namespace/&lt;tcno&gt;

The tender's Pinecone namespace: whether it exists and is complete (has its complete manifest), and what it holds.

```json
{"tcno": "100091540", "index": "tender-processing", "namespace": "tcno-100091540", "exists": true,
 "complete": true, "chunks": 312, "vector_count": 313, "embed_model": "llama-text-embed-v2",
 "created_at": "2026-11-11T06:08:24+00:00", "unreadable": ["scan.pdf"],
 "sources": [{"name": "TenderTiger Tender Data", "type": "tender_data", "chunks": 1},
             {"name": "NIT/NIT_PGMS.pdf", "type": "nit", "chunks": 188}]}
```

`vector_count` counts the manifest too. `503` when `PINECONE_API_KEY` is not set, `502` when Pinecone fails.

### DELETE /api/namespace/&lt;tcno&gt;

Deletes the tender's Pinecone namespace, to free a place under the plan's namespace limit; other tenders'
namespaces are untouched. The result is kept, with `namespace` `null`; the next check downloads and indexes the
documents again. Response `200`: `{"tcno": "100091540", "namespace": "tcno-100091540", "deleted": true}` (`false`
when there was none). It takes a few seconds: the namespace is emptied first, and checked again after it is
deleted, as Pinecone can bring back a namespace written to just before. `409` while
the tender is queued or being checked, `503` / `502` as above.

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

### GET /api/profile-gaps

The profile-gap report of `?month=YYYY-MM` (the current month, UTC, by default):

```json
{"month": "2026-11", "label": "November 2026", "tenders": 29, "criteria": 289, "cannot_determine": 201,
 "reasons": [{"key": "land_border", "reason": "Land border declaration", "criteria": 16, "tenders": 16,
              "share": 8, "profile": "empty", "examples": ["Land Border Eligibility", "Land Border Restriction"]}],
 "generated_at": "2026-11-11T12:00:00+00:00"}
```

`profile` is `stated`, `empty` or `missing` for a fact the company profile can give, `null` for one each bid or the
tender documents must supply. `400` for a month not in the form `YYYY-MM`.

### GET /api/profile-gaps/file

The same report as a markdown download, `profile-gaps-<month>.md`.

## Migrating from eligibility_criteria.md

Versions before Pinecone saved each tender's criteria as `eligibility_criteria.md` in its folder. Those
files are no longer read. With the app stopped, index the tenders already downloaded:

```powershell
python scripts/migrate_to_pinecone.py --dry-run     # what would be done
python scripts/migrate_to_pinecone.py               # every tender folder, or name TCNOs to do only those
```

It reads the documents in each folder (OCR for scanned PDFs again), takes the TenderTiger pre-qualification text
from the old file so TenderTiger is not asked, builds the tender's namespace, and deletes `eligibility_criteria.md`
(`--keep-criteria-files` keeps them; a tender with no readable text keeps its file). Hand edits made to those
files are not carried over: only the documents and the pre-qualification text are indexed. Complete namespaces
are skipped unless `--force`. It stops at the plan's namespace limit; run it again after deleting namespaces or
upgrading. A tender not migrated is simply indexed the next time it
is checked (its documents are downloaded again).

## Tests

```powershell
python -m unittest test_app test_ext test_pinecone_service
```

The tests replace the tender API, the download, the LLM and Pinecone (with the in-memory
[pinecone_fake.py](pinecone_fake.py)) with fakes, so they run offline in a few seconds.

You are a QA test agent working in a Linux VM with a terminal. Test a CSV-contact-import web app end to end and write a test report.

RULES
- Lines between `<<'EOF'` and `EOF` must be copied exactly as shown, starting at the first column, and the `EOF` line must be alone on its line.
- Do NOT modify any file under app/, scripts/ or tests/. You are testing, not fixing.
- Run every command exactly as written. Paste the real output as evidence. Never guess or invent results.
- If a step fails, retry it once. If it still fails, mark the test FAIL, save the error output, and continue with the next test.
- Work in the repo folder unless a step says otherwise.

WHAT THE APP DOES (so you understand the results)
- It has three containers: `api` (web server + UI on port 8000), `worker` (processes imports) and `db` (Postgres).
- You upload a CSV. The file is streamed to disk and the import is queued.
- The worker then reads the file in 10,000-row chunks. For each chunk it validates the phone numbers (turning them into +E.164 format, e.g. +919876543210) and saves the rows to the `contacts` table in one transaction.
- The same phone number in the same campaign is saved only once. Later copies are counted as duplicates.
- Rows whose phone is missing or invalid go to the `import_errors` table with a reason: empty, invalid or sci_notation.
- For every import, these three counts must add up to the number of data rows: valid_rows + invalid_rows + duplicate_rows.

======================================================================
STEP 0 — SETUP
======================================================================
0.1  Check the tools. Each of these must print a version:
       docker --version && docker compose version && git --version && python3 --version && curl --version | head -1
     If Docker is missing, install Docker Engine plus the compose plugin for this OS, then check again.

0.2  Get the code:
       git clone https://github.com/huamanraj/larage-csv-upload.git && cd larage-csv-upload
       git checkout huamanraj/zealous-pascal-x3puuy

0.3  Create a compose override file so the crash test does not have to wait 10 minutes:
       cat > docker-compose.override.yml <<'EOF'
services:
  worker:
    environment:
      LOCK_TIMEOUT: "20 seconds"
EOF

0.4  Start the app and wait until it answers:
       docker compose up --build -d
       for i in $(seq 1 60); do curl -sf localhost:8000/api/config && break; sleep 2; done; echo
     PASS if JSON is printed that contains "chunk_size":10000.

0.5  Define these shell helpers. Re-define them if you open a new shell.
       q() { docker compose exec -T db psql -U postgres -d csvimport -Atc "$1"; }
       upload() { curl -s -X POST --data-binary @"$1" -H "X-File-Name: $(basename "$1")" "localhost:8000/api/imports?campaign_id=$2"; echo; }
       wait_done() { for i in $(seq 1 600); do s=$(q "select status from imports where id=$1"); [ "$s" = done ] || [ "$s" = failed ] && { echo "status=$s"; return; }; sleep 1; done; echo "TIMEOUT"; }
       counts() { q "select id, status, checkpoint_row as rows, valid_rows, invalid_rows, duplicate_rows, round(extract(epoch from finished_at-started_at)::numeric,2) as seconds, coalesce(error,'') from imports where id=$1"; }
       reset_db() { curl -s -X POST localhost:8000/api/reset; echo; }

0.6  Make a folder for the test data:
       mkdir -p testdata

======================================================================
STEP 1 — CREATE TEST DATA
======================================================================
1.1  Random bulk files. The generator uses only the Python standard library, and the same N always gives the same file.
     Columns: phone,name,country,city,company. About 8% of rows are foreign numbers, about 10% are duplicates, and about 8% are junk values.
       python3 scripts/make_sample.py 1000    testdata/s1k.csv
       python3 scripts/make_sample.py 100000  testdata/s100k.csv
       python3 scripts/make_sample.py 1000000 testdata/s1m.csv
       wc -l testdata/*.csv && ls -lh testdata/

1.2  Golden file. Every row has a known expected result:
       cat > testdata/golden.csv <<'EOF'
phone,name,country,city
+91 98765 43210,Asha,India,Pune
09123456789,Bilal,,Delhi
(415) 555-0132,Carol,United States,San Francisco
07911 123456,Dan,UK,London
0044 20 7946 0958,Eve,,London
050 123 4567,Farah,UAE,Dubai
9.19877E+11,Gita,India,Pune
,Hari,India,Pune
12345,Ivan,India,Pune
447911123457,Jo,,Leeds
8123 4567,Kai,Singapore,Singapore
+1 (212) 555-0199,Liam,,New York
n/a,Mia,,
+91 98765 43210,Asha Duplicate,India,Pune
919876543210,Asha Again,India,Pune
EOF
     Check with `head -2 testdata/golden.csv`: the first line must be exactly "phone,name,country,city".

1.3  Outlook-style file. Phone columns are tried in priority order, and the name is joined from its parts:
       printf '%s\n' \
         'First Name,Last Name,Company,Business Phone,Mobile Phone,Home Country/Region,E-mail Address' \
         'Anita,Jorgensen,Contoso,555-555-1212,+1 425 882 8080,,anita@contoso.com' \
         'Kemal,Celik,Contoso,0212 555 1234,,Turkey,kemal@contoso.com' \
         'Shiori,Inoue,Contoso,555-555-1212,,,shiori@contoso.com' > testdata/outlook.csv

1.4  A file with no phone column (must be rejected):
       printf 'email,name,fax\na@b.com,x,12345\n' > testdata/nophone.csv

1.5  An all-foreign file (every number uses the slower parser):
       python3 - <<'EOF'
import csv, random
r = random.Random(3)
with open('testdata/foreign100k.csv', 'w', newline='') as f:
    w = csv.writer(f); w.writerow(['phone', 'name', 'country', 'city'])
    for i in range(100000):
        c = r.choice(['US', 'UK', 'UAE', 'SG'])
        p = {'US': f"({r.randrange(201,989)}) {r.randrange(200,999)}-{r.randrange(1000,9999)}",
             'UK': f"07{r.randrange(100,999)} {r.randrange(100000,999999)}",
             'UAE': f"05{r.choice('024568')} {r.randrange(100,999)} {r.randrange(1000,9999)}",
             'SG': f"{r.choice('89')}{r.randrange(100,999)} {r.randrange(1000,9999)}"}[c]
        w.writerow([p, f'Person {i}', c, 'X'])
EOF

======================================================================
STEP 2 — TESTS  (run in this order; start with a clean database)
======================================================================
Run first:  reset_db     → expected: {"ok":true,...}

T1  SMOKE (1k rows)
    upload testdata/s1k.csv 1          → note import_id (call it ID)
    wait_done ID; counts ID
    PASS if: status=done, rows=1000, and valid_rows + invalid_rows + duplicate_rows = 1000.

T2  GOLDEN VALIDATION (exact expected results)
    upload testdata/golden.csv 11 → ID; wait_done ID; counts ID
    q "select phone_e164, name from contacts where import_id=ID order by id"
    q "select row_no, raw_phone, reason from import_errors where import_id=ID order by row_no"
    PASS only if ALL of these hold:
      - counts: rows=15, valid_rows=9, invalid_rows=4, duplicate_rows=2
      - contacts are exactly these 9, in this order:
        +919876543210 Asha | +919123456789 Bilal | +14155550132 Carol | +447911123456 Dan |
        +442079460958 Eve | +971501234567 Farah | +447911123457 Jo | +6581234567 Kai | +12125550199 Liam
      - import_errors are exactly: row 7 "9.19877E+11" sci_notation | row 8 (empty) empty | row 9 "12345" invalid | row 13 "n/a" invalid
      - the name for +919876543210 is "Asha", not "Asha Duplicate" (the first row wins)

T3  OUTLOOK FORMAT
    upload testdata/outlook.csv 12 → ID; wait_done ID; counts ID
    q "select phone_e164, name, vars from contacts where import_id=ID order by id"
    q "select row_no, raw_phone, reason from import_errors where import_id=ID"
    PASS if: valid_rows=2, invalid_rows=1;
      contacts = "+14258828080 | Anita Jorgensen" (taken from Mobile Phone, because 555-555-1212 is a fake number) and "+902125551234 | Kemal Celik";
      vars contains Company and E-mail Address;
      the error row is row 3, raw_phone "555-555-1212", reason invalid.

T4  IDEMPOTENT RE-UPLOAD
    upload testdata/golden.csv 11
    PASS if the response is {"import_id":<same ID as T2>,"duplicate":true} and `q "select count(*) from imports where campaign_id=11"` = 1.

T5  SAME FILE, DIFFERENT CAMPAIGN = NEW IMPORT
    upload testdata/golden.csv 21 → ID; wait_done ID; counts ID
    PASS if duplicate is false, a new import_id is returned, and the counts are the same as T2 (9/4/2).

T6  NO PHONE COLUMN
    curl -s -w " HTTP %{http_code}\n" -X POST --data-binary @testdata/nophone.csv "localhost:8000/api/imports?campaign_id=30"
    PASS if HTTP 422 and the message mentions "no phone column".

T7  KEYSET PAGINATION
    curl -s "localhost:8000/api/campaigns/11/contacts?limit=4"
    curl -s "localhost:8000/api/campaigns/11/contacts?limit=4&after_id=<last id of page 1>"
    PASS if page 2 starts after the last id of page 1, ids ascend, and no id appears on both pages.

T8  100k PERFORMANCE
    upload testdata/s100k.csv 40 → ID; wait_done ID; counts ID
    Also record the machine size: nproc; free -m
    PASS if status=done, rows=100000 and the three counts add up to 100000. Record the seconds value (no hard limit; on 4 cores expect about 3–5 s).

T9  ALL-FOREIGN NUMBERS
    upload testdata/foreign100k.csv 41 → ID; wait_done ID; counts ID
    q "select substr(phone_e164,1,3), count(*) from contacts where import_id=ID group by 1 order by 2 desc"
    PASS if: done, the counts add up to 100000, valid_rows > 90000, and the prefixes are mostly +1, +44, +97 (UAE +971) and +65. There must be (almost) no +91.

T10 1M ROWS + CONSTANT MEMORY
    upload testdata/s1m.csv 50 → ID
    While it runs, take 5 samples 3 seconds apart:
      for i in 1 2 3 4 5; do docker stats --no-stream --format "{{.Name}} {{.MemUsage}} {{.CPUPerc}}"; sleep 3; done
    wait_done ID; counts ID
    q "select min((data->>'rss_mb')::float), max((data->>'rss_mb')::float) from import_events where import_id=ID and kind='chunk'"
    PASS if: done, rows=1000000, the counts add up to 1000000, the worker container memory stays roughly flat (it must not grow with rows processed), and max rss_mb - min rss_mb < 50.
    Record: seconds, the peak worker memory, the peak CPU.

T11 CRASH + RESUME (exactly-once)
    Clean reference: use T10's counts (same file s1m.csv, campaign 50).
    upload testdata/s1m.csv 51 → ID
    Poll until it is part-way: until [ "$(q "select checkpoint_row from imports where id=ID")" -ge 200000 ]; do sleep 0.5; done
    Crash the worker hard:      docker compose kill -s SIGKILL worker
    q "select status, checkpoint_row from imports where id=ID"      → expect processing, and a checkpoint >= 200000
    Start it again:             docker compose up -d worker
    wait_done ID; counts ID
    q "select count(*) from contacts where import_id=ID"
    q "select count(*) from import_events where import_id=ID and kind='claimed'"
    PASS if: done; valid/invalid/duplicate are IDENTICAL to T10; the contacts count = valid_rows; and there are 2 claimed events (the job was taken over after the crash).

T12 GRACEFUL STOP + RESUME
    upload testdata/s1m.csv 52 → ID; wait until checkpoint_row >= 200000 (as in T11)
    docker compose stop worker
    q "select status, checkpoint_row from imports where id=ID"      → expect queued (not processing)
    q "select kind from import_events where import_id=ID and kind='released'"   → expect one row
    docker compose start worker; wait_done ID; counts ID
    PASS if: status was queued after the stop, a released event exists, and the final counts are IDENTICAL to T10.

T13 RESET BUTTON / API
    reset_db
    q "select (select count(*) from imports), (select count(*) from contacts), (select count(*) from import_errors)"
    PASS if the output is 0|0|0. Then `upload testdata/golden.csv 11` must return duplicate:false and import_id 1.

T14 UI (only if you can use a browser, e.g. Playwright/Chromium; otherwise mark SKIPPED)
    Open http://localhost:8000
    a) Upload testdata/s100k.csv with the drop zone. The flow boxes light up in order: upload loop → queue → worker → chunk loop → done.
    b) Click the speed buttons 0.1× and 5×. The playback speed changes.
    c) Drag the timeline bar back to about 30%. The counters and the "chunk N / 10" label go back to earlier values.
    d) Click any round "i" button. A popover with "What" and "Why it matters" appears. Click outside and it closes.
    e) The CPU / RAM / DB writes charts show lines, and their values change as the cursor moves.
    f) Click "reset db" (bottom-left) and confirm. The page clears.
    Take a screenshot for each of a–f.

T15 UPLOAD SIZE LIMIT (optional, do it last)
    Add to docker-compose.override.yml, under services:
      api:
        environment:
          MAX_UPLOAD_MB: "1"
    docker compose up -d api; wait 5 s
    curl -s -w " HTTP %{http_code}\n" -X POST --data-binary @testdata/s100k.csv "localhost:8000/api/imports?campaign_id=60"
    PASS if HTTP 413. Afterwards remove those lines and run `docker compose up -d api`.

======================================================================
STEP 3 — REPORT
======================================================================
Write TEST_REPORT.md containing:
1. Environment: OS, `nproc`, `free -m`, docker version, git commit (`git rev-parse --short HEAD`).
2. A results table with the columns: Test | PASS/FAIL/SKIPPED | Key numbers | Evidence (the command output, trimmed).
3. Performance: seconds for T8, T9 and T10; the peak worker memory and CPU from T10.
4. For every FAIL: the exact command, the expected result, the actual output, and the relevant logs:
     docker compose logs --tail=80 worker
     docker compose logs --tail=80 api
5. Anything surprising you noticed, even if the test passed.
Do not claim PASS without pasted evidence.

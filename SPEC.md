# SPEC: Orch — Serial-Queue Job Orchestrator (Phase 1 Prototype)

> วิธีใช้กับ Claude Code: วางไฟล์นี้ไว้ที่ root ของโปรเจกต์ (แนะนำตั้งชื่อ `CLAUDE.md` หรือ `SPEC.md`) แล้วสั่งว่า
> **"อ่าน SPEC.md แล้วสร้าง Phase 1 ตามลำดับใน Build Order ทีละขั้น เขียน test ทุกขั้น และรัน test ให้ผ่านก่อนไปขั้นถัดไป"**

---

## 1. เป้าหมาย

สร้างโปรแกรมแทน Windows Task Scheduler ที่:

- ตั้งงานง่าย มี GUI และมี CLI สไตล์ `schtasks` (เรียกจาก PowerShell ได้ พร้อมผลลัพธ์ `--json`)
- รองรับ trigger และ action หลายแบบ
- **รันทีละ Job (Serial Queue)** มีกฎลำดับคิวชัดเจน
- มี Retry, Timeout + Kill process tree, Log ต่อรอบการรัน
- มี Dependency แบบ chain (B รันหลัง A)
- เบา: Python stdlib เท่านั้น, ไม่มี Windows Service ของตัวเอง
- รันได้แม้ไม่มีคน login (ผ่าน engine ที่ถูกเปิดตอน boot)

**Non-goals ของ Phase 1:** รันขนาน/หลาย Worker บนหลายเครื่อง, Web UI, Credential store, Alert (อีเมล/LINE), Dependency แบบหลายตัว (AND), per-job run-as account

## 2. ข้อจำกัดและการตัดสินใจ (ห้ามเปลี่ยนโดยไม่ถามผม)

| หัวข้อ | ตัดสินใจ |
|---|---|
| ภาษา | Python 3.10+ **stdlib only** (GUI ใช้ `tkinter`; `pyodbc` เป็น optional เฉพาะ job type `sql`) |
| Storage | SQLite (WAL) ไฟล์เดียว `orch.db` ต้องอยู่บน **local disk** (WAL ใช้บน network share ไม่ได้) |
| โครงไฟล์ | `orch.py` (core + engine + CLI ไฟล์เดียว), `orch_gui.py`, `tests/`, `README.md` |
| เวลา | เก็บเป็น local naive `YYYY-MM-DD HH:MM:SS` (เทียบ string ได้ตรง ๆ) ระบุใน README ว่ายังไม่รองรับ DST |
| Thread/Connection | **ทุก thread ต้องมี sqlite connection ของตัวเอง**, `isolation_level=None`, ใช้ `BEGIN IMMEDIATE` ผ่าน context manager `tx(con)` |
| Engine | **1 engine ต่อ 1 queue** รัน Job ทีละตัว; queue ต่างกันรันขนานกันได้ (คนละ process) |
| Account | engine รันด้วยสิทธิ์ของ account ที่เปิดมัน → Job ทุกตัวใน queue นั้นสืบทอดสิทธิ์นี้ ถ้าต้องการหลาย account ให้แยก queue + engine (ไม่ทำ per-job run-as ใน Phase 1) |
| Process model | ทุก Job รันเป็น **child process เสมอ** (รวม `sql`/`http` ที่รันผ่าน `orch.py _exec <type>`) เพื่อให้ timeout/kill ใช้กลไกเดียวกันหมด |
| UI automation | ต้องมี user session → ให้ใช้ queue แยก และเปิด engine ของ queue นั้นแบบ `onlogon` ไม่ใช่ตอน boot (ระบุใน README) |

## 3. Data Model (SQLite)

```sql
job_def(id PK, name UNIQUE, job_type, params TEXT(JSON), queue DEFAULT 'default',
        priority INT DEFAULT 5, timeout_sec INT DEFAULT 3600 (0=ไม่จำกัด),
        max_retry INT DEFAULT 0, retry_delay_sec INT DEFAULT 60, retry_backoff REAL DEFAULT 2.0,
        dedup_policy DEFAULT 'skip',      -- allow | skip | coalesce
        missed_policy DEFAULT 'run_once', -- skip | run_once | run_all
        after_job TEXT NULL, after_on DEFAULT 'success', -- success | failure | always
        enabled INT DEFAULT 1, description, created_at)

schedule(id PK, job_id FK->job_def ON DELETE CASCADE, kind, expr, next_run_at, last_fire_at, enabled)
        -- kind: cron | every | at | startup

job_run(id PK, job_id (ไม่ใส่ FK เพื่อเก็บประวัติเมื่อลบ def), job_name, queue, status, priority,
        trigger, attempt DEFAULT 1, retry_of, scheduled_at, not_before,
        started_at, finished_at, heartbeat, pid, exit_code, error, cancel_requested DEFAULT 0)
        INDEX (queue, status, priority, scheduled_at, id)

run_log(id PK, run_id, ts, level, message)   INDEX (run_id, id)
queue_state(name PK, paused)
engine_lock(queue PK, host, pid, heartbeat)
```

**Status ของ job_run:** `Pending, Running, Succeeded, Failed` (ล้มเหลวแต่มี retry ต่อ), `Dead` (ล้มเหลวและหมดสิทธิ์ retry), `Cancelled`, `Skipped` (ถูก dedup/disabled)

**params (JSON) ตามชนิดงาน**

| job_type | params |
|---|---|
| `command` | `command`, `cwd?`, `env?`, `encoding?` (รันด้วย shell) |
| `powershell` | `file` + `args?` หรือ `command`; เรียก `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass` |
| `python` | `file`, `args?`; ใช้ `sys.executable -u`, ตั้ง `PYTHONIOENCODING=utf-8` |
| `sql` | `conn` (รองรับ `env:VARNAME` อ่านจาก environment ตอนรัน), `query` หรือ `query_file`; ใช้ `pyodbc`; รองรับ `sqlite:<path>` สำหรับ test; commit เมื่อสำเร็จ, rollback เมื่อ error; **ไม่รองรับ `GO`** |
| `http` | `url`, `method?`, `headers?`, `body?`, `expect?` (เช่น `"200,201"`, default 2xx), `timeout?` |

## 4. พฤติกรรมหลัก (ต้องทำตามนี้ตรง ๆ)

### 4.1 กฎลำดับคิว
```sql
SELECT * FROM job_run
WHERE queue=? AND status='Pending' AND (not_before IS NULL OR not_before <= now)
ORDER BY priority DESC, scheduled_at ASC, id ASC LIMIT 1
```
Claim แบบ atomic ภายใน `BEGIN IMMEDIATE` (เปลี่ยนเป็น `Running` + ตั้ง `started_at/heartbeat`) ถ้า queue ถูก pause ไม่หยิบงานใหม่ (งานที่รันอยู่ทำต่อจนจบ) ทำ pre-check แบบ SELECT ธรรมดาก่อนเพื่อไม่ต้องล็อกเขียนตอนคิวว่าง

### 4.2 Engine (`orch.py engine --queue Q`)
- **สอง thread:** main = worker loop (รันงานทีละตัว), อีกตัว = scheduler thread (ทุก 1 วินาที: fire schedule ที่ถึงเวลา, อัปเดต lock ทุก ~5 วินาที, prune ทุกชั่วโมง) — ต้องแยก thread เพื่อให้ schedule ยัง fire ระหว่างงานยาวรันอยู่
- **Engine lock:** ตอนเริ่ม `BEGIN IMMEDIATE` อ่าน `engine_lock`; ถ้ามีแถวของ engine อื่นและ heartbeat สดกว่า 30 วินาที → ออกด้วย error; ถ้า stale → ยึดต่อ ระหว่างทำงานอัปเดต heartbeat ด้วย `WHERE pid=? AND host=?` ถ้า rowcount=0 (ถูกแย่ง) ให้ engine หยุดตัวเอง; ตอนจบปกติลบแถว lock
- **Startup:** (1) recovery (2) fire trigger ชนิด `startup` ของ job ใน queue นี้ (3) เริ่ม scheduler thread (4) เริ่ม worker loop
- **Graceful shutdown** (SIGINT/SIGTERM/SIGBREAK): หยุดหยิบงานใหม่; ถ้ามีงานรันอยู่ให้ kill process tree แล้ว **คืนงานกลับเป็น `Pending`** (ไม่นับเป็น attempt ที่ล้มเหลว) พร้อม log WARN
- **Logging ของ engine:** stdout (ถ้ามี) + RotatingFileHandler ที่ `logs/engine-<queue>.log` ข้างไฟล์ DB (engine ที่เปิดจาก Task Scheduler ไม่มี console)

### 4.3 การรัน Job
1. โหลด def; ถ้า def ถูกลบ → จบเป็น `Dead` ("definition deleted"); ถ้า def `enabled=0` และ trigger ไม่ใช่ `manual` → `Skipped`
2. สร้าง command จาก `job_type` + `params`, `Popen` โดย `stdin=DEVNULL`, `stdout=PIPE`, `stderr=STDOUT` (โหมด bytes), Windows ใช้ `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`, POSIX ใช้ `start_new_session=True`
3. thread อ่าน output ทีละบรรทัดลง `run_log` (level `OUT`), จำกัด 5,000 บรรทัด/รัน และ 2,000 ตัวอักษร/บรรทัด เกินให้ log WARN ครั้งเดียว; decode ด้วย encoding ที่กำหนด (default บน Windows = OEM code page ผ่าน `GetOEMCP`, ที่อื่น utf-8; `python`/`sql`/`http` ใช้ utf-8)
4. monitor loop ทุก 0.25 วินาที: ตรวจ timeout, อัปเดต `heartbeat` ทุก 5 วินาที, ตรวจ `cancel_requested` ทุก 2 วินาที, ตรวจ stop event
5. เมื่อ timeout/cancel/shutdown → **kill process tree**: Windows `taskkill /PID <pid> /T /F`; POSIX `killpg(SIGTERM)` รอ 3 วินาทีแล้ว `killpg(SIGKILL)` (ต้อง kill กลุ่มซ้ำแม้ leader จบแล้ว)
6. `finalize`: 
   - exit 0 → `Succeeded`
   - cancel → `Cancelled`
   - อื่น ๆ (exit ≠ 0, timeout, เริ่มโปรเซสไม่ได้, config error) → ถ้า `attempt <= max_retry` เป็น `Failed` แล้วสร้าง run ใหม่ (`attempt+1`, `trigger='retry'`, `retry_of`, คง `scheduled_at` เดิม, `not_before = now + retry_delay_sec × retry_backoff^(attempt-1)`) มิฉะนั้นเป็น `Dead`
   - **Retry ต้องไม่ทำให้คิวติด** เพราะใช้ `not_before` ให้งานอื่นแซงได้
7. ถ้าจบสถานะ `Succeeded` หรือ `Dead` → trigger งานที่ `after_job = ชื่อ job นี้` ตาม `after_on` (`success`↔Succeeded, `failure`↔Dead, `always`↔ทั้งคู่); `Cancelled`/`Failed`(ที่ยังมี retry) ไม่ trigger; งานที่ถูก trigger ผ่าน dedup policy ของตัวเองตามปกติ (อาจอยู่คนละ queue ได้)

### 4.4 Enqueue + Dedup (ทำใน transaction เดียว)
- `allow`: เพิ่มเสมอ
- `skip`: ถ้ามี `Pending` ของ job เดียวกันอยู่แล้ว → สร้างแถวสถานะ `Skipped` (error = `dedup(skip): มีงานรออยู่แล้ว #id`) เพื่อให้เห็นในประวัติ
- `coalesce`: ตั้ง `Pending` เก่าเป็น `Skipped` (error = `coalesced by #newid`) แล้วเพิ่มตัวใหม่
- `manual` (Run now) และ `retry` **ข้าม dedup เสมอ**

### 4.5 Schedule
| kind | รูปแบบ | หมายเหตุ |
|---|---|---|
| `cron` | 5 ฟิลด์ `m h dom mon dow` | รองรับ `*`, list, range, step (`*/5`, `1-5`, `5/10`); dow 0–7 (0 และ 7 = อาทิตย์); ถ้า dom และ dow ระบุทั้งคู่ใช้ **OR** ตามมาตรฐาน cron; เขียน parser เอง, หาเวลาถัดไปแบบข้ามวัน/ชั่วโมงที่ไม่ตรงเพื่อไม่ให้วนทีละนาที, หาไม่เจอภายใน ~5 ปีให้ raise |
| `every` | `30s`, `5m`, `2h`, `1d` หรือจำนวนวินาที | anchor กับเวลา fire ก่อนหน้า (`prev + interval`) เพื่อไม่ให้เวลาเลื่อนสะสม |
| `at` | `YYYY-MM-DD HH:MM[:SS]` | ครั้งเดียว; เวลาในอดีตตอนสร้าง → error; fire แล้วตั้ง `next_run_at=NULL, enabled=0` |
| `startup` | — | fire ตอน engine ของ queue นั้นเริ่ม |

หนึ่ง job มีได้หลาย schedule

**Missed schedule algorithm** (ในแต่ละ tick ต่อ schedule ที่ `next_run_at <= now`):
1. วนเก็บเวลา fire ทั้งหมดที่ `<= now` (cap 1,000 รอบ; ถ้าชน cap ให้คำนวณ `next` ใหม่จาก now)
2. แบ่งเป็น *on-time* (ช้าไม่เกิน 90 วินาที) และ *missed* (ช้ากว่านั้น)
3. เลือก enqueue: on-time ทั้งหมด + missed ตาม policy (`skip` = ไม่เอา, `run_once` = เอาตัวสุดท้ายตัวเดียว, `run_all` = เอาทั้งหมด) ใช้ `scheduled_at` = เวลา fire เดิม
4. อัปเดต `next_run_at`, `last_fire_at`
- หมายเหตุใน README: `run_all` ต้องคู่กับ `dedup=allow` มิฉะนั้น dedup จะตัดรอบที่เกินทิ้ง

### 4.6 Recovery ตอน engine เริ่ม
งานของ queue นี้ที่ค้าง `Running` (engine ก่อนหน้าตาย) → log WARN; ถ้า pid ที่บันทึกไว้ยังมีชีวิต ให้ log WARN ว่า "อาจยังรันอยู่ ตรวจสอบด้วยตนเอง" (**ห้าม auto-kill** เพราะเสี่ยง pid reuse) แล้ว finalize เป็นล้มเหลว (`engine lost`) ตามกติกา retry ปกติ ระบุใน README เรื่อง idempotency

### 4.7 อื่น ๆ
- **Cancel:** `Pending` → `Cancelled` ทันที; `Running` → ตั้ง `cancel_requested=1` แล้ว engine kill ภายใน ~2 วินาที
- **Pause/Resume queue:** ผ่าน `queue_state`
- **Prune:** ลบ run + log ที่จบแล้วเก่ากว่า N วัน (default 30) ทั้งอัตโนมัติรายชั่วโมงและคำสั่ง `prune --days N`
- **Validation ตอนสร้าง/แก้:** ตรวจ params ตามชนิดงาน, ตรวจ `after_job` ต้องมีอยู่จริงและ **ห้ามเกิด cycle** (A→B→A), ชื่อ job ห้ามซ้ำ, เปลี่ยนชื่อ job ไม่รองรับใน Phase 1 (chain อ้างด้วยชื่อ)
- **ความลับ:** ห้ามพิมพ์ params ทั้งก้อนลง log; `sql` เก็บ connection string เป็น `env:VAR` เป็นแนวทางแนะนำ; ส่ง params ให้ `_exec` ผ่าน environment variable `ORCH_PARAMS` ไม่ใช่ argv

## 5. CLI (`python orch.py <verb>`) — ตั้งชื่อ verb ให้ใกล้เคียง `schtasks`

| Verb | หน้าที่ |
|---|---|
| `init` | สร้าง DB/schema |
| `create NAME --type T ...` | สร้าง job (+ schedule ด้วย `--cron`, `--every`, `--at`, `--startup` ซ้ำได้) |
| `change NAME ...` | แก้เฉพาะ flag ที่ระบุ (ถ้าระบุ schedule flag ใด ๆ = แทนที่ schedule ทั้งชุด; `--after ""` = ล้าง chain) |
| `query [NAME] [--json]` | รายการ job พร้อมสรุป schedule และเวลารันถัดไป |
| `run NAME` | Run now (manual, ข้าม dedup) |
| `runs [--queue Q] [--status S] [--job NAME] [--limit N] [--json]` | ประวัติ/คิว |
| `end RUN_ID` | ยกเลิก run |
| `logs RUN_ID [--follow]` | ดู log |
| `enable NAME` / `disable NAME` / `delete NAME` | จัดการ job |
| `pause [--queue Q]` / `resume [--queue Q]` | หยุด/เดินคิว |
| `prune --days N` | ล้างประวัติเก่า |
| `engine --queue Q [--poll 1.0]` | รัน engine |
| `install-startup --queue Q --trigger boot\|logon [--user U] [--apply]` | สร้างสคริปต์ลงทะเบียน engine (ดู §7) โดยดีฟอลต์ **พิมพ์คำสั่งออกมาเฉย ๆ** ต้องใส่ `--apply` ถึงจะรันจริง |
| `_exec sql\|http` | ภายในเท่านั้น (ไม่แสดงใน help) |

Flags ของ create/change: `--type --command --file --args --cwd --conn --query --query-file --url --method --body --header k:v(ซ้ำได้) --expect --params <JSON> --queue --priority --timeout --retry --retry-delay --backoff --dedup --missed --after --after-on --desc --disabled`

ทุก verb ที่แสดงผลต้องไม่ crash เมื่อ console ไม่รองรับภาษาไทย (`sys.stdout.reconfigure(encoding="utf-8", errors="replace")`)

## 6. GUI (`orch_gui.py`, tkinter, อ่าน/เขียน DB เดียวกันโดย import จาก `orch.py`)

- แถบบนแสดงสถานะ engine ของแต่ละ queue (running/stopped จาก `engine_lock` ที่ heartbeat สด) และสถานะ pause
- **แท็บ Jobs:** ตาราง (ชื่อ, ชนิด, queue, priority, schedule, เวลารันถัดไป, enabled, after) + ปุ่ม New / Edit / Run now / Enable-Disable / Delete
- **แท็บ Queue/Runs:** ตารางรัน 200 รายการล่าสุด, filter ตามสถานะ/queue, ระบายสีตามสถานะ, ปุ่ม Cancel / Pause queue / Resume queue, ดับเบิลคลิก = เปิด log
- **แท็บ Log:** แสดง log ของ run ที่เลือก, refresh อัตโนมัติถ้ายัง Running
- **Job dialog:** ฟิลด์ทั่วไป (ชื่อ, ชนิด, queue, priority, timeout, retry, delay, backoff, dedup, missed, after/after_on, description, enabled) + กล่อง params JSON (เติม template ให้ตามชนิด และเปลี่ยน template เมื่อเปลี่ยนชนิดถ้ายังไม่ได้แก้) + กล่อง schedules (บรรทัดละหนึ่งรายการ เช่น `cron: */5 * * * *`, `every: 10m`, `at: 2026-10-01 08:00`, `startup`)
- refresh ทุก 2 วินาทีแบบ **อัปเดตแถวเดิมในที่เดิม** (ไม่ลบแล้วสร้างใหม่ทั้งตาราง เพื่อไม่ให้ selection/scroll หาย)
- GUI ห้ามมี logic ธุรกิจของตัวเอง เรียกฟังก์ชันใน `orch.py` เท่านั้น (create_job, update_job, enqueue, cancel_run, set_paused ฯลฯ)

## 7. การติดตั้ง engine บน Windows

- ใช้ **Task Scheduler เป็นแค่ตัวเปิด engine** (หนึ่ง task ต่อหนึ่ง queue) ไม่ใช่ตัวตั้งเวลางานทั้งหมด
- ⚠️ Task ที่สร้างด้วย `schtasks` มี *Stop the task if it runs longer than 72 hours* เป็นค่าเริ่มต้น ซึ่งจะฆ่า engine ทุก 3 วัน ดังนั้น `install-startup` ต้องสร้างด้วย PowerShell `Register-ScheduledTask` และตั้ง `-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable`
- `boot` → trigger `-AtStartup`, principal `SYSTEM` (หรือ `--user` พร้อม `-User/-Password`, รันได้แม้ไม่มีคน login) → **ห้ามใช้กับงาน UI automation**
- `logon` → trigger `-AtLogOn` รันใน session ของ user เหมาะกับ queue สำหรับ UI automation (ต้อง auto-login และไม่ Lock หน้าจอ)
- ส่วนนี้ทดสอบบน Windows จริงเท่านั้น ให้แยกเป็น "manual test checklist" ใน README

## 8. Build Order (ทำทีละขั้น จบแต่ละขั้นต้องมี test ผ่าน)

1. **Foundation:** schema, `connect()`, `tx()`, time helpers, `init`
2. **Cron/interval parser + next-run** (unit test ล้วน ๆ: `*/5`, ช่วงคร่อมเดือน/ปี, dom+dow OR, ก.พ. 29)
3. **Job CRUD + validation + CLI create/query/change/delete/enable/disable** (รวมตรวจ cycle)
4. **Enqueue + dedup + claim_next + กฎลำดับคิว + pause/resume + cancel(Pending)**
5. **Executor + monitor loop:** command/python/powershell, log capture, timeout+kill tree, cancel(Running), heartbeat
6. **Finalize + Retry/backoff + Dependency chain**
7. **Scheduler thread + missed policy + startup trigger + prune**
8. **Engine lock + recovery + graceful shutdown + engine logging**
9. **`sql` และ `http` (`_exec`)**
10. **GUI**
11. **`install-startup` + README + manual test checklist สำหรับ Windows**

## 9. Test Plan (`tests/`, ใช้ `unittest`/`pytest`, DB ชั่วคราวผ่าน `ORCH_DB`)

Job ใน test ให้ใช้ `sys.executable -c "..."` เพื่อให้รันได้ทุก OS ส่วน engine เปิดเป็น subprocess จริง และมี helper `wait_until(cond, timeout)`

| # | สถานการณ์ | ผลที่ต้องได้ |
|---|---|---|
| 1 | Pause queue, enqueue prio 1 → 9 → 5 แล้ว resume | เริ่มรันตามลำดับ 9, 5, 1; priority เท่ากันเป็น FIFO |
| 2 | Job exit 3, `max_retry=2`, delay 1s | 3 รัน: Failed, Failed, Dead; `attempt` 1,2,3; เวลาห่างตาม backoff |
| 3 | Job หลับ 30s, `timeout=2` แล้วมี job ถัดไป | Job แรกถูก kill (โปรเซสตายจริง รวมลูก), error ระบุ timeout, job ถัดไปรันต่อไม่ติดคิว |
| 4 | A สำเร็จ, B `after=A` | B ถูกสร้างด้วย trigger `after:A#id`; A ล้มเหลว → B ไม่รัน (เมื่อ `after_on=success`) |
| 5 | dedup `skip` / `coalesce` ขณะคิว pause | skip: รอบสองเป็น `Skipped`; coalesce: ตัวเก่า `Skipped` ตัวใหม่ `Pending` |
| 6 | Cancel งานที่กำลังรัน | จบเป็น `Cancelled` ภายใน ~3 วินาที และโปรเซสตาย |
| 7 | Missed: ตั้ง `next_run_at` ย้อนหลัง 1 ชั่วโมง กับ `every 10m` | `skip`=0 รัน, `run_once`=1 รัน, `run_all`+`dedup=allow`=ประมาณ 6 รัน |
| 8 | Kill -9 engine ระหว่างงานรัน แล้วเปิดใหม่ | งานเดิมถูกตั้งเป็นล้มเหลว (`engine lost`) และ retry ตามกติกา; มี WARN ถ้า pid ยังมีชีวิต |
| 9 | เปิด engine ซ้อนใน queue เดียวกัน | ตัวที่สองออกพร้อม error ชัดเจน |
| 10 | Schedule fire ระหว่างงานยาวรันอยู่ | มี run ใหม่เข้าคิวระหว่างนั้น (พิสูจน์ว่า scheduler thread แยกจริง) |
| 11 | `sql` (sqlite) และ `http` (local `http.server` ใน thread) | สำเร็จ/ล้มเหลวตาม status/exit; log มีผล query/สถานะ HTTP |
| 12 | Graceful shutdown ระหว่างงานรัน | งานกลับเป็น `Pending` และ `attempt` ไม่เพิ่ม |
| 13 | Validation | cycle, ชื่อซ้ำ, cron ผิด, `at` อดีต, params ขาด → error ชัดเจน ไม่มี job ค้างครึ่งทาง |

## 10. Acceptance Criteria (Phase 1 ถือว่าเสร็จเมื่อ)

- ทุกข้อใน Test Plan ผ่านบน Windows และ Linux (ยกเว้นส่วนเฉพาะ Windows ใน §7 ที่ทดสอบมือ)
- engine รันต่อเนื่อง ≥ 72 ชั่วโมงบน Windows โดยไม่ถูกหยุด (ยืนยัน `ExecutionTimeLimit`)
- ย้ายงานจริง 3–5 งานจาก Task Scheduler มาใช้ (SQL/Script/HTTP) และดูผลได้ครบทั้งใน GUI และ CLI
- README อธิบาย: การติดตั้ง, ตัวอย่างคำสั่ง PowerShell (`orch query --json | ConvertFrom-Json`), ข้อจำกัดที่รู้ (ไม่มี DST, DB ต้องอยู่ local disk, ต้องทำ Job ให้ idempotent เพราะมี retry/recovery, `sql` ไม่รองรับ `GO`, connection string ควรใช้ `env:`)

## 11. ความเสี่ยงที่ต้องรายงานผมทันทีถ้าเจอระหว่างสร้าง

- พฤติกรรม kill process tree บน Windows ไม่เด็ดขาด (โปรเซสลูกหลุด)
- SQLite lock contention เมื่อมี engine หลาย queue + GUI + pump thread เขียนพร้อมกัน
- Encoding ของ output ภาษาไทยเพี้ยนใน log (ทดสอบกับ `cmd`, PowerShell และ Python)
- ข้อกำหนดที่ขัดกันเอง เช่น "ไม่ต้องมี service" กับ "รันได้แม้ไม่มีคน login" (แนวทางใน §7 คือคำตอบที่เลือกไว้แล้ว ถ้าจะเปลี่ยนให้ถามก่อน)

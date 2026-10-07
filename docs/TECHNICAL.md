# Orch — เอกสารทางเทคนิค (Technical Documentation)

เอกสารนี้อธิบายการออกแบบและการทำงานภายในของ Orch Phase 1 สำหรับผู้ดูแลระบบและนักพัฒนาที่จะแก้ไขหรือต่อยอดโปรแกรม
ถ้าต้องการวิธีใช้งานและการตั้งค่า ดู [MANUAL.md](MANUAL.md) ส่วนข้อกำหนดต้นฉบับอยู่ที่ [SPEC.md](../SPEC.md)

---

## สารบัญ

1. [ภาพรวม](#1-ภาพรวม)
2. [สถาปัตยกรรม](#2-สถาปัตยกรรม)
3. [โครงสร้างไฟล์และโค้ด](#3-โครงสร้างไฟล์และโค้ด)
4. [Data model](#4-data-model)
5. [วงจรชีวิตของ run](#5-วงจรชีวิตของ-run)
6. [คิว: enqueue, dedup, claim](#6-คิว-enqueue-dedup-claim)
7. [Engine](#7-engine)
8. [Executor: การรันโปรเซสลูก](#8-executor-การรันโปรเซสลูก)
9. [Finalize, retry และ dependency chain](#9-finalize-retry-และ-dependency-chain)
10. [Scheduler](#10-scheduler)
11. [งานชนิด sql และ http (`_exec`)](#11-งานชนิด-sql-และ-http-_exec)
12. [SQLite และ concurrency](#12-sqlite-และ-concurrency)
13. [ความปลอดภัยและความลับ](#13-ความปลอดภัยและความลับ)
14. [GUI](#14-gui)
15. [การลงทะเบียนกับ Task Scheduler](#15-การลงทะเบียนกับ-task-scheduler)
16. [ค่าคงที่และ exit code](#16-ค่าคงที่และ-exit-code)
17. [การทดสอบ](#17-การทดสอบ)
18. [ส่วนที่เพิ่มหรือตีความจาก SPEC](#18-ส่วนที่เพิ่มหรือตีความจาก-spec)
19. [ข้อจำกัดที่รู้](#19-ข้อจำกัดที่รู้)

---

## 1. ภาพรวม

Orch เป็นตัวตั้งเวลารันงานแทน Windows Task Scheduler โดยมีหลักการสำคัญดังนี้

| หลักการ | รายละเอียด |
|---|---|
| Serial queue | งานในคิวเดียวกันรันทีละงานเสมอ คิวต่างกันรันขนานกันได้ (คนละ engine คนละโปรเซส) |
| Stdlib only | Python 3.10+ (แนะนำ 3.12 แบบ all users) ใช้เฉพาะ standard library (`pyodbc` จำเป็นเฉพาะ `sql` ที่ไม่ใช่ sqlite) |
| ฐานข้อมูลเดียว | SQLite โหมด WAL ไฟล์เดียว (`orch.db`) ใช้ร่วมกันระหว่าง CLI, GUI และ engine ทุกตัว |
| ทุกงานเป็นโปรเซสลูก | ทำให้ timeout, cancel และ kill ใช้กลไกเดียวกันทุกชนิดงาน |
| ไม่มี service ของตัวเอง | ใช้ Task Scheduler เป็นแค่ตัวเปิด engine ตอน boot หรือตอน logon |

## 2. สถาปัตยกรรม

```
 ┌──────────────┐   ┌──────────────┐        ┌─────────────────────────────────────────┐
 │ CLI orch.py  │   │ orch_gui.py  │        │ engine (1 โปรเซสต่อ 1 queue)            │
 │ create/run/  │   │ tkinter,     │        │                                         │
 │ end/pause... │   │ refresh 2 s  │        │  main thread = worker loop              │
 └──────┬───────┘   └──────┬───────┘        │    claim_next → run_job → finalize      │
        │ เขียน/อ่าน        │ เขียน/อ่าน       │                                         │
        ▼                  ▼                │  scheduler thread (ทุก 1 วินาที)          │
 ┌─────────────────────────────────────┐    │    scheduler_tick, lock heartbeat (5 s), │
 │           orch.db (SQLite WAL)      │◄──►│    prune (ทุกชั่วโมง)                     │
 │ job_def, schedule, job_run, run_log │    │                                         │
 │ queue_state, engine_lock            │    │  reader thread ต่อ 1 งาน (ไม่แตะ DB)      │
 └─────────────────────────────────────┘    └───────────────┬─────────────────────────┘
                                                            │ Popen (process group ใหม่,
                                                            │ Windows Job Object)
                                                            ▼
                                            ┌─────────────────────────────────────────┐
                                            │ child: cmd / powershell / python /      │
                                            │ orch.py _exec sql|http                   │
                                            └─────────────────────────────────────────┘
```

- **CLI และ GUI ไม่ได้สั่ง engine โดยตรง** ทุกอย่างสื่อสารผ่านตารางใน DB เช่น การสั่ง Run now คือการเพิ่มแถว `Pending` ใน `job_run`
  ส่วนการ cancel คือการตั้ง `cancel_requested=1` แล้ว engine จะอ่านค่าเองภายใน 2 วินาที
- engine หนึ่งตัวดูแลคิวเดียวเท่านั้น ถ้ามีหลายคิวต้องเปิดหลาย engine และ `engine_lock` จะป้องกันไม่ให้คิวเดียวมีสอง engine

## 3. โครงสร้างไฟล์และโค้ด

| ไฟล์ | เนื้อหา |
|---|---|
| `orch.py` | core ทั้งหมด: DB, schedule, jobs, runs, executor, engine, `_exec`, install-startup และ CLI |
| `orch_gui.py` | GUI (tkinter) เรียกฟังก์ชันใน `orch.py` เท่านั้น ไม่มี business logic ของตัวเอง |
| `tests/` | ชุดทดสอบ `unittest` (`helpers.py` + `test_01` ถึง `test_12`) |
| `*.bat` | ตัวเปิดโปรแกรมสำหรับผู้ใช้ (ASCII + CRLF) |
| `setup.ps1` / `uninstall.ps1` | ตัวติดตั้งและตัวถอนการติดตั้ง (UTF-8 **with BOM** + CRLF เพราะ PowerShell 5.1 อ่านไฟล์ที่ไม่มี BOM เป็น ANSI ทำให้ข้อความภาษาไทยเพี้ยน) |
| `logs/engine-<queue>.log` | log ของ engine (สร้างข้างไฟล์ DB) |

`orch.py` เรียงเป็นส่วน ๆ ตามลำดับนี้

| ส่วน | ฟังก์ชัน/คลาสหลัก |
|---|---|
| Time helpers | `now_dt`, `now_str`, `fmt_ts`, `parse_ts`, `add_seconds` |
| Schedules | `CronExpr`, `parse_every`, `validate_schedule`, `next_fire`, `initial_next_run`, `parse_schedule_line`, `format_schedule` |
| Database | `SCHEMA`, `SCHEMA_VERSION`, `db_path`, `connect`, `tx`, `init_db` (สร้างตารางและ migrate), `_Cursor`, `_Connection` |
| Job definitions | `validate_params`, `parse_expect`, `create_job`, `update_job`, `set_enabled`, `_reanchor_schedules`, `delete_job`, `get_job`, `list_jobs` |
| Runs | `_enqueue`, `enqueue`, `claim_next`, `set_paused`, `is_paused`, `cancel_run`, `list_runs`, `get_run`, `get_logs`, `add_log` |
| Executor | `build_command`, `default_encoding`, `execute_process`, `_supervise`, `_reader`, `_flush_logs`, `kill_tree`, `WinJob`, `pid_alive` |
| Lifecycle | `run_job`, `finalize`, `requeue`, `process_one` |
| Scheduler | `scheduler_tick`, `_fire_schedule`, `fire_startup`, `prune`, `SchedulerThread` |
| Engine | `acquire_lock`, `heartbeat_lock`, `release_lock`, `request_engine_stop`, `engine_stop_requested`, `engine_states`, `recover`, `setup_engine_logging`, `run_engine` |
| `_exec` | `exec_main`, `exec_sql`, `exec_http`, `resolve_secret`, `split_sqlite_statements`, `redact_url` |
| install-startup | `build_startup_script`, `engine_executable`, `startup_task_name`, `cmd_install_startup` |
| CLI | `build_parser`, `cmd_*`, `main` |

ข้อผิดพลาดที่เกิดจาก input ของผู้ใช้ทั้งหมดจะ raise เป็น `OrchError` แล้ว CLI แปลงเป็นข้อความ `error: ...` พร้อม exit code 2
ส่วน GUI แสดงเป็น messagebox

## 4. Data model

schema อยู่ในตัวแปร `SCHEMA` และถูกสร้างอัตโนมัติเมื่อ `connect()` พบว่า `PRAGMA user_version` น้อยกว่า `SCHEMA_VERSION` (ปัจจุบันคือ 2)

`init_db` ทำงานใน transaction เดียว: สร้างตารางที่ยังไม่มี (`IF NOT EXISTS`) แล้ว migrate ทีละขั้นแบบตรวจก่อนทำ จึงเรียกซ้ำได้
และปลอดภัยถ้าหลายโปรเซสเปิด DB เก่าพร้อมกัน
- v1 → v2: `ALTER TABLE engine_lock ADD COLUMN stop_requested` (ตรวจด้วย `PRAGMA table_info` ก่อน)

engine รุ่นเก่า (v1) ยังใช้ DB v2 ได้ เพราะ `acquire_lock` ระบุชื่อคอลัมน์ และคอลัมน์ใหม่มีค่าเริ่มต้นเป็น 0

### `job_def` — นิยามงาน

| คอลัมน์ | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `id` | auto | PK |
| `name` | — | ชื่องาน (unique) chain อ้างถึงกันด้วยชื่อ จึงเปลี่ยนชื่อไม่ได้ |
| `job_type` | — | `command` / `powershell` / `python` / `sql` / `http` |
| `params` | `{}` | JSON ของ params ตามชนิดงาน |
| `queue` | `default` | คิวที่งานนี้อยู่ |
| `priority` | 5 | ค่ามากรันก่อน |
| `timeout_sec` | 3600 | 0 = ไม่จำกัด |
| `max_retry` | 0 | จำนวนครั้งที่ retry ได้ |
| `retry_delay_sec` | 60 | เวลารอก่อน retry ครั้งแรก |
| `retry_backoff` | 2.0 | ตัวคูณเวลารอของ retry ครั้งถัดไป |
| `dedup_policy` | `skip` | `allow` / `skip` / `coalesce` |
| `missed_policy` | `run_once` | `skip` / `run_once` / `run_all` |
| `after_job` | NULL | ชื่องานที่ต้องจบก่อน (chain) |
| `after_on` | `success` | `success` / `failure` / `always` |
| `enabled` | 1 | 0 = ปิด |
| `description`, `created_at` | | คำอธิบาย และเวลาสร้าง |

### `schedule` — ตารางเวลา (หลายแถวต่อ job, `ON DELETE CASCADE`)
`kind` (`cron`/`every`/`at`/`startup`), `expr`, `next_run_at`, `last_fire_at`, `enabled`

### `job_run` — แต่ละรอบการรัน (ไม่มี FK ไปที่ `job_def` เพื่อเก็บประวัติไว้ได้แม้ลบงานแล้ว)

| คอลัมน์ | ความหมาย |
|---|---|
| `job_id`, `job_name`, `queue`, `priority` | สำเนาข้อมูลจาก `job_def` ณ เวลาที่เข้าคิว |
| `status` | ดู[หัวข้อ 5](#5-วงจรชีวิตของ-run) |
| `trigger` | `manual`, `cron`, `every`, `at`, `startup`, `retry`, `after:<JOB>#<RUN_ID>` |
| `attempt`, `retry_of` | ครั้งที่ของการลอง และ run ก่อนหน้าที่ retry มา |
| `scheduled_at` | เวลาที่ควรรัน (retry ใช้ค่าเดิมของรอบแรก) |
| `not_before` | ห้ามหยิบก่อนเวลานี้ (ใช้กับ retry) |
| `started_at`, `finished_at`, `heartbeat` | เวลาเริ่ม เวลาจบ และสัญญาณชีพล่าสุด (อัปเดตทุก 5 วินาที) |
| `pid`, `exit_code`, `error` | pid ของโปรเซสลูก, exit code และข้อความ error |
| `cancel_requested` | 1 = ผู้ใช้สั่ง cancel ระหว่างรัน |

index: `(queue, status, priority, scheduled_at, id)` สำหรับการหยิบงาน และ `(job_id, status)` สำหรับ dedup

### ตารางอื่น
- `run_log(id, run_id, ts, level, message)` index `(run_id, id)` โดย `level` เป็น `INFO` / `WARN` / `OUT` (output ของงาน)
- `queue_state(name, paused)` เก็บสถานะ pause ของแต่ละคิว
- `engine_lock(queue, host, pid, heartbeat, stop_requested)` เก็บ engine ที่ถือคิวอยู่ ส่วน `stop_requested=1` คือมีคำขอให้ engine ตัวนั้นหยุด
  (`acquire_lock` ใช้ `INSERT OR REPLACE` engine ตัวใหม่จึงเริ่มด้วย 0 เสมอ)

เวลาทั้งหมดเก็บเป็นข้อความ `YYYY-MM-DD HH:MM:SS` ตามเวลาท้องถิ่นแบบไม่มี timezone จึงเปรียบเทียบใน SQL แบบ string ได้โดยตรง

## 5. วงจรชีวิตของ run

```
            enqueue (dedup: skip)
   ┌──────────────────────────────► Skipped
   │
 (สร้าง) ──► Pending ──claim_next──► Running ──exit 0──────────────► Succeeded ──► trigger chain (success/always)
              │  ▲                   │
   cancel ────┤  │ graceful shutdown │──cancel_requested───────────► Cancelled
   (Pending)  ▼  └───────────────────┤
          Cancelled                  │──ล้มเหลว, attempt <= max_retry ─► Failed ──► สร้าง run ใหม่ (Pending, retry)
                                     │──ล้มเหลว, หมดสิทธิ์ retry ─────► Dead ─────► trigger chain (failure/always)
                                     └──job ถูก disable (trigger ≠ manual) ─► Skipped
```

"ล้มเหลว" ในที่นี้หมายถึง exit code ≠ 0, timeout, เริ่มโปรเซสไม่ได้, config error, `engine lost` (การกู้คืน) และ `engine error`
ถ้านิยามงานถูกลบไปแล้ว run จะจบเป็น `Dead` ทันทีด้วยข้อความ `definition deleted`

## 6. คิว: enqueue, dedup, claim

### `_enqueue(con, job, trigger, ...)` (ผู้เรียกต้องถือ transaction อยู่แล้ว)
- trigger `manual` และ `retry` ข้าม dedup เสมอ
- `skip`: ถ้ามีงานเดียวกันเป็น `Pending` อยู่ จะเพิ่มแถว `Skipped` พร้อม error `dedup(skip): มีงานรออยู่แล้ว #<id>` เพื่อให้เห็นในประวัติ
- `coalesce`: เพิ่มแถวใหม่แล้วตั้งแถว `Pending` เดิมทั้งหมดเป็น `Skipped` พร้อม error `coalesced by #<newid>`
- dedup ดูเฉพาะสถานะ `Pending` เท่านั้น งานที่กำลัง `Running` ไม่นับ

### `claim_next(con, queue)`
1. pre-check ด้วย SELECT ธรรมดา (ถ้าคิว pause อยู่หรือไม่มีงานให้หยิบ จะไม่ต้องขอ write lock)
2. `BEGIN IMMEDIATE` แล้วตรวจ pause และเลือกงานซ้ำอีกรอบ:
   ```sql
   SELECT * FROM job_run
   WHERE queue=? AND status='Pending' AND (not_before IS NULL OR not_before <= now)
   ORDER BY priority DESC, scheduled_at ASC, id ASC LIMIT 1
   ```
3. เปลี่ยนเป็น `Running` และตั้ง `started_at` กับ `heartbeat`

เพราะ retry ใช้ `not_before` งานอื่นจึงแซงงานที่รอ retry ได้ คิวจึงไม่ติด

### Cancel (`cancel_run`)
`Pending` → `Cancelled` ทันที; `Running` → ตั้ง `cancel_requested=1` แล้ว monitor loop ของ engine จะเห็นภายใน 2 วินาทีและ kill process tree

## 7. Engine

`run_engine(queue, poll)` ทำงานตามลำดับนี้

1. `setup_engine_logging` เขียน log ลง stdout (ถ้ามี console) และ `logs/engine-<queue>.log` (RotatingFileHandler ขนาด 5 MB เก็บ 5 ไฟล์)
2. **`acquire_lock`** ภายใน `BEGIN IMMEDIATE` อ่าน `engine_lock` ถ้ามี engine อื่นที่ heartbeat สดกว่า 30 วินาที จะออกด้วย exit code 3
   ถ้า heartbeat เก่ากว่านั้น (stale) จะยึด lock ต่อพร้อม log WARN
3. ติดตั้ง signal handler สำหรับ `SIGINT`, `SIGTERM`, `SIGBREAK` (Windows)
4. **`recover`** จัดการงานของคิวนี้ที่ค้าง `Running` (engine ก่อนหน้าตาย): log WARN, ถ้า pid ยังมีชีวิตจะ WARN ให้ตรวจสอบเอง
   (**ไม่ kill ให้** เพราะเลข pid อาจถูกนำไปใช้ซ้ำ) แล้ว `finalize(..., "engine lost")` ตามกติกา retry ปกติ
5. **`fire_startup`** นำงานที่มี schedule แบบ `startup` เข้าคิว
6. เริ่ม **`SchedulerThread`** ซึ่งใช้ connection ของตัวเอง ทุก 1 วินาทีจะเรียก `check_stop` และ `scheduler_tick`, ทุก 5 วินาทีเรียก `heartbeat_lock`
   และทุกชั่วโมงเรียก `prune`
7. **worker loop** ใน main thread เรียก `process_one` ซ้ำ ๆ ถ้าไม่มีงานจะรอ `poll` วินาที

ต้องแยก scheduler ไว้คนละ thread เพื่อให้ schedule ยัง fire ได้ระหว่างที่ worker รันงานยาว (SPEC §9 #10)

**Lock ถูกแย่ง:** `heartbeat_lock` อัปเดตด้วย `WHERE queue=? AND pid=? AND host=?` ถ้า rowcount = 0 แปลว่ามี engine อื่นยึดไปแล้ว
engine จะหยุดตัวเองและออกด้วย exit code 4 (ไม่ลบ lock ของคนอื่น)

**Graceful shutdown:** เมื่อได้รับ signal จะตั้ง `stop` event ทำให้ worker ไม่หยิบงานใหม่ ถ้ามีงานรันอยู่จะ kill process tree
แล้ว `requeue` งานกลับเป็น `Pending` (ล้าง `started_at`/`pid` และไม่นับเป็น attempt) พร้อม WARN จากนั้นรอ scheduler thread จบและลบแถว lock

**`stop-engine`:** engine ที่ Task Scheduler เปิดด้วย `pythonw.exe` ไม่มี console จึงรับ Ctrl+C หรือ CTRL_BREAK ไม่ได้
ทางเดียวที่จะหยุดแบบ graceful คือผ่าน DB:
`request_engine_stop` ตั้ง `engine_lock.stop_requested=1` (ถ้าไม่มี lock หรือ heartbeat เก่ากว่า 30 วินาที จะ raise `OrchError`)
แล้ว `check_stop` ใน scheduler thread อ่าน flag ของ lock ตัวเอง (`engine_stop_requested`, `WHERE queue/pid/host`) ทุก tick
ถ้าเจอจะ log `stop requested (stop-engine)` แล้วตั้ง `stop` event จึงเข้าเส้นทาง graceful shutdown เดิมทั้งหมด (exit code 0)
ส่วน `cmd_stop_engine` รอจนแถว lock ของ engine ตัวนั้น (host/pid เดิม) หายไป ถ้าเกิน `--wait` จะ exit 2

**การปิดแบบกะทันหัน** (End ใน Task Scheduler, `Stop-ScheduledTask`, kill ใน Task Manager, reboot) ใช้ `TerminateProcess`
engine จึงไม่มีโอกาส requeue งานที่รันอยู่ งานนั้นจะถูก `recover` ตั้งเป็น `engine lost` ตอนเปิดครั้งถัดไป ส่วนโปรเซสของงาน
ซึ่งอยู่ใน process group และ Job Object ของตัวเอง (ไม่มี KILL_ON_JOB_CLOSE) อาจรันต่อ

**Engine error:** ถ้าเกิด exception ที่ไม่คาดคิดระหว่างรันงาน (เช่น DB ใช้ไม่ได้ชั่วคราว) `_supervise` จะ kill process tree ก่อน
แล้ว `process_one` ปิด run ด้วย `engine error: <รายละเอียด>` ตามกติกา retry คิวจึงไม่มีงานรันซ้อนกัน

## 8. Executor: การรันโปรเซสลูก

### `build_command(job_type, params)` → `{cmd, shell, env, cwd, encoding}`

| ชนิด | คำสั่งที่รันจริง | encoding เริ่มต้น |
|---|---|---|
| `command` | `params.command` ผ่าน shell (`cmd.exe /c` บน Windows) | OEM code page (`GetOEMCP`, เช่น `cp874`) |
| `powershell` | `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <file> <args>` หรือ `-Command <command>` | OEM code page |
| `python` | `sys.executable -u <file> <args>` + `PYTHONIOENCODING=utf-8` | utf-8 |
| `sql`, `http` | `sys.executable -u orch.py _exec <type>` + `ORCH_PARAMS=<json>` | utf-8 |

- `env` ใน params จะถูกรวมทับ environment ของ engine ส่วน `encoding` ใน params ใช้ override การ decode output
- `args`: ถ้าเป็น list จะส่งเป็น argv ทีละตัว ถ้าเป็น string บน Windows จะต่อท้าย command line ตามที่พิมพ์มา
  (บน POSIX ใช้ `shlex.split`)
- บน Windows ถ้ามี `pythonw.exe` อยู่ ตัวที่ Task Scheduler เรียกคือ `pythonw.exe` แต่ตัวที่ใช้รันงาน `python` คือ interpreter ตัวเดียวกับ engine

### `execute_process` / `_supervise`
- `Popen(stdin=DEVNULL, stdout=PIPE, stderr=STDOUT)` แบบ bytes บน Windows ใช้ `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`
  บน POSIX ใช้ `start_new_session=True`
- บน Windows จะใส่โปรเซสลูกลงใน **Job Object** (`WinJob`) ถ้าสร้างไม่ได้จะ log WARN แล้วรันต่อโดยไม่มี Job Object
- **reader thread** อ่าน output ทีละบรรทัด decode แล้วใส่ `queue.Queue` โดยไม่แตะ DB เอง จำกัด 5,000 บรรทัดต่อรอบ
  และ 2,000 ตัวอักษรต่อบรรทัด ถ้าเกินจะ WARN ครั้งเดียว แต่ยังอ่านทิ้งต่อไปจนหมด เพื่อไม่ให้โปรเซสลูกค้างเพราะ pipe เต็ม
- **monitor loop** ทุก 0.25 วินาที: เขียน log ที่ค้างอยู่ลง `run_log` เป็น batch ใน transaction เดียว ตรวจ stop event, timeout,
  `cancel_requested` (ทุก 2 วินาที) และอัปเดต `heartbeat` (ทุก 5 วินาที)
- เมื่อโปรเซสจบจะรอ reader thread สูงสุด 5 วินาที ถ้ายังมีโปรเซสหลานถือ pipe ไว้ จะ WARN แล้วเลิกอ่าน

### `kill_tree(pid, proc, job)`
- **Windows:** `taskkill /PID <pid> /T /F` ตาม SPEC แล้วตามด้วย `TerminateJobObject`
  `taskkill /T` ตาม tree จาก parent pid จึงหาโปรเซสหลานที่ตัวกลางจบไปก่อนไม่เจอ แต่ Job Object ยังเก็บโปรเซสนั้นไว้
  Job Object ไม่ได้ตั้ง `KILL_ON_JOB_CLOSE` ดังนั้นงานที่จบปกติ หรือ engine ที่ตายไป จะไม่ทำให้โปรเซสที่เหลืออยู่ถูกฆ่า
- **POSIX:** `killpg(SIGTERM)` รอ 3 วินาที แล้ว `killpg(SIGKILL)` ซ้ำเสมอ แม้ leader จะจบไปแล้ว

## 9. Finalize, retry และ dependency chain

`finalize(con, run_id, outcome, exit_code, error)` ทำงานใน transaction เดียว

| ผลลัพธ์ | สถานะ |
|---|---|
| `exited` + exit 0 | `Succeeded` |
| `cancelled` | `Cancelled` |
| อื่น ๆ และ `attempt <= max_retry` | `Failed` + สร้าง run ใหม่: `attempt+1`, `trigger='retry'`, `retry_of`, `scheduled_at` เดิม, `not_before = now + retry_delay_sec × retry_backoff^(attempt-1)` |
| อื่น ๆ และหมดสิทธิ์ retry | `Dead` |

ตัวอย่าง: `--retry 2 --retry-delay 5` (backoff 2) → รอ 5 วินาที แล้ว 10 วินาที รวมทั้งหมด 3 รอบ

**Chain:** เมื่อ run จบเป็น `Succeeded` หรือ `Dead` จะหางานที่ `after_job = ชื่องานนี้` แล้วเข้าคิวตาม `after_on`
(`success` ↔ Succeeded, `failure` ↔ Dead, `always` ↔ ทั้งคู่) ด้วย trigger `after:<ชื่องาน>#<run_id>`
`Cancelled` และ `Failed` (ที่ยังจะ retry ต่อ) ไม่ trigger งานที่ถูก trigger ผ่าน dedup ของตัวเองตามปกติ และอยู่คนละคิวได้

## 10. Scheduler

### cron (`CronExpr`)
- 5 ช่อง `นาที ชั่วโมง วันที่ เดือน วันในสัปดาห์` รองรับ `*`, list (`1,15`), range (`1-5`), step (`*/5`, `1-10/2`, `5/10` = ตั้งแต่ 5 ไปจนสุดช่วงทีละ 10)
- วันในสัปดาห์ 0–7 (0 และ 7 = อาทิตย์) ยังไม่รองรับชื่อ (`MON`, `JAN`)
- ถ้าช่อง dom และ dow ถูกระบุทั้งคู่ (ไม่ได้ขึ้นต้นด้วย `*`) จะใช้ **OR** ตามมาตรฐาน cron
- `next_after` ข้ามทั้งเดือน วัน หรือชั่วโมงที่ไม่ตรง จึงไม่ต้องวนทีละนาที ถ้าหาไม่เจอภายใน 5 ปี (เช่น `0 0 31 2 *`) จะ raise error

### every / at / startup
- `every`: `30s`, `5m`, `2h`, `1d` หรือจำนวนวินาที รอบถัดไป = รอบก่อน + interval (anchor) เวลาจึงไม่เลื่อนสะสม
- `at`: `YYYY-MM-DD HH:MM[:SS]` ถ้าเป็นเวลาในอดีตตอนสร้างจะ error หลัง fire แล้วจะตั้ง `next_run_at=NULL, enabled=0`
- `startup`: fire โดย `fire_startup` ตอน engine ของคิวนั้นเริ่ม

### Missed schedule (`_fire_schedule`) ทำต่อ schedule ที่ `next_run_at <= now` ในแต่ละ tick
1. เก็บเวลา fire ทั้งหมดที่ `<= now` (จำกัด 1,000 รอบ ถ้าเกินจะคำนวณ next ใหม่จาก now)
2. แบ่งเป็น on-time (ช้าไม่เกิน 90 วินาที) กับ missed
3. enqueue on-time ทั้งหมด + missed ตาม policy (`skip` = ไม่เอา, `run_once` = เอาตัวสุดท้าย, `run_all` = เอาทั้งหมด) โดยใช้ `scheduled_at` = เวลา fire เดิม
4. อัปเดต `next_run_at` และ `last_fire_at` (ถ้าเป็น `at` จะตั้ง `enabled=0` ด้วย)

schedule ของงานที่ถูก disable จะไม่ fire เมื่อเปิดงานกลับ (`set_enabled`) schedule แบบ cron/every ที่ `next_run_at` เลยไปแล้วจะถูกคำนวณใหม่จากเวลาปัจจุบัน
ช่วงที่ปิดงานไว้จึงไม่นับเป็นรอบที่พลาด

### Prune
`prune(days)` ลบ `job_run` ที่จบแล้วและ `finished_at` เก่ากว่า N วัน พร้อม `run_log` ของ run นั้นใน transaction เดียว

## 11. งานชนิด sql และ http (`_exec`)

`orch.py _exec sql|http` ถูกดักไว้ใน `main()` ก่อนเข้า argparse จึงไม่แสดงใน `--help`
params ถูกส่งมาทางตัวแปร `ORCH_PARAMS` แล้วตรวจอีกครั้งด้วย `validate_params` exit code: 0 = สำเร็จ, 1 = ล้มเหลวระหว่างรัน, 2 = config ผิด

### sql (`exec_sql`)
- `conn`: `env:VAR` อ่านจาก environment ตอนรัน (`resolve_secret`), `sqlite:<path>` หรือ ODBC connection string (ใช้ `pyodbc`)
- `query` หรือ `query_file` (อ่านเป็น utf-8, รองรับ BOM) ถ้าพบบรรทัด `GO` จะ error ทันที
- sqlite: แยก statement ด้วย `sqlite3.complete_statement` (`;` ใน string ไม่นับเป็นตัวแยก) แล้วรันใน transaction เดียว
- ODBC: `pyodbc.connect(autocommit=False)` รันทั้ง batch แล้ววนอ่านผลทุกชุดด้วย `nextset()`
- พิมพ์ผลลัพธ์แบบคั่นด้วย tab (สูงสุด 200 แถวต่อชุด) และ `N row(s) affected` จากนั้น commit ถ้าเกิด error จะ rollback แล้ว exit 1

### http (`exec_http`)
- `method` ค่าเริ่มต้นเป็น GET (หรือ POST ถ้ามี `body`) ถ้า `body` เป็น object/list จะส่งเป็น JSON และตั้ง `Content-Type` ให้เอง
- `expect`: `"200,201"`, `"2xx"`, `"200-299"` ค่าเริ่มต้นคือ 2xx ถ้า status อยู่นอกช่วงจะ exit 1
- `timeout` ค่าเริ่มต้น 30 วินาที ถ้า URL มี `user:password@` จะแปลงเป็น header `Authorization: Basic` (urllib ไม่รองรับ userinfo ใน URL)
- log แสดงบรรทัด `GET <URL ที่ตัด userinfo และ query string>`, `HTTP <status> <reason>` และ body สูงสุด 4,000 ตัวอักษร
- ใช้ proxy ของระบบตามปกติของ `urllib` ถ้าต้องการปิด ใส่ `env: {"no_proxy": "*"}`

## 12. SQLite และ concurrency

- **ทุก thread มี connection ของตัวเอง** ได้แก่ worker (main), scheduler thread และ GUI (main thread ของ tkinter)
  ส่วน reader thread ไม่แตะ DB
- `connect()` ตั้ง `isolation_level=None` (autocommit), `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=30000`, `foreign_keys=ON`
- การเขียนทุกครั้งต้องทำผ่าน `with tx(con):` (`BEGIN IMMEDIATE`) โดย `tx` ซ้อนกันไม่ได้
  ฟังก์ชันที่ขึ้นต้นด้วย `_` (`_enqueue`, `_fire_schedule`) จึงถือว่าผู้เรียกถือ transaction อยู่แล้ว
- **`_Cursor.fetchone()` ปิด cursor หลังอ่าน:** SELECT ที่อ่านไม่สุดจะค้าง read transaction ไว้ ทำให้ connection ที่เปิดนานติดอยู่กับ
  snapshot เก่าของ WAL และมองไม่เห็นการเขียนจากโปรเซสอื่น
- `claim_next` และ `scheduler_tick` ทำ pre-check แบบอ่านอย่างเดียวก่อน ตอนที่คิวว่างจึงไม่ขอ write lock
- ผล stress test (`tests/test_12_stress.py`): engine 3 คิว + ตัวอ่านแบบ GUI ทุก 0.2 วินาที + CLI เขียนต่อเนื่อง 30 วินาที
  ไม่พบ `database is locked` ไม่มีงานในคิวเดียวกันรันซ้อนกัน และ output ไม่หาย

## 13. ความปลอดภัยและความลับ

- ไม่มีการพิมพ์ params ทั้งก้อนลง log (log ของ run มีแค่ชื่องาน, attempt, trigger และ output)
- params ของ `sql`/`http` ส่งให้โปรเซสลูกทาง environment (`ORCH_PARAMS`) ไม่ผ่าน argv จึงไม่ปรากฏใน process list
- `sql` ไม่พิมพ์ connection string และแนะนำให้ใช้ `env:VAR`
- `http` ตัด userinfo และ query string ออกจาก URL ใน log
- **ข้อควรระวัง:** params เก็บเป็นข้อความธรรมดาใน `orch.db` และ `query --json` แสดง params ทั้งหมด ควรจำกัดสิทธิ์การเข้าถึงไฟล์ DB
- งานทุกตัวในคิวรันด้วยสิทธิ์ของ account ที่เปิด engine (ถ้าเป็น boot task ในนาม SYSTEM ก็ได้สิทธิ์ SYSTEM)

## 14. GUI

- `App.refresh()` อ่านข้อมูลหนึ่งรอบ ส่วน `_tick()` เป็น loop ของ `after(2000)` การเรียก `refresh()` หลังบันทึกจึงไม่สร้าง loop ซ้ำ
- `sync_tree()` อัปเดต Treeview **ในที่เดิม** โดยใช้ iid คงที่ (`job-<id>` หรือเลข run id) แถวที่เลือกไว้และตำแหน่ง scroll จึงไม่หาย
- `App.call()` เรียกฟังก์ชันใน `orch.py` แล้วแปลง `OrchError` และ `sqlite3.Error` (เช่น database is locked) เป็น `App.error()`
  ทั้ง `App.error` และ `App.confirm` override ได้ใน test
- ปุ่ม **Stop engine** (แท็บ Queue / Runs) ใช้ `target_queue()` แล้วถามยืนยันก่อนเรียก `request_engine_stop`
  แถบบนแสดง `(stopping)` จาก `engine_states()[...]["stopping"]
- `JobDialog`: เปลี่ยน template ของ params ตามชนิดงานเฉพาะเมื่อผู้ใช้ยังไม่ได้แก้ params
  ตอนแก้ไขงาน ถ้าข้อความ schedule ไม่เปลี่ยนจะส่ง `schedules=None` จึงไม่รีเซ็ต `next_run_at`
- แท็บ Log ต่อท้ายเฉพาะบรรทัดใหม่ (`get_logs(after_id)`) และหยุดดึงเมื่อ run จบแล้ว

## 15. การลงทะเบียนกับ Task Scheduler

`build_startup_script(queue, trigger, user)` สร้างสคริปต์ PowerShell:

- `New-ScheduledTaskAction -Execute <pythonw.exe> -Argument '"<orch.py>" --db "<orch.db>" engine --queue <Q>'`
  โดยใส่ `--db` เป็น path เต็มเสมอ engine ที่ Task Scheduler เปิด (เช่นในนาม SYSTEM) จึงใช้ DB ไฟล์เดียวกัน
- `New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries`
- trigger ที่สอง `$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)`
  (ไม่ใส่ `-RepetitionDuration` จึงทำซ้ำไม่สิ้นสุด ทดสอบบน PowerShell 5.1 / Windows 10 แล้ว) และลงทะเบียนด้วย `-Trigger @($trigger, $repeat)`
  เหตุผลคือ restart-on-failure (`RestartCount`) ไม่ครอบคลุมกรณีโปรเซสที่เริ่มสำเร็จแล้ว crash หรือถูก kill
  ส่วนการเปิดซ้ำระหว่างที่ engine ยังรันอยู่ไม่มีผลเสีย เพราะ `MultipleInstances IgnoreNew` ทำให้ Task Scheduler ไม่เปิดตัวที่สอง
  และถ้ามีตัวที่สองหลุดมาได้ engine lock ก็จะทำให้มันออกด้วย exit code 3
- `boot`: `-AtStartup` + principal `SYSTEM` (ServiceAccount, Highest) หรือ `--user` ซึ่งจะถามรหัสผ่านด้วย `Get-Credential` ตอนรัน
- `logon`: `-AtLogOn -User <user>` + principal แบบ Interactive
- ชื่อ task คือ `Orch Engine (<queue>)` ถ้าไม่ใส่ `--apply` จะแค่พิมพ์สคริปต์ออกมา ถ้าใส่จะเขียนเป็นไฟล์ `.ps1` ชั่วคราว รันด้วย
  `powershell -ExecutionPolicy Bypass -File` แล้วลบไฟล์ทิ้ง

ต้องใช้ `Register-ScheduledTask` เพราะ task ที่สร้างด้วย `schtasks` ถูกตั้ง *Stop the task if it runs longer than 72 hours* ไว้เป็นค่าเริ่มต้น

### ตัวติดตั้ง (`setup.bat` → `setup.ps1`)

`setup.bat` ตรวจสิทธิ์ด้วย `fltmc` ถ้ายังไม่ใช่ Administrator จะเรียกตัวเองใหม่ด้วย `Start-Process -Verb RunAs`
จากนั้นรัน `setup.ps1` ด้วย `-ExecutionPolicy Bypass` ตัวสคริปต์ไม่มีตรรกะของ Orch เอง แต่เรียกคำสั่งที่มีอยู่แล้วใน `orch.py` เท่านั้น
(`init`, `stop-engine`, `install-startup --apply`) โดยมีขั้นตอนดังนี้

1. **หา Python** จาก `HKLM:\SOFTWARE\Python\PythonCore\<ver>\InstallPath` (เวอร์ชัน ≥ 3.10 ที่สูงที่สุด)
   - ใช้ HKLM เท่านั้น เพราะ Python แบบรายผู้ใช้ลงทะเบียนใน HKCU และ engine ที่รันในนาม SYSTEM ใช้ไม่ได้
   - ถ้าไม่พบ จะลงด้วย `python-3.*-amd64.exe` ที่วางไว้ข้างสคริปต์ หรือ winget (`--scope machine`) ด้วย `InstallAllUsers=1 PrependPath=1`
2. **pyodbc** ติดตั้งด้วย `pip install --find-links <โฟลเดอร์ setup>` แล้วตรวจว่า `pyodbc.__file__` อยู่ใต้ `sys.prefix` (ไม่ใช่ user site)
   ถ้าล้มเหลวจะเตือนแต่ติดตั้งต่อ
3. **อัปเดต:** ถ้ามี task อยู่แล้ว จะ `Disable-ScheduledTask` เพื่อกันไม่ให้ trigger ที่ทำซ้ำเปิด engine ใหม่ระหว่างคัดลอก
   ตามด้วย `stop-engine --wait 60` และ `Stop-ScheduledTask` เผื่อกรณีที่ engine ไม่หยุด
4. **คัดลอก** ด้วย `robocopy /E` (ไม่ใช้ `/MIR` จึงไม่ลบอะไรที่ปลายทาง) โดยยกเว้น `.git __pycache__ logs backup dist` และ `orch.db* python-*.exe *.whl`
   **จำกัดสิทธิ์** (`Set-InstallAcl`): `icacls <dir> /reset` แล้ว `/inheritance:r /grant:r` ให้ SYSTEM และ Administrators = F,
   Users = RX, account ที่รัน setup และ `-AllowUser` = M (ใช้ SID ของกลุ่ม จึงไม่ขึ้นกับภาษาของ Windows) ตามด้วย `icacls <dir>\* /reset /T`
   เพื่อให้ไฟล์ข้างในสืบทอดอย่างเดียว เหตุผล: โฟลเดอร์ใหม่ใต้ `C:\` สืบทอด `Authenticated Users: Modify` มา ทำให้ผู้ใช้ทั่วไปแก้ `orch.py`
   หรือเพิ่มงานใน `orch.db` ให้ engine แบบ boot รันในนาม SYSTEM ได้ (privilege escalation) ผลข้างเคียงคือผู้ใช้ที่ไม่มีสิทธิ์เขียนเปิด DB ไม่ได้
   (SQLite WAL ต้องเขียนไฟล์ `-shm` แม้แค่อ่าน) `uninstall` ไม่คืนสิทธิ์เดิม ทดสอบใน `test_11_install.test_install_acl`
5. `init` ซึ่งสร้าง DB ใหม่หรือ migrate schema เดิม
6. `install-startup --apply` ซึ่งใช้ `-Force` จึงแทนที่ task เดิมและ task กลับเป็น enabled จากนั้น `Start-ScheduledTask`
   แล้วรอไม่เกิน 30 วินาทีจน `engine_states()` บอกว่า running และตรวจว่า task มี `PT0S` และ trigger 2 ตัว
7. สร้าง shortcut ใน Desktop และ Start menu ของ all users (`pythonw.exe "<dir>\orch_gui.py" --db "<dir>\orch.db"`)
8. เพิ่มโฟลเดอร์ติดตั้งและโฟลเดอร์ Python ลง machine PATH โดยเขียน registry ตรง ๆ แบบ `REG_EXPAND_SZ` ที่ยังไม่ขยาย `%VAR%`
   (`setx` ตัด PATH ที่ยาวเกิน 1024 ตัวอักษร) แล้วส่ง `WM_SETTINGCHANGE`

ทุกขั้นถูกบันทึกด้วย `Start-Transcript` ลง `<dir>\logs\setup-<เวลา>.log` ส่วน output ของโปรแกรมภายนอกส่งผ่าน pipeline เพื่อให้ถูกบันทึกด้วย
ถ้าล้มระหว่างอัปเดต จะ enable และ start task เดิมกลับให้

`uninstall.ps1` จัดการเฉพาะ task (`Orch Engine (*)`) และ shortcut ที่อ้างถึงโฟลเดอร์ของตัวเองเท่านั้น จึงไม่กระทบ Orch ที่ติดตั้งไว้ที่อื่น
มันจะหยุด engine แบบปลอดภัย, ลบ task, ไอคอน (ทุกชื่อที่เปิด `orch_gui.py` ของโฟลเดอร์นั้น) และ PATH ของโฟลเดอร์นั้น
แต่ไม่ลบโฟลเดอร์และ `orch.db` โดยบันทึก log ไว้ที่ `<dir>\logs\uninstall-<เวลา>.log`
(ไม่เอาโฟลเดอร์ Python ออกจาก PATH เพราะโปรแกรมอื่นอาจใช้)
`package.bat` ใช้ `git archive HEAD` จึงได้เฉพาะไฟล์ที่ commit แล้ว

## 16. ค่าคงที่และ exit code

| ค่าคงที่ | ค่า | ความหมาย |
|---|---|---|
| `LOG_MAX_LINES` / `LOG_MAX_CHARS` | 5000 / 2000 | จำนวนบรรทัด output สูงสุดต่อรอบ / จำนวนตัวอักษรสูงสุดต่อบรรทัด |
| `MONITOR_INTERVAL` | 0.25 s | รอบของ monitor loop |
| `HEARTBEAT_EVERY` | 5 s | ความถี่ในการอัปเดต heartbeat ของ run |
| `CANCEL_CHECK_EVERY` | 2 s | ความถี่ในการตรวจ `cancel_requested` |
| `POSIX_KILL_GRACE` | 3 s | เวลารอระหว่าง SIGTERM กับ SIGKILL |
| `MISSED_GRACE_SEC` | 90 s | ช้าไม่เกินนี้ถือว่ายังตรงเวลา |
| `MISSED_CAP` | 1000 | จำนวนรอบที่พลาดสูงสุดที่จะไล่คำนวณ |
| `PRUNE_DEFAULT_DAYS` | 30 | อายุประวัติการรันก่อนถูกลบ |
| `LOCK_STALE_SEC` | 30 s | heartbeat ของ lock เก่ากว่านี้ถือว่า engine ตายแล้ว |
| `SchedulerThread.TICK` / `PRUNE_EVERY` | 1 s / 3600 s | ความถี่ของ scheduler / prune |
| `EXEC_MAX_ROWS` / `HTTP_MAX_BODY_CHARS` | 200 / 4000 | แถว SQL ที่พิมพ์ต่อชุด / ความยาว body ของ http ที่ log |
| `ENGINE_RELAUNCH_MIN` | 5 นาที | ช่วงของ trigger ที่ทำซ้ำใน install-startup |
| `REFRESH_MS` / `RUNS_LIMIT` (GUI) | 2000 / 200 | ความถี่ refresh / จำนวน run ที่แสดง |

| Exit code | ความหมาย |
|---|---|
| 0 | สำเร็จ |
| 2 | input ผิด หรือหา job/run ไม่เจอ (`OrchError` และ error ของ argparse) รวมถึง `stop-engine` ที่ไม่มี engine รันอยู่ หรือ engine ไม่หยุดภายใน `--wait` |
| 3 | `engine`: มี engine ของคิวนี้รันอยู่แล้ว |
| 4 | `engine`: lock ถูก engine อื่นยึดไประหว่างทำงาน |

## 17. การทดสอบ

- ใช้ `unittest` (stdlib) รันด้วย `python -m unittest discover -s tests -p "test_*.py"` (138 test ใช้เวลาประมาณ 70 วินาที)
- `tests/helpers.py`: `DBTestCase` สร้างโฟลเดอร์ชั่วคราวและ `ORCH_DB` แยกให้แต่ละ test มี `cli()`, `start_engine()` (เปิด engine เป็น subprocess จริง),
  `stop_engine()` (CTRL_BREAK หรือ SIGTERM), `wait_until()` และ `py_cmd()`
- งานใน test ใช้ `sys.executable -c "..."` จึงรันได้ทุก OS
- test ของ GUI ขับ `App` โดยตรงโดยไม่เรียก `mainloop` และจะถูกข้ามถ้าไม่มีหน้าจอ
- ตัวแปรแวดล้อม: `ORCH_TEST_TMP` (เปลี่ยนโฟลเดอร์ชั่วคราว), `ORCH_STRESS=1` (เปิด stress test), `ORCH_STRESS_SECONDS`
- ตารางจับคู่ SPEC §9 #1–#13 กับชื่อ test อยู่ใน [README.md](../README.md#ทดสอบ)

| ไฟล์ | ครอบคลุม |
|---|---|
| `test_01_foundation` | schema, WAL, `tx`, connection ต่อ thread, snapshot ของ connection ที่เปิดนาน, time helpers |
| `test_02_schedule` | cron (`*/5`, คร่อมเดือนและปี, dom+dow OR, 29 ก.พ.), every, at |
| `test_03_jobs` | CRUD, validation, การตรวจ cycle, CLI create/query/change/delete/enable/disable |
| `test_04_queue` | ลำดับคิว, dedup, pause, cancel ตอน Pending |
| `test_05_executor` | build_command, การจับ output, จำกัดความยาว, timeout, kill tree (รวมโปรเซสหลานที่หลุด tree), cancel, heartbeat |
| `test_06_finalize` | retry/backoff, chain, config error, engine error |
| `test_07_scheduler` | missed policy, cap, at, startup, scheduler thread, prune |
| `test_08_engine` | engine subprocess: lock, recovery, shutdown, schedule ระหว่างงานยาว, Test Plan #2/#4/#7/#11 |
| `test_09_exec` | sql (sqlite) และ http (server ภายในเครื่อง) |
| `test_10_gui` | ตาราง, refresh ในที่เดิม, ปุ่ม, dialog |
| `test_11_install` | สคริปต์ install-startup, `--db`, ไฟล์ .bat, ภาษาไทยผ่าน cmd/PowerShell/Python, engine ผ่าน pythonw, setup/uninstall.ps1 (BOM, parse, ปฏิเสธเมื่อไม่ใช่ Administrator) |
| `test_12_stress` | lock contention (เฉพาะเมื่อตั้ง `ORCH_STRESS=1`) |

## 18. ส่วนที่เพิ่มหรือตีความจาก SPEC

| เรื่อง | พฤติกรรม | เหตุผล |
|---|---|---|
| ลบงานที่มีงานอื่นอ้างถึงใน `after_job` | ปฏิเสธ | SPEC ไม่ได้กำหนดไว้ และการปล่อยให้ chain ชี้ไปที่งานที่ไม่มีอยู่แย่กว่า |
| เปิดงานกลับหลัง disable | คำนวณ `next_run_at` ใหม่จากเวลาปัจจุบัน (ทั้ง `set_enabled` และ `update_job` เช่น checkbox ใน dialog ของ GUI) | ช่วงที่ปิดงานไว้ไม่ควรนับเป็นรอบที่พลาด |
| Windows Job Object | ใช้ร่วมกับ `taskkill` ตอน kill | ผู้ใช้อนุมัติหลังพบความเสี่ยงตาม §11 (โปรเซสหลานหลุดจาก tree) |
| Engine error | kill tree แล้ว finalize เป็นล้มเหลว | รักษาการรันทีละงาน (§2) |
| `stop-engine` + `engine_lock.stop_requested` (schema v2) | หยุด engine แบบ graceful ผ่าน DB | engine ที่ติดตั้งไว้ (pythonw) รับ signal ไม่ได้ ผู้ใช้อนุมัติแล้ว (เปลี่ยน data model §3) |
| Trigger ที่ทำซ้ำทุก 5 นาทีใน install-startup | เพิ่มจาก `-AtStartup`/`-AtLogOn` | restart-on-failure ไม่ครอบคลุม engine ที่ crash หรือถูก kill ผู้ใช้อนุมัติแล้ว |
| เปลี่ยน queue ของ job | ย้าย run ที่ยัง `Pending` ไปคิวใหม่ด้วย | ถ้าไม่ย้าย run อาจค้างอยู่ในคิวที่ไม่มี engine |
| `--desc ""` / ลบ description ใน GUI | ล้างค่าเป็น NULL | ทำแบบเดียวกับ `--after ""` |
| `--db` (global flag) | กำหนด DB ได้จาก command line | ให้ task ใน Task Scheduler อ้าง DB ไฟล์เดียวกันได้แน่นอน |
| http: userinfo ใน URL | แปลงเป็น Basic auth และตัดออกจาก log | urllib ไม่รองรับ userinfo และป้องกันรหัสผ่านหลุดลง log |
| `_Cursor.fetchone` ปิด cursor | ป้องกัน snapshot ค้าง | connection ที่เปิดนานต้องเห็นการเขียนจากโปรเซสอื่น |
| ไฟล์ `.bat` | ตัวเปิดโปรแกรมสำหรับผู้ใช้ | ผู้ใช้ขอเพิ่ม |
| `setup.bat` / `uninstall.bat` / `package.bat` | ติดตั้งแบบดับเบิลคลิกครั้งเดียว | ผู้ใช้ขอ (เลือกแทนการทำเป็น .exe ด้วย PyInstaller) |

## 19. ข้อจำกัดที่รู้

- ไม่รองรับ DST (เวลาท้องถิ่นแบบไม่มี timezone)
- การปิด engine แบบกะทันหัน (End task, kill หรือ reboot) ทำให้งานที่รันอยู่จบเป็น `engine lost` และโปรเซสของงานอาจค้างต่อ ต้องใช้ `stop-engine` ถ้าจะปิดแบบปลอดภัย
- DB ต้องอยู่บน local disk (WAL ใช้บน network share ไม่ได้)
- งานต้อง idempotent เพราะมี retry และการกู้คืน (`engine lost`)
- `sql` ไม่รองรับ `GO` และ `pyodbc` ยังไม่ได้ทดสอบกับ SQL Server จริง
- ยังไม่ได้ทดสอบบน Linux (โค้ดฝั่ง POSIX มีอยู่แต่ยังไม่เคยรันจริง)
- มีช่วงสั้น ๆ ระหว่าง `Popen` กับการใส่โปรเซสลงใน Job Object ถ้าโปรเซสลูกสร้างโปรเซสหลานในช่วงนั้นพอดี โปรเซสหลานนั้นจะอยู่นอก Job Object
- `kill_tree` ฆ่าได้เฉพาะโปรเซสที่เป็นลูกหลานของคำสั่งที่ Orch รัน งานที่ส่งต่อให้ service อื่นรัน (เช่น `UiRobot.exe execute` ซึ่ง `UiPath.Executor.exe` ถูกเปิดโดย UiPath service) จะยังทำงานต่อหลัง Cancel หรือ timeout (ดู MANUAL §4.7)
- ช่อง cron ยังไม่รองรับชื่อวันหรือชื่อเดือน (`MON`, `JAN`)
- ยังเปลี่ยนชื่องานไม่ได้ และยังไม่รองรับ dependency แบบรอหลายงาน (AND)
- ไม่อยู่ในขอบเขต Phase 1: การรันขนานในคิวเดียว, worker หลายเครื่อง, Web UI, credential store, การแจ้งเตือน, per-job run-as

# Orch — Serial-Queue Job Orchestrator (Phase 1)

โปรแกรมตั้งเวลารันงานแทน Windows Task Scheduler มีทั้ง CLI สไตล์ `schtasks` (ส่งผลลัพธ์เป็น `--json` ได้) และ GUI
งานในคิวเดียวกันจะ **รันทีละงาน** ตามลำดับ priority มี retry, timeout พร้อม kill process tree, log ของทุกรอบการรัน
และ dependency แบบ chain

- Python 3.10+ **stdlib เท่านั้น** (`pyodbc` จำเป็นเฉพาะเมื่อใช้งานชนิด `sql` กับฐานข้อมูลที่ไม่ใช่ sqlite)
- เก็บข้อมูลใน SQLite ไฟล์เดียว `orch.db` (โหมด WAL)
- ไม่มี Windows Service ของตัวเอง: ใช้ Task Scheduler เพื่อเปิด engine ตอน boot หรือตอน logon เท่านั้น (ดู [การเปิด engine อัตโนมัติ](#การเปิด-engine-อัตโนมัติ-windows))

## ไฟล์ในโปรเจกต์

| ไฟล์ | หน้าที่ |
|---|---|
| `orch.py` | core + engine + CLI |
| `orch_gui.py` | GUI (tkinter) |
| `start_orch.bat` | เปิด engine ของคิว `default` ในหน้าต่างของมันเอง แล้วเปิด GUI (**ดับเบิลคลิกไฟล์นี้เพื่อเริ่มใช้งาน**) |
| `orch_gui.bat` | เปิดเฉพาะ GUI |
| `orch_engine.bat [QUEUE]` | รัน engine ของคิวที่ระบุในหน้าต่างนี้ (Ctrl+C เพื่อหยุด) |
| `orch.bat` | ตัวเรียก CLI ใช้ `orch query` แทน `python orch.py query` ได้ |
| `tests/` | ชุดทดสอบ (`unittest`) |
| `docs/MANUAL.md` | **คู่มือการใช้งานและการตั้งค่า** (ติดตั้ง, สร้างงาน, schedule, ตัวเลือก, engine, แก้ปัญหา) |
| `docs/TECHNICAL.md` | **เอกสารทางเทคนิค** (สถาปัตยกรรม, data model, การทำงานภายใน, การทดสอบ) |

## ติดตั้ง

1. ติดตั้ง Python 3.10 ขึ้นไป (เลือก "Add python.exe to PATH")
2. คัดลอกโฟลเดอร์นี้ไปไว้บน **local disk** (ห้ามไว้บน network share เพราะ SQLite WAL ใช้บน share ไม่ได้)
3. `python orch.py init`
4. (ถ้าใช้ `sql` กับ SQL Server) `pip install pyodbc` และติดตั้ง ODBC Driver for SQL Server

ค่าเริ่มต้น DB คือ `orch.db` ข้างไฟล์ `orch.py` เปลี่ยนได้ด้วย `--db PATH` (ใส่ก่อนคำสั่ง) หรือตัวแปร `ORCH_DB`
log ของ engine อยู่ที่ `logs\engine-<queue>.log` ข้างไฟล์ DB

## เริ่มใช้งานเร็ว

```cmd
python orch.py create hello --type command --command "echo hello" --every 5m
python orch.py create nightly --type python --file C:\jobs\etl.py --cron "0 2 * * *" --retry 2 --timeout 1800
python orch.py create report --type powershell --file C:\jobs\report.ps1 --after nightly
python orch.py engine --queue default        :: หรือดับเบิลคลิก orch_engine.bat
python orch.py run hello                     :: สั่งรันทันที
python orch.py runs
python orch.py logs 12 --follow
```

### ตัวอย่างใน PowerShell

```powershell
$jobs = python orch.py query --json | ConvertFrom-Json
$jobs | Select-Object name, job_type, queue, next_run_at

python orch.py runs --status Dead --limit 20 --json | ConvertFrom-Json |
    Format-Table id, job_name, attempt, finished_at, error

python orch.py create api-ping --type http --url https://example.com/health --expect "200,204" --every 1m
python orch.py create load-sales --type sql --conn env:SALES_DB --query-file C:\sql\load.sql --cron "30 1 * * *"
```

## ชนิดงาน (`--type`) และ params

ใส่ params ได้ 2 แบบ: ใส่ทีละ flag (`--command`, `--file`, `--args`, `--cwd`, `--conn`, `--query`, `--query-file`, `--url`,
`--method`, `--body`, `--header k:v`, `--expect`) หรือใส่ทั้งก้อนด้วย `--params '<JSON>'` ถ้าใส่ทั้งสองแบบ ค่าจาก flag จะทับ `--params`
ทุกชนิดรองรับ `env` (object) และ `cwd` ด้วย

| ชนิด | params | หมายเหตุ |
|---|---|---|
| `command` | `command`, `cwd?`, `env?`, `encoding?` | รันผ่าน shell (`cmd.exe`) และ decode output ด้วย OEM code page ของเครื่อง (เครื่องภาษาไทยคือ `cp874`) |
| `powershell` | `file` + `args?` หรือ `command` | เรียกด้วย `-NoProfile -NonInteractive -ExecutionPolicy Bypass` |
| `python` | `file`, `args?` | ใช้ interpreter ตัวเดียวกับที่รัน engine พร้อม `-u` และ `PYTHONIOENCODING=utf-8` |
| `sql` | `conn`, `query` หรือ `query_file` | `conn` เป็น `env:VAR` (แนะนำ), `sqlite:<path>` หรือ ODBC connection string; commit เมื่อสำเร็จ rollback เมื่อ error; **ไม่รองรับ `GO`** |
| `http` | `url`, `method?`, `headers?`, `body?`, `expect?`, `timeout?` | `expect` เช่น `"200,201"`, `"2xx"` หรือ `"200-299"` (ค่าเริ่มต้นคือ 2xx); ถ้า `body` เป็น JSON object จะส่งเป็น JSON |

ทุกงานรันเป็น child process เสมอ งาน `sql`/`http` รันผ่าน `orch.py _exec <type>` และส่ง params ผ่านตัวแปร
`ORCH_PARAMS` ไม่ผ่าน command line; Orch จะไม่พิมพ์ connection string ลง log และ log ของ `http` แสดง URL
โดยตัด query string และ `user:password@` ออก (ถ้าใส่ `user:password@` ใน URL จะส่งเป็น Basic auth ให้)

## Schedule

ใส่ได้หลาย schedule ต่อ job: `--cron`, `--every`, `--at` ใส่ซ้ำได้ และมี `--startup` (ใน GUI ใส่บรรทัดละหนึ่งรายการ)

| ชนิด | ตัวอย่าง | หมายเหตุ |
|---|---|---|
| cron | `*/5 * * * *`, `0 8 * * 1-5`, `0 0 1,15 * *` | 5 ช่อง `m h dom mon dow`; dow 0 และ 7 = อาทิตย์; ถ้าระบุทั้ง dom และ dow จะนับแบบ OR |
| every | `30s`, `10m`, `2h`, `1d`, `90` | รอบถัดไปนับจากเวลารอบก่อนหน้า เวลาจึงไม่เลื่อนสะสม |
| at | `2026-10-01 08:00` | รันครั้งเดียว ถ้าเป็นเวลาในอดีตจะ error |
| startup | — | รันเมื่อ engine ของคิวนั้นเริ่มทำงาน |

**รอบที่พลาด** (engine ปิดอยู่ หรือเครื่องดับ): รอบที่ช้าไม่เกิน 90 วินาทีนับว่าตรงเวลาและรันเสมอ ส่วนรอบที่ช้ากว่านั้นทำตาม `--missed`:
`skip` (ไม่รัน), `run_once` (ค่าเริ่มต้น: รันแค่รอบล่าสุด) หรือ `run_all` (รันทุกรอบ ต้องใช้คู่กับ `--dedup allow`
มิฉะนั้น dedup จะตัดรอบที่เกินทิ้ง)

## พฤติกรรมของคิว

- **1 engine ต่อ 1 queue** และรันทีละงาน ลำดับการหยิบงานคือ `priority` มากก่อน → `scheduled_at` → ลำดับที่เข้าคิว
- **dedup** (`--dedup`): `skip` (ค่าเริ่มต้น: ถ้ามีงานเดียวกันรออยู่แล้ว รอบใหม่จะถูกบันทึกเป็น `Skipped`), `coalesce` (แทนที่ตัวที่รออยู่ด้วยรอบใหม่),
  `allow` (เข้าคิวเสมอ); การสั่งรันเอง (Run now) และการ retry ไม่ผ่าน dedup
- **retry**: `--retry N --retry-delay S --backoff B` รอบที่ n จะรอ `S × B^(n-1)` วินาที ระหว่างรอ งานอื่นแซงคิวได้
  สถานะ `Failed` หมายถึงล้มเหลวแต่ยังจะ retry ต่อ ส่วน `Dead` หมายถึงหมดสิทธิ์ retry แล้ว
- **timeout** (`--timeout`, 0 = ไม่จำกัด) และ **cancel** (`orch end RUN_ID`): kill ทั้ง process tree ด้วย `taskkill /T /F`
  แล้วตามด้วย Windows Job Object ซึ่งเก็บทุกโปรเซสที่งานสร้างไว้ จึงฆ่าโปรเซสหลานที่หลุดจาก tree ได้ (กรณีตัวกลางจบไปก่อน เช่น .bat ที่ใช้ `start`)
  Job Object ใช้ตอนสั่ง kill เท่านั้น: งานที่จบปกติหรือ engine ที่ตายไป จะไม่ทำให้โปรเซสที่เหลืออยู่ถูกฆ่า
- **chain**: `--after A --after-on success|failure|always` เมื่อ A จบเป็น `Succeeded`/`Dead` งานนี้จะเข้าคิว (อยู่คนละ queue ได้)
- **pause/resume** คิว: งานที่รันอยู่จะรันต่อจนจบ แต่ engine จะไม่หยิบงานใหม่
- **ปิด engine** (Ctrl+C / CTRL_BREAK / ปิดงานใน Task Scheduler): kill งานที่รันอยู่แล้วคืนกลับเป็น `Pending` (ไม่นับเป็น attempt ที่ล้มเหลว)
- **engine ตายกะทันหัน**: เมื่อเปิดใหม่ งานที่ค้างเป็น `Running` จะถูกตั้งเป็นล้มเหลว (`engine lost`) แล้ว retry ตามกติกา
  ถ้าโปรเซสเดิมยังไม่ตาย จะมี WARN ให้ตรวจสอบด้วยตนเอง (Orch จะไม่ kill ให้ เพราะเลข pid อาจถูกนำไปใช้ซ้ำกับโปรเซสอื่น)
- ประวัติการรันที่เก่ากว่า 30 วันจะถูกลบทุกชั่วโมง หรือสั่งเองด้วย `orch prune --days N`

## คำสั่ง CLI

`init`, `create`, `change` (แก้เฉพาะ flag ที่ระบุ; ถ้าระบุ schedule flag ใด ๆ จะแทนที่ schedule ทั้งชุด; `--after ""` = ล้าง chain),
`query [NAME] [--json]`, `run NAME`, `runs [--queue Q] [--status S] [--job NAME] [--limit N] [--json]`, `end RUN_ID`,
`logs RUN_ID [--follow]`, `enable`/`disable`/`delete NAME`, `pause`/`resume [--queue Q]`, `prune --days N`,
`engine --queue Q [--poll 1.0]`, `install-startup --queue Q --trigger boot|logon [--user U] [--apply]`
ดูรายละเอียดด้วย `python orch.py <คำสั่ง> --help`; exit code 2 = input ผิดหรือหา job ไม่เจอ, 3 = มี engine ของคิวนี้รันอยู่แล้ว

## GUI

ดับเบิลคลิก `orch_gui.bat` (หรือ `python orch_gui.py [--db PATH]`)
- **แถบบน**: สถานะ engine ของแต่ละคิวและการ pause
- **แท็บ Jobs**: New / Edit / Run now / Enable-Disable / Delete
- **แท็บ Queue / Runs**: 200 รายการล่าสุด กรองตามสถานะหรือ queue ได้ มี Cancel / Pause / Resume และดับเบิลคลิกเพื่อดู log
- **แท็บ Log**: ถ้างานยังรันอยู่ log ใหม่จะต่อท้ายเอง
- GUI รีเฟรชทุก 2 วินาที และต้องเปิด engine แยกต่างหาก งานถึงจะรันจริง

## การเปิด engine อัตโนมัติ (Windows)

ใช้ Task Scheduler **เป็นแค่ตัวเปิด engine** (หนึ่ง task ต่อหนึ่ง queue) โดยสร้างด้วย `Register-ScheduledTask`
ซึ่งตั้ง `ExecutionTimeLimit = 0` ได้ (task ที่สร้างด้วย `schtasks` จะถูกหยุดหลัง 72 ชั่วโมงตามค่าเริ่มต้น)
ตั้งให้ restart ทุก 1 นาทีได้สูงสุด 999 ครั้ง, `IgnoreNew` และ `StartWhenAvailable`

```cmd
python orch.py install-startup --queue default --trigger boot          :: พิมพ์สคริปต์ออกมาให้ตรวจก่อน
python orch.py install-startup --queue default --trigger boot --apply  :: ลงทะเบียนจริง (ต้องเปิด prompt แบบ Administrator)
python orch.py install-startup --queue ui --trigger logon --apply
```

- `boot`: เริ่มตอนเปิดเครื่องในนาม `SYSTEM` (หรือ `--user DOMAIN\svc` ซึ่งจะถามรหัสผ่านตอนรันสคริปต์ และไม่เก็บรหัสผ่านไว้ในสคริปต์)
  รันได้แม้ไม่มีใคร login **ห้ามใช้กับงาน UI automation**
- `logon`: เริ่มเมื่อ user login และรันใน session ของ user ใช้กับคิวสำหรับงาน UI automation
  (เครื่องต้องตั้ง auto-login และห้าม lock หน้าจอ) ควรแยกงานประเภทนี้ไว้คิวของตัวเอง
- **สิทธิ์ของงาน**: ทุกงานในคิวรันด้วยสิทธิ์ของ account ที่เปิด engine ของคิวนั้น ถ้าต้องการหลาย account ให้แยกคิวและแยก engine
- engine ที่ Task Scheduler เปิดใช้ `pythonw.exe` จึงไม่มีหน้าต่าง console ให้ดู log ที่ `logs\engine-<queue>.log`
- ยกเลิกด้วย `Unregister-ScheduledTask -TaskName 'Orch Engine (default)' -Confirm:$false`

## ข้อจำกัดที่รู้

- **ไม่รองรับ DST**: เวลาทั้งหมดเก็บเป็นเวลาท้องถิ่นแบบ `YYYY-MM-DD HH:MM:SS` ไม่มี timezone (ประเทศไทยไม่มี DST)
- **DB ต้องอยู่บน local disk** ห้ามไว้บน network share
- **งานต้อง idempotent** (รันซ้ำได้โดยไม่เสียหาย) เพราะมี retry และมีการกู้คืนหลัง engine ตาย งานเดิมจึงอาจถูกรันซ้ำ
- **`sql` ไม่รองรับ `GO`** ถ้าต้องแบ่ง batch ให้แยกเป็นหลาย job แล้วต่อกันด้วย `--after`
- **connection string ควรใช้ `env:VAR`** อย่าใส่รหัสผ่านไว้ใน params ตรง ๆ เพราะ params เก็บเป็นข้อความธรรมดาใน `orch.db`
- **ภาษาไทยใน output ของ `cmd`/PowerShell** decode ด้วย OEM code page ถ้าเครื่องไม่ได้ตั้ง locale ภาษาไทย (OEM ไม่ใช่ `cp874`)
  ตัว `cmd`/PowerShell เองจะแสดงภาษาไทยไม่ได้ ถ้าโปรแกรมที่เรียกเขียน output เป็น UTF-8 ให้ใส่ `"encoding": "utf-8"`
  (หมายเหตุ: ใส่ `chcp 65001` ไว้ใน `command` เดียวกันแล้วตามด้วย `echo` ใช้ไม่ได้ เพราะ `cmd` ยังเขียน output ด้วย OEM code page)
- log ถูกจำกัดที่ 5,000 บรรทัดต่อรอบ และ 2,000 ตัวอักษรต่อบรรทัด
- ยังไม่รองรับการเปลี่ยนชื่อ job (chain อ้างถึงกันด้วยชื่อ) และลบ job ที่มี job อื่นอ้างถึงใน `--after` ไม่ได้
- Phase 1 ยังไม่มี: การรันขนานในคิวเดียว, worker หลายเครื่อง, Web UI, credential store, การแจ้งเตือน (อีเมล/LINE),
  dependency แบบรอหลายงาน (AND), และการกำหนด account รายงาน (per-job run-as)

## ทดสอบ

```cmd
python -m unittest discover -s tests -p "test_*.py"
set ORCH_STRESS=1 && python -m unittest discover -s tests -p "test_12_stress.py"   :: stress test (ประมาณ 40 วินาที)
```
test ของ engine เปิด `orch.py engine` เป็น subprocess จริง ใช้เวลาทั้งชุดประมาณ 1 นาที test ของ GUI จะถูกข้ามเมื่อไม่มีหน้าจอ
stress test (SPEC §11) เปิด engine 3 คิวพร้อมกัน มีตัวอ่านแบบ GUI ทุก 0.2 วินาที และสั่ง CLI เขียนซ้ำ ๆ แล้วตรวจว่าไม่มี `database is locked`,
ไม่มีงานในคิวเดียวกันรันซ้อนกัน และ output ไม่หาย (ตั้ง `ORCH_STRESS_SECONDS` เพื่อเปลี่ยนระยะเวลาได้)

**ยังไม่ได้ทดสอบบน Linux:** Phase 1 ทดสอบบน Windows 11 เท่านั้น โค้ดฝั่ง POSIX (`killpg`, `start_new_session`, SIGTERM) มีอยู่แล้วแต่ยังไม่เคยรันจริง

| SPEC §9 | test |
|---|---|
| #1 ลำดับ priority / FIFO | `test_04_queue` `test_priority_order_and_fifo`, `test_08_engine` `test_priority_order_through_engine` |
| #2 retry + backoff | `test_06_finalize` `test_retry_backoff_then_dead`, `test_08_engine` `TestPlanThroughEngine.test_retry_backoff` |
| #3 timeout kill tree แล้วคิวเดินต่อ | `test_08_engine` `test_timeout_then_next_job`, `test_05_executor` `test_timeout_kills_process_tree` / `test_timeout_kills_orphaned_grandchild` |
| #4 chain | `test_06_finalize` `test_chain_*`, `test_08_engine` `TestPlanThroughEngine.test_chain` |
| #5 dedup skip / coalesce | `test_04_queue` `test_dedup_skip`, `test_dedup_coalesce` |
| #6 cancel งานที่รันอยู่ | `test_08_engine` `test_cancel_running_via_cli` |
| #7 missed schedule | `test_07_scheduler` `test_missed_policies`, `test_08_engine` `TestPlanThroughEngine.test_missed_policies_at_engine_start` |
| #8 engine ตาย (kill -9) แล้วเปิดใหม่ | `test_08_engine` `test_recovery_after_hard_kill` |
| #9 engine ซ้อน | `test_08_engine` `test_second_engine_refused` |
| #10 schedule fire ระหว่างงานยาว | `test_08_engine` `test_scheduler_thread_independent_of_worker` |
| #11 sql / http | `test_09_exec`, `test_08_engine` `TestPlanThroughEngine.test_sql_and_http` |
| #12 graceful shutdown | `test_08_engine` `test_graceful_shutdown_requeues` |
| #13 validation | `test_03_jobs` `test_validation_errors_leave_no_partial_job`, `test_cycle_detection` |

## Manual test checklist (Windows)

ส่วนที่ต้องทดสอบบนเครื่อง Windows จริง (ไม่อยู่ในชุด test อัตโนมัติ):

- [ ] **Boot task**: เปิด prompt แบบ Administrator แล้วสั่ง `install-startup --queue default --trigger boot --apply` จากนั้นดูผลว่า `ExecutionTimeLimit` เป็น `PT0S`
      (`(Get-ScheduledTask 'Orch Engine (default)').Settings.ExecutionTimeLimit`)
- [ ] **รันได้โดยไม่มีคน login**: รีสตาร์ทเครื่องแต่ยังไม่ login แล้วตรวจจากเครื่องอื่นหรือ login ภายหลังว่า `logs\engine-default.log` มีบรรทัด `started`
      และงานที่ตั้ง `every: 1m` มีประวัติการรันช่วงก่อน login
- [ ] **สิทธิ์ของ SYSTEM**: engine ในนาม SYSTEM อ่าน/เขียน `orch.db` และโฟลเดอร์ `logs` ได้ และงานที่เรียกไฟล์ของ user ทำงานได้
- [ ] **Restart อัตโนมัติ**: kill `pythonw.exe` ของ engine ใน Task Manager แล้วดูว่าภายใน ~1 นาที Task Scheduler เปิด engine ขึ้นใหม่
      และ run ที่ค้างอยู่ถูกตั้งเป็น `engine lost` แล้ว retry
- [ ] **ไม่ถูกหยุดที่ 72 ชั่วโมง**: ปล่อย engine รันต่อเนื่อง ≥ 72 ชั่วโมง ใน engine log ต้องไม่มีการ stop/start ในช่วงนั้น
- [ ] **Logon task**: `install-startup --queue ui --trigger logon --apply` แล้ว logout/login ตรวจว่า engine ของคิว `ui` เปิดใน session ของ user
      และงาน UI automation (เช่น เปิด Notepad) มองเห็นหน้าต่างได้
- [ ] **Kill process tree**: งาน `.bat` ที่เปิดโปรแกรมลูกหลายชั้น เมื่อถูก timeout หรือ `end` ต้องไม่มีโปรเซสลูกค้างใน Task Manager
- [ ] **ภาษาไทย**: `echo` ภาษาไทยผ่าน `command`, `Write-Output` ผ่าน `powershell`, `print` ผ่าน `python` ต้องแสดงถูกต้องทั้งใน `orch logs` และ GUI
- [ ] **ย้ายงานจริง 3–5 งาน** (SQL / Script / HTTP) จาก Task Scheduler มาใช้ แล้วดูผลได้ครบทั้งใน GUI และ CLI
- [ ] **ยกเลิก**: `Unregister-ScheduledTask` แล้ว engine ไม่เปิดเองอีกหลังรีสตาร์ท

# Orch — คู่มือการใช้งานและการตั้งค่า (User Manual)

คู่มือนี้อธิบายการติดตั้ง การสร้างและตั้งค่างาน การดูผล การดูแลระบบ และการแก้ปัญหา
ถ้าต้องการรายละเอียดการทำงานภายใน ดู [TECHNICAL.md](TECHNICAL.md)

---

## สารบัญ

1. [ติดตั้ง](#1-ติดตั้ง)
2. [เริ่มใช้งานใน 5 นาที](#2-เริ่มใช้งานใน-5-นาที)
3. [แนวคิดพื้นฐาน](#3-แนวคิดพื้นฐาน)
4. [สร้างงาน](#4-สร้างงาน)
5. [ตั้งเวลา (Schedule)](#5-ตั้งเวลา-schedule)
6. [ตัวเลือกของงาน](#6-ตัวเลือกของงาน)
7. [ดูผลการรัน](#7-ดูผลการรัน)
8. [จัดการงานและคิว](#8-จัดการงานและคิว)
9. [Engine และการเปิดอัตโนมัติ](#9-engine-และการเปิดอัตโนมัติ)
10. [การตั้งค่าระบบ](#10-การตั้งค่าระบบ)
11. [ใช้งานจาก PowerShell](#11-ใช้งานจาก-powershell)
12. [ย้ายงานจาก Windows Task Scheduler](#12-ย้ายงานจาก-windows-task-scheduler)
13. [แก้ปัญหา](#13-แก้ปัญหา)
14. [สรุปคำสั่ง](#14-สรุปคำสั่ง)

---

## 1. ติดตั้ง

### 1.1 ติดตั้งด้วย setup.bat (แนะนำ)

1. แตกไฟล์ zip ของ Orch ไว้ที่ไหนก็ได้ (เช่น Downloads)
2. ดับเบิลคลิก **`setup.bat`** แล้วกด **Yes** ที่หน้าต่าง UAC
3. รอจนขึ้น `ติดตั้งเสร็จแล้ว` แล้วกดปุ่มใดก็ได้เพื่อปิดหน้าต่าง

สิ่งที่ setup ทำให้:
- หา Python 3.10 ขึ้นไปที่ติดตั้งให้ทุกคน (all users) ถ้าไม่มีจะลง Python 3.12 ให้ (Python ที่ติดตั้งแบบรายผู้ใช้จะไม่ถูกนับ เพราะ engine ที่รันในนาม SYSTEM ใช้ไม่ได้)
- ติดตั้ง `pyodbc` ให้ทั้งเครื่อง (ถ้าไม่สำเร็จจะเตือนแล้วติดตั้งต่อ งานอื่นยังใช้ได้ปกติ ยกเว้นงาน `sql` กับ SQL Server)
- คัดลอกโปรแกรมไปที่ `C:\Orch`, สร้าง `orch.db`, ลงทะเบียน engine ของคิว `default` แบบเปิดตอนบูต และสั่งเริ่มทันที
- จำกัดสิทธิ์ของ `C:\Orch`: เขียนได้เฉพาะ SYSTEM, Administrators และ account ที่รัน setup ผู้ใช้อื่นอ่านได้อย่างเดียว
  (engine รัน `orch.py` และงานใน `orch.db` ในนาม SYSTEM ถ้าใครเขียนโฟลเดอร์นี้ได้ ก็สั่งให้เครื่องรันอะไรก็ได้ในนาม SYSTEM)
- สร้างไอคอน **Orch** บน Desktop และใน Start menu สำหรับเปิด GUI
- เพิ่ม `C:\Orch` ลง PATH ของเครื่อง (เปิดหน้าต่าง cmd หรือ PowerShell ใหม่แล้วพิมพ์ `orch query` ได้เลย)

| ต้องการ | ทำอย่างไร |
|---|---|
| ติดตั้งที่อื่น | `setup.bat -InstallDir D:\Orch` |
| เพิ่มคิวสำหรับงาน UI automation | login ด้วย user ของ robot แล้วรัน `setup.bat -Queue ui -Trigger logon` ตอน UAC กรอกบัญชี admin คนไหนก็ได้ task จะผูกกับ **user ที่ login อยู่** (ระบุเองได้ด้วย `-User DOMAIN\rpa`) user นั้นไม่ต้องเป็น admin และได้สิทธิ์เขียน `C:\Orch` อัตโนมัติ **จึงมีสิทธิ์เท่า admin ของเครื่อง** (เหมือน `-AllowUser`) |
| ให้ผู้ใช้อื่นใช้ GUI/CLI ได้ | `setup.bat -AllowUser DOMAIN\user` (หลายคนคั่นด้วย `,`) **account นั้นจะมีสิทธิ์เท่า admin ของเครื่อง** เพราะสร้างงานที่รันในนาม SYSTEM ได้ ผู้ใช้ที่ไม่ได้รับสิทธิ์จะเปิด GUI ไม่ได้ |
| อัปเดตเป็นเวอร์ชันใหม่ | แตก zip ใหม่แล้วรัน `setup.bat` ทับ ระบบจะหยุด engine แบบปลอดภัยก่อน และไม่แตะ `orch.db` |
| เครื่องไม่มีอินเทอร์เน็ต | วาง `python-3.12.x-amd64.exe` และ `pyodbc-*-cp312-*-win_amd64.whl` ไว้ข้าง `setup.bat` ก่อนรัน |
| ถอนการติดตั้ง | ดับเบิลคลิก `C:\Orch\uninstall.bat` (ลบ task **ของทุกคิว**, ไอคอน และ PATH แต่เก็บ `C:\Orch` และ `orch.db` ไว้) ตอนจบจะแสดงคำสั่ง `setup.bat` ของแต่ละคิวไว้ใช้ติดตั้งกลับ เพราะ setup ติดตั้งครั้งละหนึ่งคิว |
| สร้าง zip ไปติดตั้งเครื่องอื่น | รัน `package.bat` บนเครื่องที่มี git จะได้ `dist\Orch-<วันที่>.zip` |

log ของการติดตั้งอยู่ที่ `C:\Orch\logs\setup-<เวลา>.log` และของการถอนการติดตั้งอยู่ที่ `logs\uninstall-<เวลา>.log`

### 1.2 ติดตั้งเอง

1. ติดตั้ง **Python 3.10 ขึ้นไป (แนะนำ 3.12)** จาก python.org โดยเลือก **Customize installation** แล้วติ๊ก
   **"Install Python for all users"** และ **"Add Python to environment variables"**
   (engine ที่เปิดตอนบูตรันในนาม SYSTEM จึงต้องมองเห็น Python และ package ที่ติดตั้งไว้ ห้ามติดตั้งแบบรายผู้ใช้)
2. คัดลอกโฟลเดอร์ Orch ไปไว้บน **ดิสก์ในเครื่อง** เช่น `C:\Orch` (**ห้ามไว้บน network share**)
3. เปิด Command Prompt แล้วสั่ง:
   ```cmd
   cd /d C:\Orch
   python orch.py init
   ```
   ถ้าขึ้น `initialized C:\Orch\orch.db` แปลว่าพร้อมใช้
4. (ถ้าจะใช้งาน SQL กับ SQL Server) ใน Command Prompt แบบ Administrator สั่ง `python -m pip install pyodbc` (ห้ามใช้ `--user`)
   และติดตั้ง *ODBC Driver for SQL Server*

## 2. เริ่มใช้งานใน 5 นาที

1. ดับเบิลคลิก **`start_orch.bat`** ระบบจะเปิดหน้าต่าง engine (หน้าต่างดำ ห้ามปิดถ้าต้องการให้งานรัน) และหน้าต่าง GUI
2. ใน GUI แท็บ **Jobs** กด **New** แล้วกรอก:
   - Name: `hello`
   - Type: `command`
   - Params: `{"command": "echo hello"}`
   - Schedules: `every: 1m`
3. กด **Save** แล้วเลือกงาน `hello` และกด **Run now**
4. ไปที่แท็บ **Queue / Runs** จะเห็นแถวสีเขียว `Succeeded` ดับเบิลคลิกเพื่อดู log

ไฟล์ .bat ที่มีให้

| ไฟล์ | ใช้ทำอะไร |
|---|---|
| `start_orch.bat` | เปิด engine ของคิว `default` และ GUI (ใช้ตัวนี้เป็นหลัก) |
| `orch_gui.bat` | เปิดเฉพาะ GUI |
| `orch_engine.bat [คิว]` | เปิดเฉพาะ engine ของคิวที่ระบุ (ไม่ระบุ = `default`) หยุดด้วย Ctrl+C |
| `orch.bat` | พิมพ์ `orch <คำสั่ง>` แทน `python orch.py <คำสั่ง>` (ต้องอยู่ในโฟลเดอร์ Orch) |

## 3. แนวคิดพื้นฐาน

| คำ | ความหมาย |
|---|---|
| **Job (งาน)** | นิยามของสิ่งที่จะรัน เช่น "รันสคริปต์ backup ทุกตี 2" |
| **Run (รอบการรัน)** | การรันงานหนึ่งครั้ง มีเลข ID สถานะ และ log ของตัวเอง |
| **Queue (คิว)** | กลุ่มงานที่รัน **ทีละงาน** ตามลำดับ priority ค่าเริ่มต้นคือ `default` |
| **Engine** | โปรแกรมที่คอยหยิบงานในคิวมารัน **1 engine ต่อ 1 คิว** ถ้าไม่มี engine เปิดอยู่ งานในคิวนั้นจะไม่รัน |

**สถานะของ run**

| สถานะ | สีใน GUI | ความหมาย |
|---|---|---|
| `Pending` | เหลือง | รอคิว (หรือรอเวลา retry) |
| `Running` | ฟ้า | กำลังรัน |
| `Succeeded` | เขียว | สำเร็จ (exit code 0) |
| `Failed` | ส้ม | ล้มเหลว แต่จะ retry ต่อ |
| `Dead` | แดง | ล้มเหลว และหมดสิทธิ์ retry แล้ว |
| `Cancelled` | เทา | ถูกยกเลิก |
| `Skipped` | เทาอ่อน | ถูกข้าม (มีงานเดียวกันรออยู่แล้ว หรือถูกปิดงานไว้) |

**ลำดับการหยิบงานในคิว:** priority มากก่อน → เวลาที่ควรรันเร็วกว่าก่อน → เข้าคิวก่อนได้ก่อน

## 4. สร้างงาน

สร้างงานได้ 2 ทาง คือ **GUI** (แท็บ Jobs → New) หรือ **CLI** (`python orch.py create ชื่องาน --type ชนิด ...`)

### 4.1 `command` — คำสั่งของ Windows (cmd)
```cmd
python orch.py create cleanup --type command --command "del /q C:\temp\*.tmp" --cron "0 3 * * *"
python orch.py create run-bat --type command --command "C:\jobs\nightly.bat" --cwd C:\jobs --every 1h
```
params: `command` (จำเป็น), `cwd`, `env`, `encoding`

### 4.2 `powershell`
```cmd
python orch.py create report --type powershell --file C:\jobs\report.ps1 --args "-Month 9" --cron "0 7 1 * *"
python orch.py create disk --type powershell --command "Get-PSDrive C | Select Used,Free" --every 30m
```
params: `file` + `args` **หรือ** `command` (ต้องมีอย่างใดอย่างหนึ่ง) ระบบเรียกด้วย `-NoProfile -NonInteractive -ExecutionPolicy Bypass`

### 4.3 `python`
```cmd
python orch.py create etl --type python --file C:\jobs\etl.py --args "--full" --cwd C:\jobs --cron "0 2 * * *"
```
params: `file` (จำเป็น), `args` ใช้ Python ตัวเดียวกับที่รัน engine และ output เป็น UTF-8

> ถ้า `args` ขึ้นต้นด้วย `-` ให้เขียนแบบ `--args="-x 1"`

### 4.4 `sql`
```cmd
python orch.py create load-sales --type sql --conn env:SALES_DB --query-file C:\sql\load.sql --cron "30 1 * * *"
python orch.py create purge --type sql --conn env:SALES_DB --query "DELETE FROM log WHERE dt < DATEADD(day,-90,GETDATE())"
```
- `conn`: แนะนำให้ใช้ **`env:ชื่อตัวแปร`** เพื่อไม่ให้รหัสผ่านถูกเก็บใน DB (ดู[หัวข้อ 10.3](#103-ความลับ-connection-string-และตัวแปรแวดล้อม))
  หรือใช้ `sqlite:C:\path\file.db` สำหรับ SQLite
- `query` หรือ `query-file` (อย่างใดอย่างหนึ่ง) ถ้าสำเร็จจะ commit ถ้า error จะ rollback
- **ห้ามมี `GO`** ถ้าต้องแบ่ง batch ให้แยกเป็นหลายงานแล้วต่อกันด้วย `--after`

### 4.5 `http`
```cmd
python orch.py create api-health --type http --url https://api.example.com/health --expect 200 --every 5m
python orch.py create webhook --type http --url https://hooks.example.com/x --method POST ^
    --header "Content-Type: application/json" --body "{\"text\":\"done\"}" --after etl
```
params: `url` (จำเป็น), `method`, `headers`, `body`, `expect` (เช่น `200,201`, `2xx`, `200-299` ค่าเริ่มต้น 2xx), `timeout` (วินาที ค่าเริ่มต้น 30)
ถ้า status ไม่ตรงกับ `expect` จะถือว่าล้มเหลว

### 4.6 ใส่ params ทั้งก้อนเป็น JSON
ใช้ได้ทุกชนิดงาน และเป็นวิธีเดียวที่ใส่ `env` หรือ `encoding` ผ่าน CLI ได้:
```cmd
python orch.py create legacy --type command --params "{\"command\": \"C:/old/run.exe\", \"env\": {\"MODE\": \"prod\"}, \"encoding\": \"utf-8\"}"
```
> ใน Command Prompt การใส่ `\` ใน JSON มักทำให้ JSON ผิดรูปแบบ ให้ใช้ `/` แทนใน path (Windows รับได้) หรือแก้ params ผ่าน GUI

ใน GUI ให้พิมพ์ JSON ลงกล่อง **Params** ได้เลย (ระบบเติม template ให้ตามชนิดงาน) โดย path ต้องเขียน `\\` เช่น `"C:\\old\\run.exe"` ตามกติกาของ JSON

### 4.7 งาน UiPath และ RPA อื่น ๆ

**หลักการ:** Orch จะรู้ผลของงานได้ ก็ต่อเมื่อคำสั่งที่ใช้ **รอจนงานเสร็จแล้วคืน exit code**
(0 = สำเร็จ, ค่าอื่น = ล้มเหลว)

ห้ามใช้ shortcut ที่สร้างจาก UiPath Assistant (`... - UiPath.lnk`) หรือคำสั่ง `start` เพราะ shortcut แบบนี้
เป็นลิงก์ `uipath-assistant://` แค่สั่งให้ Assistant เริ่มงาน แล้วจบทันที ผลคือ:
- Orch ขึ้น Succeeded ภายใน 1 วินาที แม้ robot จะ error
- งานถัดไปในคิวเริ่มทั้งที่ robot ยังทำงานอยู่
- retry, timeout และ `--after` ใช้ไม่ได้

ให้เรียก `UiRobot.exe execute` แทน คำสั่งนี้รอ robot ทำจนเสร็จ แล้วพิมพ์ output arguments ของ workflow เป็น JSON (เช่น `{}`):
```cmd
python orch.py create rpa-sort --type command --queue ui --timeout 600 --retry 0 ^
    --command "\"%LOCALAPPDATA%\Programs\UiPath\Studio\UiRobot.exe\" execute --process-name \"ชื่อ process\""
```
- `--process-name` ใช้ชื่อเดียวกับที่เห็นใน UiPath Assistant หรือจะใช้ `--package-id <id>`, `--file <x.nupkg>` แทนก็ได้
- ส่งค่าเข้า workflow ได้ด้วย `--input "{\"arg\": 1}"`
- ตำแหน่งของ `UiRobot.exe` ขึ้นกับวิธีติดตั้ง UiPath ให้ตรวจดูก่อน (เครื่องที่ติดตั้ง Studio แบบ per-user อยู่ที่ `%LOCALAPPDATA%\Programs\UiPath\Studio\`)
- งานที่ต้องใช้หน้าจอต้องอยู่คิว `ui` เท่านั้น (ดู [9.2](#92-ตั้งให้-engine-เปิดเองตอนเปิดเครื่อง-แนะนำสำหรับเครื่องที่ใช้งานจริง))

> **ข้อจำกัด: Cancel และ timeout หยุด robot ไม่ได้**
> ตัวที่รัน robot จริง (`UiPath.Executor.exe`) ถูกเปิดโดย service ของ UiPath ไม่ใช่ process ลูกของ `UiRobot.exe`
> พอ Orch kill งาน robot จึงยังทำงานต่อจนจบ Orch แสดง Cancelled หรือ Failed แต่ไม่รู้ผลจริงของ robot
> - ถ้าจะหยุด robot จริง ให้หยุดจาก UiPath Assistant ด้วย
> - ตั้ง `--timeout` ให้เผื่อเวลามาก ๆ (เช่น 5–10 เท่าของเวลาปกติ) ถ้าหมดเวลาแล้ว robot ยังไม่จบ
>   งานถัดไปในคิวจะเริ่มซ้อนกับ robot ตัวเดิม
> - ถ้า retry ทำงานหลัง timeout robot อาจถูกรันซ้อนกันสองรอบ ถ้า process รันซ้ำแล้วเกิดความเสียหายได้ ให้ตั้ง `--retry 0`

**RPA หรือโปรแกรมอื่น:** ใช้หลักการเดียวกัน ถ้าโปรแกรมมี CLI ที่รอจนเสร็จแล้วคืน exit code ก็ใช้ได้เลย
(Python/Selenium ใช้ชนิด `python`, .exe ทั่วไปใช้ชนิด `command`)
ส่วนโปรแกรมที่สั่งงานได้แค่ผ่าน URL หรือ shortcut จะได้แค่ "สั่งแล้วจบ" Orch จะไม่รู้ผลจริง

## 5. ตั้งเวลา (Schedule)

งานหนึ่งมีได้หลาย schedule ใน CLI ใส่ `--cron`, `--every`, `--at` ซ้ำได้ และมี `--startup`
ใน GUI ใส่ในกล่อง **Schedules** บรรทัดละหนึ่งรายการ เช่น:
```
cron: 0 2 * * *
every: 10m
at: 2026-12-31 23:00
startup
```
งานที่ไม่มี schedule ยังสั่งรันเองได้ (Run now) หรือรันต่อจากงานอื่นด้วย `--after`

### 5.1 cron — `นาที ชั่วโมง วันที่ เดือน วันในสัปดาห์`

| ตัวอย่าง | ความหมาย |
|---|---|
| `*/5 * * * *` | ทุก 5 นาที |
| `0 * * * *` | ต้นชั่วโมงทุกชั่วโมง |
| `0 2 * * *` | ทุกวันเวลา 02:00 |
| `30 8 * * 1-5` | จันทร์–ศุกร์ เวลา 08:30 |
| `0 18 * * 5` | ทุกวันศุกร์ เวลา 18:00 |
| `0 0 1 * *` | วันที่ 1 ของทุกเดือน เวลาเที่ยงคืน |
| `0 9 1,15 * *` | วันที่ 1 และ 15 เวลา 09:00 |
| `0 6 * 1,4,7,10 *` | วันละครั้งเวลา 06:00 เฉพาะเดือนแรกของแต่ละไตรมาส |
| `0 9 15 * 1` | วันที่ 15 **หรือ** ทุกวันจันทร์ เวลา 09:00 (ถ้าระบุทั้งวันที่และวันในสัปดาห์ จะนับแบบ "หรือ") |

- วันในสัปดาห์: 0 หรือ 7 = อาทิตย์, 1 = จันทร์ … 6 = เสาร์ (ใช้ตัวเลขเท่านั้น ยังไม่รองรับชื่อ `MON`)
- รูปแบบที่ใช้ได้: `*`, `5`, `1,15`, `1-5`, `*/10`, `0-30/10`, `5/10` (ตั้งแต่ 5 ทีละ 10)

### 5.2 every — ทุก ๆ ช่วงเวลา
`30s`, `5m`, `2h`, `1d` หรือตัวเลขวินาที เช่น `90` รอบแรกเริ่มหลังสร้างงานไปหนึ่งช่วง และแต่ละรอบนับต่อจากรอบก่อนพอดี (เวลาไม่เลื่อนสะสม)

### 5.3 at — ครั้งเดียว
`YYYY-MM-DD HH:MM` หรือ `YYYY-MM-DD HH:MM:SS` ต้องเป็นเวลาในอนาคต หลังรันแล้ว schedule นี้จะปิดตัวเอง

### 5.4 startup
รันทุกครั้งที่ engine ของคิวนั้นเริ่มทำงาน (เช่น หลังเปิดเครื่อง ถ้าตั้ง engine ให้เปิดตอน boot)

### 5.5 รอบที่พลาด (engine ปิดอยู่หรือเครื่องดับ)
รอบที่ช้าไม่เกิน 90 วินาทีถือว่าตรงเวลาและรันเสมอ ส่วนรอบที่ช้ากว่านั้นทำตามตัวเลือก `--missed`:

| ค่า | ผลลัพธ์ | เหมาะกับ |
|---|---|---|
| `run_once` (ค่าเริ่มต้น) | รันชดเชย 1 ครั้ง | งานส่วนใหญ่ เช่น sync หรือ report |
| `skip` | ไม่รันชดเชย | งานที่ผูกกับเวลา เช่น แจ้งเตือนตอน 8 โมง |
| `run_all` | รันชดเชยทุกรอบที่พลาด | งานที่ทุกรอบสำคัญ **ต้องใช้คู่กับ `--dedup allow`** |

## 6. ตัวเลือกของงาน

| CLI | ช่องใน GUI | ค่าเริ่มต้น | คำอธิบาย |
|---|---|---|---|
| `--queue` | Queue | `default` | คิวของงาน งานในคิวเดียวกันรันทีละงาน |
| `--priority` | Priority | 5 | ค่ามากรันก่อน (ใช้ค่าเท่าไรก็ได้ เช่น 1–9) |
| `--timeout` | Timeout (s) | 3600 | เกินเวลานี้จะ kill ทั้งโปรเซสและลูกหลานทั้งหมด (0 = ไม่จำกัด) |
| `--retry` | Max retry | 0 | จำนวนครั้งที่ลองใหม่เมื่อล้มเหลว |
| `--retry-delay` | Retry delay (s) | 60 | เวลารอก่อน retry ครั้งแรก |
| `--backoff` | Retry backoff | 2.0 | ตัวคูณเวลารอ: ครั้งที่ n รอ `delay × backoff^(n-1)` |
| `--dedup` | Dedup | `skip` | เมื่อถึงรอบใหม่แต่รอบเดิมยังรออยู่ ดูตารางด้านล่าง |
| `--missed` | Missed | `run_once` | ดู[หัวข้อ 5.5](#55-รอบที่พลาด-engine-ปิดอยู่หรือเครื่องดับ) |
| `--after` | After job | — | รันต่อจากงานนี้ (`--after ""` = ยกเลิก) |
| `--after-on` | After on | `success` | `success` = เมื่องานก่อนหน้าสำเร็จ, `failure` = เมื่อล้มเหลวจนหมดสิทธิ์ retry, `always` = ทั้งสองกรณี |
| `--desc` | Description | — | คำอธิบาย |
| `--disabled` | Enabled (ติ๊กออก) | เปิด | ปิดงานไว้ schedule จะไม่ทำงาน แต่ยังสั่ง Run now ได้ |

**dedup (การรันซ้ำซ้อน)**

| ค่า | เมื่อถึงรอบใหม่แต่รอบเดิมยัง `Pending` |
|---|---|
| `skip` | รอบใหม่ถูกบันทึกเป็น `Skipped` |
| `coalesce` | รอบเดิมถูกบันทึกเป็น `Skipped` แล้วรอบใหม่เข้าคิวแทน |
| `allow` | เข้าคิวทุกรอบ |

การสั่ง Run now และการ retry ไม่ผ่าน dedup เสมอ

**ตัวอย่าง retry:** `--retry 3 --retry-delay 30 --backoff 2` → ถ้าล้มเหลวจะลองใหม่หลัง 30, 60 และ 120 วินาที (รวมทั้งหมด 4 รอบ)
ระหว่างรอ retry งานอื่นในคิวรันแซงได้

**ตัวอย่าง chain:**
```cmd
python orch.py create extract   --type python --file C:\etl\extract.py --cron "0 1 * * *"
python orch.py create transform --type python --file C:\etl\transform.py --after extract
python orch.py create alert     --type http --url https://hooks.example.com/fail --method POST --after transform --after-on failure
```

**แก้ไขงาน:** `python orch.py change ชื่องาน <เฉพาะ flag ที่ต้องการเปลี่ยน>`
- ถ้าใส่ schedule flag ใด ๆ (`--cron`/`--every`/`--at`/`--startup`) schedule เดิม **ทั้งหมด** จะถูกแทนที่
- การลบ schedule ทั้งหมดของงาน ทำได้ใน GUI เท่านั้น (ลบข้อความในกล่อง Schedules ให้ว่าง) หรือใช้ `disable` แทน
- ถ้าเปลี่ยน `--type` ต้องใส่ params ของชนิดใหม่ด้วย
- เปลี่ยนชื่องานไม่ได้ ถ้าต้องการชื่อใหม่ให้สร้างงานใหม่แล้วลบงานเดิม

## 7. ดูผลการรัน

### CLI
```cmd
python orch.py query                          :: รายการงาน พร้อม schedule และเวลารันถัดไป
python orch.py runs                           :: 50 run ล่าสุด
python orch.py runs --status Dead --limit 20  :: เฉพาะที่ล้มเหลว
python orch.py runs --job etl --queue default
python orch.py logs 123                       :: log ของ run #123
python orch.py logs 123 --follow              :: ดู log ต่อเนื่องจนงานจบ
```
ในช่อง `ERROR` ของ `runs` ข้อความที่พบบ่อยได้แก่ `exit code N`, `timeout after Ns`, `cancelled by user`, `engine lost`,
`dedup(skip): มีงานรออยู่แล้ว #id`, `coalesced by #id`, `job disabled` และ `definition deleted`

### GUI
- **แถบบน**: สถานะ engine ของแต่ละคิว (● running / ○ stopped, `(stopping)` ระหว่างที่กำลังหยุด) และบอกถ้าคิวถูก pause
- **แท็บ Queue / Runs**: 200 run ล่าสุด กรองด้วยช่อง Status และ Queue ได้ ดับเบิลคลิก (หรือกด Open log) เพื่อดู log
- **แท็บ Log**: ถ้างานยังรันอยู่ log ใหม่จะต่อท้ายเองทุก 2 วินาที บรรทัด WARN เป็นสีส้ม

**ระดับของบรรทัด log:** `OUT` = output ของงาน, `INFO` = เหตุการณ์ปกติ (เริ่ม/จบ), `WARN` = เหตุการณ์ที่ควรดู (timeout, kill, retry, output ยาวเกินแล้วถูกตัด)
แต่ละรอบเก็บ output ได้สูงสุด 5,000 บรรทัด และ 2,000 ตัวอักษรต่อบรรทัด

## 8. จัดการงานและคิว

| ต้องการ | CLI | GUI |
|---|---|---|
| รันทันที | `run ชื่องาน` | Jobs → Run now |
| ยกเลิก run | `end RUN_ID` | Queue / Runs → Cancel run |
| ปิด/เปิดงาน | `disable ชื่องาน` / `enable ชื่องาน` | Jobs → Enable/Disable |
| ลบงาน | `delete ชื่องาน` | Jobs → Delete |
| หยุดคิวชั่วคราว | `pause --queue Q` | เลือก Queue ในช่องกรอง (หรือเลือก run) → Pause queue |
| เดินคิวต่อ | `resume --queue Q` | Resume queue |
| หยุด engine อย่างปลอดภัย | `stop-engine --queue Q` | เลือก Queue → Stop engine |
| ล้างประวัติเก่า | `prune --days 30` | — |

- **ยกเลิก:** run ที่ยัง `Pending` จะถูกยกเลิกทันที ส่วนที่ `Running` จะถูก kill ภายในประมาณ 2 วินาที
- **Pause:** งานที่กำลังรันจะรันต่อจนจบ แต่ engine จะไม่หยิบงานใหม่จนกว่าจะ resume
- **ลบงาน:** ประวัติการรันยังอยู่ แต่ลบงานที่มีงานอื่นรันต่อจากมัน (`--after`) ไม่ได้ ต้องแก้งานเหล่านั้นก่อน
- **เปิดงานกลับ:** รอบที่ผ่านไประหว่างปิดงานจะไม่ถูกรันชดเชย
- **ประวัติ:** run ที่จบแล้วเกิน 30 วันจะถูกลบอัตโนมัติทุกชั่วโมง (ขณะที่ engine เปิดอยู่)

## 9. Engine และการเปิดอัตโนมัติ

### 9.1 เปิดและปิดเอง
```cmd
python orch.py engine --queue default       :: หรือ orch_engine.bat default
```
- หยุดด้วย **Ctrl+C** หรือ `python orch.py stop-engine --queue default` (สั่งจากหน้าต่างอื่นหรือจากปุ่ม Stop engine ใน GUI ก็ได้)
  ถ้ามีงานกำลังรัน งานจะถูก kill แล้วคืนกลับเป็น `Pending` (ไม่นับเป็นการล้มเหลว) และจะรันใหม่เมื่อเปิด engine
- `stop-engine` รอให้ engine หยุดจริงไม่เกิน 60 วินาที (เปลี่ยนได้ด้วย `--wait SEC` และ `--wait 0` คือไม่รอ)
- ถ้าเปิด engine ของคิวเดียวกันซ้ำ ตัวที่สองจะออกทันทีด้วยข้อความ `already running`
- ถ้ามีหลายคิว ต้องเปิด engine แยกตัวละคิว: `orch_engine.bat default`, `orch_engine.bat ui`

### 9.2 ตั้งให้ engine เปิดเองตอนเปิดเครื่อง (แนะนำสำหรับเครื่องที่ใช้งานจริง)
> ถ้าติดตั้งด้วย `setup.bat` ขั้นตอนนี้ทำให้แล้ว (คิว `default` แบบ boot) และใช้ `setup.bat -Queue Q -Trigger boot|logon` เพื่อเพิ่มคิวได้
> ส่วนขั้นตอนข้างล่างใช้เมื่อติดตั้งเอง

1. เปิด **Command Prompt แบบ Run as administrator**
2. ดูสคริปต์ก่อน (ยังไม่ติดตั้งอะไร):
   ```cmd
   cd /d C:\Orch
   python orch.py install-startup --queue default --trigger boot
   ```
3. ติดตั้งจริง:
   ```cmd
   python orch.py install-startup --queue default --trigger boot --apply
   ```
4. ตรวจผล: เปิด Task Scheduler จะมี task ชื่อ **Orch Engine (default)** หรือใน PowerShell:
   ```powershell
   (Get-ScheduledTask 'Orch Engine (default)').Settings.ExecutionTimeLimit    # ต้องได้ PT0S
   (Get-ScheduledTask 'Orch Engine (default)').Triggers.Count                 # ต้องได้ 2
   Start-ScheduledTask 'Orch Engine (default)'                               # เริ่มทันทีโดยไม่ต้องรีสตาร์ท
   ```

| ตัวเลือก | ทำงานเมื่อ | สิทธิ์ของงาน | ใช้กับ |
|---|---|---|---|
| `--trigger boot` | เปิดเครื่อง (ไม่ต้องมีคน login) | SYSTEM | งานเบื้องหลังทั่วไป: SQL, script, HTTP |
| `--trigger boot --user DOMAIN\svc` | เปิดเครื่อง | account ที่ระบุ (ถามรหัสผ่านตอนติดตั้ง) | งานที่ต้องเข้า network share หรือใช้สิทธิ์ของ account นั้น |
| `--trigger logon [--user U]` | user login | user คนนั้น ใน session ของเขา | **งาน UI automation** (ต้องเห็นหน้าจอ) |

- งานทุกตัวในคิวรันด้วยสิทธิ์ของ account ที่เปิด engine ถ้าต้องใช้หลาย account ให้แยกเป็นหลายคิว และติดตั้ง engine แยกกัน
- **งาน UI automation** ให้แยกไว้คิวของตัวเอง (เช่น `ui`) แล้วใช้ `--trigger logon` เครื่องต้องตั้ง auto-login และห้าม lock หน้าจอ
  ห้ามใช้ `boot` กับงานประเภทนี้
- task แบบ logon รันได้เฉพาะตอนที่เจ้าของ task login อยู่ ถ้า GUI ขึ้น `ui: ○ stopped` ทั้งที่ user ของ robot login อยู่ ให้ตรวจว่า task ผูกกับใคร
  `(Get-ScheduledTask 'Orch Engine (ui)').Principal.UserId` (PowerShell แบบ Administrator) ถ้าไม่ใช่ user ของ robot ให้รัน
  `setup.bat -Queue ui -Trigger logon -User DOMAIN\rpa` ใหม่ หมายเหตุ: `install-startup --trigger logon` ที่ไม่ใส่ `--user` จะใช้บัญชีที่รันคำสั่ง
  ซึ่งตอนรันแบบ Administrator มักเป็นบัญชี admin ไม่ใช่ user ของ robot
- engine ที่เปิดจาก Task Scheduler ไม่มีหน้าต่าง ให้ดู log ที่ `logs\engine-<คิว>.log` หรือดูสถานะที่แถบบนของ GUI
- task มี trigger สองตัว: ตอนบูต (หรือตอน logon) และอีกตัวที่ทำซ้ำทุก 5 นาที ถ้า engine ตาย (crash หรือถูก kill) จะถูกเปิดใหม่ภายใน 5 นาที
  ระหว่างที่ engine ยังรันอยู่ การเปิดซ้ำจะถูกข้าม และ task ไม่มีเวลาหมดอายุ (ไม่ถูกหยุดที่ 72 ชั่วโมง)
- **การหยุด engine ที่ติดตั้งไว้**
  - หยุดชั่วคราว (เช่น ให้ engine อ่านตัวแปรแวดล้อมใหม่): `python orch.py stop-engine --queue default` แล้ว engine จะกลับมาเองภายใน 5 นาที
    (หรือสั่ง `Start-ScheduledTask 'Orch Engine (default)'` ทันที)
  - หยุดถาวร หรือตอนอัปเดตไฟล์ `orch.py`: ใช้ PowerShell แบบ Administrator
    ```powershell
    Disable-ScheduledTask 'Orch Engine (default)'      # กันไม่ให้ trigger ที่ทำซ้ำเปิด engine ใหม่
    python C:\Orch\orch.py stop-engine --queue default
    # ... อัปเดตไฟล์ ...
    Enable-ScheduledTask 'Orch Engine (default)'; Start-ScheduledTask 'Orch Engine (default)'
    ```
  - **อย่าใช้ End ใน Task Scheduler, `Stop-ScheduledTask` หรือ kill ใน Task Manager** เพราะเป็นการหยุดแบบกะทันหัน
    งานที่รันอยู่จะจบเป็น `engine lost` (นับเป็น attempt) และโปรเซสของงานอาจค้างต่อ ส่วนการ reboot ก็ให้ผลแบบเดียวกัน
    จึงควรสั่ง `stop-engine` ก่อน reboot ถ้ามีงานสำคัญกำลังรัน
- **PATH ของ SYSTEM:** งาน `command` ของ engine แบบ boot เห็นเฉพาะ PATH ระดับเครื่อง ไม่เห็น PATH ของ user
  ให้ใส่ path เต็มของโปรแกรม (เช่น `"C:\Program Files\Python312\python.exe" C:\jobs\x.py`) หรือใช้งานชนิด `python` ซึ่งใช้ interpreter ตัวเดียวกับ engine
- **ยกเลิกการติดตั้ง:** `Unregister-ScheduledTask -TaskName 'Orch Engine (default)' -Confirm:$false` (PowerShell แบบ Administrator)
- ถ้าย้ายโฟลเดอร์ Orch หรือเปลี่ยนตำแหน่ง DB ต้องสั่ง `install-startup ... --apply` ใหม่ เพราะ task จำ path เดิมไว้

## 10. การตั้งค่าระบบ

### 10.1 ตำแหน่งฐานข้อมูล
ลำดับที่ระบบใช้:
1. `--db PATH` (ใส่ก่อนคำสั่ง เช่น `python orch.py --db D:\orch\orch.db query`)
2. ตัวแปร `ORCH_DB`
3. `orch.db` ในโฟลเดอร์เดียวกับ `orch.py`

ตั้งค่า `ORCH_DB`:

| Shell | คำสั่ง |
|---|---|
| Command Prompt | `set ORCH_DB=D:\orch\orch.db` (ยกเลิกด้วย `set ORCH_DB=`) |
| PowerShell | `$env:ORCH_DB = "D:\orch\orch.db"` |
| ถาวร (ทั้งเครื่อง) | `setx /M ORCH_DB D:\orch\orch.db` (Administrator แล้วเปิดหน้าต่างใหม่) |

> **สำคัญ:** CLI, GUI และ engine ต้องชี้ไปที่ DB **ไฟล์เดียวกัน** ถ้าหน้าต่างหนึ่งตั้ง `ORCH_DB` แต่อีกหน้าต่างไม่ได้ตั้ง
> จะเห็นงานไม่ตรงกัน ดูว่าใช้ไฟล์ไหนอยู่ได้จากชื่อหน้าต่าง GUI และจากบรรทัดแรกของ log engine (`db ...`)
> ส่วน `GUI --db` ใช้ `python orch_gui.py --db PATH`

### 10.2 ไฟล์ log
- log ของแต่ละ run เก็บใน DB ดูด้วย `logs RUN_ID` หรือใน GUI
- log ของ engine อยู่ที่ `logs\engine-<คิว>.log` ข้างไฟล์ DB (หมุนไฟล์ทุก 5 MB เก็บย้อนหลัง 5 ไฟล์)

### 10.3 ความลับ (connection string) และตัวแปรแวดล้อม
- ใช้ `--conn env:SALES_DB` แทนการใส่ connection string ตรง ๆ ระบบจะอ่านค่าตัวแปรตอนรันงาน
- ตัวแปรต้องอยู่ใน environment **ของ engine** ถ้า engine รันในนาม SYSTEM (boot task) ต้องตั้งเป็นตัวแปรระดับเครื่อง:
  ```cmd
  setx /M SALES_DB "Driver={ODBC Driver 18 for SQL Server};Server=db01;Database=sales;Trusted_Connection=yes;Encrypt=no"
  ```
  จากนั้น **รีสตาร์ท engine** (`python orch.py stop-engine --queue Q` แล้ว `Start-ScheduledTask` หรือรอไม่เกิน 5 นาที)
  เพราะ engine อ่าน environment ตอนเริ่มเท่านั้น
- ตัวแปรเฉพาะงานใส่ใน params `env` ได้ แต่ค่าจะถูกเก็บใน DB แบบข้อความธรรมดา
- `query --json` แสดง params ทั้งหมด ควรจำกัดสิทธิ์การเข้าถึงโฟลเดอร์ Orch
- log ของงาน `http` ตัด query string และ `user:password@` ออกจาก URL แล้ว

### 10.4 ภาษาไทยและ encoding
- `command`/`powershell` อ่าน output ด้วย code page ของเครื่อง (Windows ภาษาไทยใช้ `cp874`) ภาษาไทยจึงแสดงถูกต้องโดยไม่ต้องตั้งค่า
- ถ้าโปรแกรมที่เรียกเขียน output เป็น UTF-8 ให้ใส่ `"encoding": "utf-8"` ใน params
- `python`, `sql`, `http` ใช้ UTF-8 เสมอ
- ใส่ `chcp 65001` ไว้ในคำสั่งเดียวกันไม่ช่วยให้ `echo` ของ cmd เป็น UTF-8
- ถ้าเครื่องไม่ได้ตั้ง locale ภาษาไทย ตัว cmd/PowerShell เองจะเขียนภาษาไทยไม่ได้ ควรใช้งานชนิด `python` แทน

### 10.5 Proxy
งาน `http` ใช้ proxy ของระบบ ถ้าเรียก URL ภายในองค์กรไม่ได้เพราะ proxy ให้ใส่ `"env": {"no_proxy": "*"}` ใน params

## 11. ใช้งานจาก PowerShell

ทุกคำสั่งที่แสดงผลรองรับ `--json` (`query`, `runs`) จึงใช้ร่วมกับ `ConvertFrom-Json` ได้:
```powershell
cd C:\Orch

# งานทั้งหมดและเวลารันถัดไป
python orch.py query --json | ConvertFrom-Json | Format-Table name, job_type, queue, enabled, next_run_at

# run ที่ล้มเหลววันนี้
$today = Get-Date -Format 'yyyy-MM-dd'
python orch.py runs --status Dead --limit 200 --json | ConvertFrom-Json |
    Where-Object { $_.finished_at -like "$today*" } | Format-Table id, job_name, error

# สั่งรันแล้วรอจนจบ
$id = ((python orch.py run etl) -split '#')[1].Split(' ')[0]
python orch.py logs $id --follow

# สร้างงานจากรายการ
@('site-a','site-b') | ForEach-Object {
    python orch.py create "ping-$_" --type http --url "https://$_.example.com/health" --every 5m
}
```
exit code: 0 = สำเร็จ, 2 = ข้อมูลผิดหรือหางานไม่เจอ, 3 = engine ของคิวนั้นรันอยู่แล้ว, 4 = lock ของ engine ถูกยึดไประหว่างทำงาน

## 12. ย้ายงานจาก Windows Task Scheduler

| Task Scheduler | Orch |
|---|---|
| Daily 02:00 | `--cron "0 2 * * *"` |
| Weekly จันทร์ 07:30 | `--cron "30 7 * * 1"` |
| Monthly วันที่ 1 เวลา 06:00 | `--cron "0 6 1 * *"` |
| Repeat every 15 minutes | `--every 15m` (หรือ `--cron "*/15 * * * *"` ถ้าต้องการให้ตรงนาที 0/15/30/45) |
| One time | `--at "2026-12-31 23:00"` |
| At startup | `--startup` (รันเมื่อ engine เริ่ม ถ้าติดตั้ง engine แบบ boot ก็คือตอนเปิดเครื่อง) |
| Start a program `C:\x.bat` | `--type command --command "C:\x.bat" --cwd C:\` |
| Start `powershell -File x.ps1` | `--type powershell --file x.ps1` |
| Stop task if runs longer than | `--timeout` (วินาที) |
| If the task fails, restart every | `--retry` + `--retry-delay` |
| Run whether user is logged on or not | engine แบบ `--trigger boot` |
| Run only when user is logged on | engine แบบ `--trigger logon` (คิวแยก) |

ขั้นตอนแนะนำ:
1. สร้างงานใน Orch โดยใส่ `--disabled` ไว้ก่อน แล้วลองกด Run now จนผลถูกต้อง
2. ปิด (Disable) task เดิมใน Task Scheduler
3. `enable` งานใน Orch แล้วดูผลรอบแรกตามเวลาจริง
4. เมื่อมั่นใจแล้วจึงลบ task เดิม
5. ตรวจว่างานรันซ้ำได้โดยไม่เสียหาย (idempotent) เพราะ Orch อาจรันซ้ำเมื่อมีการ retry หรือการกู้คืนหลัง engine ตาย

## 13. แก้ปัญหา

| อาการ | สาเหตุที่เป็นไปได้ | วิธีแก้ |
|---|---|---|
| งานค้าง `Pending` ไม่รันเลย | ไม่มี engine ของคิวนั้นเปิดอยู่ | ดูแถบบนของ GUI ถ้าเป็น ○ stopped ให้เปิด engine ของคิวนั้น |
| | คิวถูก pause | `resume --queue Q` |
| | เป็นรอบ retry ที่ยังไม่ถึงเวลา `not_before` | รอ หรือดูเวลาใน `runs --json` |
| | งานอื่น priority สูงกว่าหรือรันนานอยู่ | ดูงานที่ `Running` ในคิวเดียวกัน |
| schedule ไม่ทำงาน | งานถูก disable หรือ schedule `at` รันไปแล้ว | `query` ดูช่อง ENABLED และ NEXT RUN |
| `error: job 'x' not found` ทั้งที่เพิ่งสร้าง | ใช้ DB คนละไฟล์ (เช่น ตั้ง `ORCH_DB` ในหน้าต่างเดียว) | ดู[หัวข้อ 10.1](#101-ตำแหน่งฐานข้อมูล) ล้างด้วย `set ORCH_DB=` |
| engine ออกทันทีพร้อม `already running` | มี engine ของคิวนี้เปิดอยู่แล้ว (อาจเป็นตัวที่ Task Scheduler เปิดไว้) | ใช้ตัวเดิม หรือหยุดตัวเดิมด้วย `stop-engine` ก่อน ถ้า engine เดิมตายไปแล้ว lock จะหมดอายุเองใน 30 วินาที |
| `stop-engine`: `no running engine for queue` | ไม่มี engine ของคิวนั้นรันอยู่ (หรือตายไปแล้ว) | ดูแถบบนของ GUI ไม่ต้องทำอะไรเพิ่ม |
| engine กลับมาเองหลัง `stop-engine` | trigger ที่ทำซ้ำทุก 5 นาทีของ task | `Disable-ScheduledTask` ก่อน (ดู[หัวข้อ 9.2](#92-ตั้งให้-engine-เปิดเองตอนเปิดเครื่อง-แนะนำสำหรับเครื่องที่ใช้งานจริง)) |
| งาน `command` ใช้ได้ตอนรันเอง แต่ engine แบบ boot หาโปรแกรมไม่เจอ | SYSTEM ไม่เห็น PATH ของ user | ใส่ path เต็มของโปรแกรม |
| run จบเป็น `Dead` ด้วย `engine lost` | engine ตายระหว่างงานรัน (ไฟดับ, ถูก kill) | ถ้ามี WARN ว่า pid ยังมีชีวิต ให้ตรวจใน Task Manager ว่างานเดิมยังรันอยู่หรือไม่ |
| ภาษาไทยใน log เพี้ยน | encoding ไม่ตรงกับที่โปรแกรมเขียนออกมา | ดู[หัวข้อ 10.4](#104-ภาษาไทยและ-encoding) |
| `sql`: `pyodbc is not installed` | ยังไม่ได้ติดตั้ง หรือติดตั้งแบบ `--user` ซึ่ง SYSTEM มองไม่เห็น | `python -m pip install pyodbc` ใน prompt แบบ Administrator (ด้วย Python ตัวเดียวกับที่รัน engine) |
| `sql`: `environment variable 'X' is not set` | ตัวแปรไม่อยู่ใน environment ของ engine | ดู[หัวข้อ 10.3](#103-ความลับ-connection-string-และตัวแปรแวดล้อม) แล้วรีสตาร์ท engine |
| `sql`: `'GO' batch separators are not supported` | สคริปต์มี `GO` | แยกเป็นหลายงานแล้วต่อด้วย `--after` |
| `http`: `request failed` | DNS, firewall หรือ proxy | ลองใส่ `no_proxy` และทดสอบ URL จากเครื่องเดียวกัน |
| `install-startup --apply` ล้มเหลว | ไม่ได้เปิดแบบ Administrator | เปิด Command Prompt ด้วย Run as administrator |
| ดับเบิลคลิก `setup.bat` แล้วไม่มีอะไรเกิดขึ้น | กด No ที่ UAC หรือ account ไม่มีสิทธิ์ Administrator | รันใหม่แล้วกด Yes หรือให้ผู้ดูแลเครื่องรันให้ |
| setup: `ไม่พบ Python และเครื่องนี้ไม่มี winget` | Windows รุ่นเก่าหรือถูกปิด winget | วาง `python-3.12.x-amd64.exe` ไว้ข้าง `setup.bat` แล้วรันใหม่ |
| setup: winget ล้มเหลว | ไม่มีอินเทอร์เน็ตหรือติด proxy | ใช้ตัวติดตั้ง Python แบบ offline ตามด้านบน |
| setup: `ติดตั้ง pyodbc ไม่สำเร็จ` | ไม่มีอินเทอร์เน็ต | วางไฟล์ `pyodbc-*.whl` ไว้ข้าง `setup.bat` แล้วรันใหม่ (จำเป็นเฉพาะงาน `sql` กับ SQL Server) |
| setup ล้มกลางทางตอนอัปเดต | ดูขั้นที่ล้มใน `logs\setup-*.log` | สคริปต์เปิด engine เดิมกลับให้แล้ว แก้สาเหตุแล้วรัน `setup.bat` ใหม่ |
| PowerShell ปฏิเสธการรันสคริปต์ | นโยบายองค์กร (Group Policy) บังคับ ExecutionPolicy | ให้ IT อนุญาต หรือใช้วิธี[ติดตั้งเอง](#12-ติดตั้งเอง) |
| GUI เปิดไม่ขึ้นเมื่อดับเบิลคลิก `orch_gui.py` | Windows เปิดไฟล์ด้วย editor | ใช้ `orch_gui.bat` แทน |
| งาน `.bat` ที่เปิดโปรแกรมอื่นถูก kill แล้วโปรแกรมนั้นยังค้าง | เกิดได้เฉพาะช่วงสั้น ๆ ทันทีที่งานเริ่ม | kill เองใน Task Manager (ปกติระบบใช้ Job Object ฆ่าลูกหลานทั้งหมดให้แล้ว) |

## 14. สรุปคำสั่ง

```text
python orch.py [--db PATH] <คำสั่ง>

init                                    สร้างฐานข้อมูล
create NAME --type T [params] [options] [schedules]
change NAME [เฉพาะที่ต้องการเปลี่ยน]
query [NAME] [--json]                   รายการงาน
run NAME                                รันทันที
runs [--queue Q] [--status S] [--job NAME] [--limit N] [--json]
end RUN_ID                              ยกเลิก run
logs RUN_ID [--follow]                  ดู log
enable NAME | disable NAME | delete NAME
pause [--queue Q] | resume [--queue Q]
prune [--days N]                        ล้างประวัติ (ค่าเริ่มต้น 30 วัน)
engine [--queue Q] [--poll 1.0]         รัน engine ของคิว
stop-engine [--queue Q] [--wait SEC]    หยุด engine ของคิวอย่างปลอดภัย (งานที่รันอยู่กลับเป็น Pending)
install-startup --queue Q --trigger boot|logon [--user U] [--apply]

params : --command --file --args --cwd --conn --query --query-file --url --method
         --body --header K:V (ซ้ำได้) --expect --params JSON
options: --queue --priority --timeout --retry --retry-delay --backoff --dedup allow|skip|coalesce
         --missed skip|run_once|run_all --after JOB --after-on success|failure|always --desc --disabled
schedule: --cron EXPR --every INTERVAL --at 'YYYY-MM-DD HH:MM' --startup (ซ้ำได้)
```

ดูรายละเอียดของแต่ละคำสั่งด้วย `python orch.py <คำสั่ง> --help`

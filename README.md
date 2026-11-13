# cloud-status-watch

เครื่องมือส่วนตัวสำหรับเฝ้าสถานะ **สาธารณะ** ของ Microsoft 365, Outlook.com, Azure และ AWS
แล้วแจ้งเตือนเข้า Discord เมื่อมี incident ใหม่ สถานะเปลี่ยน หรือ incident ปิด

- รันบน GitHub Actions ทุก 10 นาที ไม่ต้องมีเซิร์ฟเวอร์
- ใช้ Python standard library อย่างเดียว ไม่ต้องติดตั้ง package
- ไม่ใช้ API key ของ vendor ใด ๆ อ่านเฉพาะหน้าสถานะที่เปิดให้ทุกคนดู
- เก็บ log เป็นไฟล์ JSONL ใน repo นี้เอง ไม่ต้องมี database

## แหล่งข้อมูล

| รหัสใน log | แหล่ง | URL | หมายเหตุ |
|---|---|---|---|
| `ms_json` | Microsoft 365 (JSON) | `https://status.cloud.microsoft/api/posts/mac` | แหล่งหลัก ให้ชื่อ incident |
| `ms_rss` | Microsoft 365 (RSS) | `https://status.cloud.microsoft/api/feed/mac` | ใช้คู่กับ JSON และเทียบกัน |
| `ms_consumer` | Outlook.com, OneDrive ฯลฯ | `https://status.cloud.microsoft/api/posts/m365Consumer` | รูปแบบข้อมูลยังไม่ได้ยืนยัน |
| `azure` | Azure Status | `https://rssfeed.azure.status.microsoft/en-us/status/feed/` | feed ว่าง = ไม่มีเหตุ |
| `aws` | AWS Health | `https://status.aws.amazon.com/rss/all.rss` | ทุก region ทุกบริการ |
| `upenn` | UPenn incident extract | ดูใน `watch.py` | ใช้เติม incident ID (EX…/MO…) เท่านั้น ดึงเฉพาะตอนมีเหตุ |

`ms_json` กับ `ms_rss` ถูกรวมเป็นเหตุการณ์เดียวชื่อ `ms365` เพื่อไม่ให้แจ้งซ้ำ

## ติดตั้ง

1. **สร้าง repo** บน GitHub แล้วนำไฟล์ทั้งหมดขึ้นไป
   โฟลเดอร์ `.github` เป็นโฟลเดอร์ซ่อน ถ้าอัปโหลดผ่านหน้าเว็บให้ตรวจว่าไฟล์ `.github/workflows/watch.yml` ขึ้นไปด้วย
2. **สร้าง Discord webhook**: Server Settings → Integrations → Webhooks → New Webhook เลือก channel แล้ว Copy Webhook URL
3. **ตั้ง secret** ที่ repo: Settings → Secrets and variables → Actions → New repository secret
   - `DISCORD_WEBHOOK_URL` (จำเป็น) = URL จากข้อ 2
   - `AWS_ALERT_FILTER` (ไม่บังคับ) = ดูหัวข้อตัวกรองด้านล่าง
4. **ทดสอบรอบแรก**: แท็บ Actions → cloud-status-watch → Run workflow
   รอบแรกจะส่งข้อความ "เริ่มเฝ้าระวัง" พร้อมค่าที่อ่านได้จากแต่ละแหล่ง

Webhook URL คือกุญแจโพสต์เข้า channel ห้าม commit ลง repo

ถ้าขั้นตอน commit ใน workflow ล้มเหลวเรื่องสิทธิ์ ให้ตรวจที่ Settings → Actions → General → Workflow permissions ว่าอนุญาต Read and write

## ตัวกรองการแจ้งเตือน AWS

ระบบบันทึกรายการ AWS **ทุก region** ลง log เสมอ ตัวกรองมีผลเฉพาะการส่ง Discord

- ไม่ตั้ง `AWS_ALERT_FILTER` = แจ้งทุกรายการ
- ตั้งเป็นคำคั่นด้วย comma เช่น `ap-southeast-1,ap-southeast-7,us-east-1` = แจ้งเฉพาะรายการที่มีคำใดคำหนึ่งอยู่ใน guid, หัวข้อ หรือคำอธิบาย
- รายการของบริการแบบ global (ไม่มี region ใน guid) แจ้งเสมอ
- รายการที่เก่ากว่า 24 ชั่วโมงไม่แจ้ง และแจ้งไม่เกิน 10 รายการต่อรอบ

ตัวกรองอยู่ใน secret จึงไม่ปรากฏในโค้ดหรือ log ของ repo

## Remark เรื่องแหล่งข้อมูล

นอกจาก incident ของ vendor ระบบจะแจ้ง remark (สีเทา ⚠️) เมื่อแหล่งข้อมูลเองน่าสงสัย และแจ้งอีกครั้ง (🟢) เมื่อกลับมาปกติ

| รหัส | ความหมาย | แจ้งเมื่อ |
|---|---|---|
| `blocked` | HTTP 401/403/429 อาจโดน block หรือจำกัดอัตรา | ทันที |
| `http_error` | เซิร์ฟเวอร์ต้นทางตอบ error อื่น | เกิดติดกัน 2 รอบ |
| `network` | เชื่อมต่อไม่ได้หรือหมดเวลา | เกิดติดกัน 2 รอบ |
| `parse` | อ่าน JSON/XML ไม่ได้ | ทันที |
| `shape` | อ่านได้แต่ไม่มี field ที่คาดไว้ รูปแบบอาจเปลี่ยน | ทันที |
| `mismatch_status` | JSON กับ RSS ของ Microsoft 365 บอกสถานะไม่ตรงกัน | ทันที |
| `mismatch_time` | JSON กับ RSS ลงเวลา update ต่างกันเกิน 90 นาที | ทันที |
| `stale` | ยังขึ้นว่ามีเหตุ แต่ไม่มี update เกิน 6 ชั่วโมง | ทันที |

Remark เดิมจะไม่ถูกแจ้งซ้ำทุกรอบ ระหว่างที่ remark ยังเปิดอยู่ ข้อความ incident จะมีบรรทัดท้ายบอกไว้ด้วย
ค่าเกณฑ์ทั้งหมดแก้ได้ที่ส่วนบนของ `watch.py`

## Log และ state

- `logs/events-YYYY-MM.jsonl` หนึ่งบรรทัดต่อหนึ่งเหตุการณ์ มีเวลา UTC และเวลาไทย แยกไฟล์รายเดือน
- `state/state.json` สถานะล่าสุดที่ใช้เทียบรอบถัดไป
- workflow จะ commit สองโฟลเดอร์นี้เมื่อมีการเปลี่ยนแปลง ประวัติ commit จึงเป็นหลักฐานเวลาอีกชั้นหนึ่ง
- ถ้าส่ง Discord ไม่สำเร็จ ระบบจะไม่บันทึก state รอบนั้น เพื่อให้รอบถัดไปส่งใหม่

ประเภทเหตุการณ์ใน log: `startup`, `new`, `update`, `resolved`, `aws`, `remark`, `remark_cleared`
รายการ AWS ที่ไม่ได้แจ้งจะมี `notified: false` พร้อม `skip_reason` (`filtered`, `old`, `cap`)

## ทดสอบบนเครื่อง

```bash
# ใช้ข้อมูลจำลอง ไม่เรียกเว็บ ไม่ส่ง Discord ไม่บันทึกอะไร
python3 watch.py --dry-run --fixtures tests/fixtures --now 2026-11-13T09:00:00Z

# เรียกเว็บจริง แต่พิมพ์ผลแทนการส่ง Discord
python3 watch.py --dry-run
```

ในโฟลเดอร์ fixture ไฟล์ที่มีข้อความ `HTTP 403` จะจำลองการโดน block และถ้าลบไฟล์ใดออกจะจำลองการเชื่อมต่อไม่ได้

## ข้อจำกัด

- **เห็นเฉพาะเหตุวงกว้าง** หน้าสถานะสาธารณะไม่แสดงเหตุที่กระทบเฉพาะบาง tenant หรือบางภูมิภาค
- **อาจช้ากว่าความจริง** ทั้งจากฝั่ง vendor และจาก GitHub Actions ซึ่งไม่รับประกันว่ารอบตั้งเวลาจะตรงนาที
- **endpoint ของ Microsoft ไม่มีเอกสารทางการ** อาจเปลี่ยนหรือปิดได้ ถ้าเกิดขึ้นจะเห็นเป็น remark `shape`, `parse` หรือ `blocked`
- **Incident ID มาจากเว็บบุคคลที่สาม** ถ้าหน้า UPenn ใช้ไม่ได้ ข้อความแจ้งเตือนยังส่งตามปกติ แต่ไม่มี ID
- **GitHub อาจปิด workflow ตั้งเวลา** ของ repo ที่ไม่มีความเคลื่อนไหวนาน ระบบนี้ commit `state.json` อย่างน้อยวันละครั้งจึงไม่ควรเข้าเงื่อนไข ถ้าการแจ้งเตือนเงียบผิดปกติให้ตรวจแท็บ Actions
- เป็นเครื่องมือช่วยรับรู้เหตุการณ์ ไม่ใช่แหล่งอ้างอิงทางการ ให้ยืนยันกับหน้าสถานะของ vendor ทุกครั้ง

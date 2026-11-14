#!/usr/bin/env python3
"""cloud-status-watch

เฝ้าสถานะ "สาธารณะ" ของ Microsoft 365 / Outlook.com / Azure / AWS / Claude
แล้วแจ้งเตือนเข้า Discord เมื่อมี incident ใหม่ สถานะเปลี่ยน หรือ incident ปิด
พร้อม remark เมื่อแหล่งข้อมูลเองมีปัญหา (โดน block, รูปแบบเปลี่ยน, ข้อมูลไม่ตรงกัน, ข้อมูลค้าง)

ใช้เฉพาะ Python standard library — ไม่ต้องติดตั้ง package เพิ่ม
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# ---------------------------------------------------------------- ค่าตั้งต้น
ICT = timezone(timedelta(hours=7))
UA = "cloud-status-watch/0.3.1 (personal status monitor)"
TIMEOUT = 25
MAX_BYTES = 3_000_000

SOURCES = {
    "ms_json": ("Microsoft 365 (JSON)", "https://status.cloud.microsoft/api/posts/mac"),
    "ms_rss": ("Microsoft 365 (RSS)", "https://status.cloud.microsoft/api/feed/mac"),
    "ms_consumer": ("Microsoft 365 consumer / Outlook.com", "https://status.cloud.microsoft/api/posts/m365Consumer"),
    "azure": ("Azure Status", "https://rssfeed.azure.status.microsoft/en-us/status/feed/"),
    "aws": ("AWS Health", "https://status.aws.amazon.com/rss/all.rss"),
    "claude": ("Claude Status", "https://status.claude.com/history.rss"),
    # ใช้เติม incident ID (EX…/MO…) เท่านั้น และดึงเฉพาะตอนมี incident ของ Microsoft 365
    "upenn": ("UPenn incident extract (เสริม)", "https://mailservices.isc.upenn.edu/computing/email/penno365/alerts/ms-incidents.html"),
}
PUBLIC_PAGE = {
    "ms365": "https://status.cloud.microsoft/",
    "ms_consumer": "https://status.cloud.microsoft/m365",
    "azure": "https://azure.status.microsoft/en-us/status/",
    "aws": "https://health.aws.amazon.com/health/status",
    "claude": "https://status.claude.com/",
}

OK_STATUSES = {
    "operational", "serviceoperational", "servicerestored", "available", "healthy",
    "normal", "falsepositive", "postincidentreviewpublished", "resolved", "ok", "good",
}
MISMATCH_MINUTES = 90      # JSON กับ RSS ลงเวลา update ต่างกันเกินนี้ = remark
STALE_HOURS = 6            # ยังขึ้นว่ามีเหตุ แต่ไม่มี update เกินนี้ = remark
OVERDUE_MINUTES = 60       # เลยเวลา "Next update by" ที่ต้นทางประกาศไว้เกินนี้ = remark
SUMMARY_LIMIT = 1200       # ความยาวเนื้อความประกาศ Microsoft ใน Discord (ตัดที่จบประโยค)
DISCORD_MAX_CHARS = 5500   # Discord จำกัดตัวอักษรรวมของ embeds ต่อข้อความไว้ที่ 6000
AWS_MAX_AGE_HOURS = 24     # รายการ AWS ที่เก่ากว่านี้ไม่แจ้งเตือน (บันทึก log อย่างเดียว)
AWS_MAX_ALERTS = 10        # กันข้อความท่วมในรอบเดียว
CLAUDE_MAX_AGE_HOURS = 24  # incident ของ Claude ที่ update ล่าสุดเก่ากว่านี้ไม่แจ้งเตือน (บันทึก log อย่างเดียว)
# ป้ายสถานะที่ถือว่าปิดเหตุ — ยืนยันจากข้อมูลจริงแล้วเฉพาะ Resolved ที่เหลือเป็นค่ามาตรฐานของ Statuspage
CLAUDE_CLOSED = {"resolved", "postmortem", "completed"}
ID_MAX_TRIES = 6           # จำนวนรอบที่ลองค้น incident ID ซ้ำต่อหนึ่งหัวข้อ (นอกเหนือจากตอนมี update)
FAIL_THRESHOLD = 2         # network/http error ต้องเกิดติดกันกี่รอบจึง remark

COLOR = {"new": 0xE74C3C, "update": 0xE67E22, "resolved": 0x2ECC71,
         "remark": 0x95A5A6, "info": 0x3498DB}

PROBLEM_TEXT = {
    "blocked": "ถูกปฏิเสธการเข้าถึง — อาจโดน block หรือจำกัดอัตราการเรียก",
    "http_error": "เซิร์ฟเวอร์ต้นทางตอบกลับด้วยข้อผิดพลาด",
    "network": "เชื่อมต่อไม่ได้หรือหมดเวลา",
    "parse": "อ่านข้อมูลไม่ได้ — รูปแบบอาจเปลี่ยน",
    "shape": "โครงสร้างข้อมูลไม่เป็นไปตามที่คาด — รูปแบบอาจเปลี่ยน",
    "mismatch_status": "ช่องทาง JSON กับ RSS รายงานสถานะไม่ตรงกัน",
    "mismatch_time": f"ช่องทาง JSON กับ RSS ลงเวลา update ต่างกันเกิน {MISMATCH_MINUTES} นาที",
    "stale": f"ยังแสดงว่ามีเหตุ แต่ไม่มี update เกิน {STALE_HOURS} ชั่วโมง — ข้อมูลอาจค้าง",
    "overdue": f"เลยเวลา update ถัดไปที่ต้นทางประกาศไว้เกิน {OVERDUE_MINUTES} นาที — ข้อมูลอาจค้าง",
}
IMMEDIATE = {"blocked", "parse", "shape", "mismatch_status", "mismatch_time", "stale", "overdue"}
TH_MONTH = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
            "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


# ---------------------------------------------------------------- เครื่องมือย่อย
class Problem(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def fmt_ict(dt):
    if not dt:
        return "ไม่ระบุ"
    d = dt.astimezone(ICT)
    return f"{d.day} {TH_MONTH[d.month - 1]} {d.year + 543} {d:%H:%M} น. (UTC+7)"


def parse_dt(value):
    if not value:
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def clean(text, limit=500):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clip_sentence(text, limit):
    """ตัดข้อความให้ไม่เกิน limit โดยจบที่ท้ายประโยค (ถ้าหาไม่ได้จึงตัดที่ช่องว่าง)"""
    if len(text) <= limit:
        return text
    cut = text[: limit - 2]
    ends = [m.end() for m in re.finditer(r"[.!?][\"')\]]?(?=\s)", cut) if m.end() >= limit // 2]
    if ends:
        return cut[: ends[-1]] + " …"
    return cut.rsplit(" ", 1)[0].rstrip(",;: ") + "…"


LABELS = ["User impact", "More info", "Current status", "Final status", "Next update by",
          "Scope of impact", "Start time", "End time", "Root cause", "Title"]
PARTS = {"impact": "User impact", "current": "Current status", "final": "Final status",
         "next": "Next update by", "start": "Start time", "end": "End time"}


def split_message(raw):
    """แยกประกาศของ Microsoft ตามป้ายกำกับ คืน dict ตาม PARTS (ไม่พบ = ข้อความว่าง)"""
    text = clean(raw, limit=20000)

    def part(label):
        stops = "|".join(l for l in LABELS if l != label)
        m = re.search(label + r"\s*:\s*(.+?)(?=\s(?:" + stops + r")\s*:|$)", text, re.I)
        return m.group(1).strip() if m else ""
    return {key: part(label) for key, label in PARTS.items()}


def first_sentence(text):
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0].rstrip(". ") if text else ""


def parse_when(text):
    """'Friday, November 13, 2026, at 10:00 AM UTC' -> datetime (อ่านไม่ได้คืน None)"""
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4}),? at (\d{1,2}:\d{2} [AP]M) UTC", text or "")
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%B %d, %Y %I:%M %p").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def fmt_next(text):
    """แปลงเวลาในประกาศเป็นเวลาไทย (ถ้าอ่านไม่ได้คืนข้อความเดิม)"""
    dt = parse_when(text)
    return fmt_ict(dt) if dt else (text or "ไม่ระบุ")


def post_fields(title, message):
    """คืน dict: title, summary, final, next, start, end
    ถ้า Title ว่าง ใช้ประโยคแรกของ User impact แทน
    summary = Current status ถ้าไม่มีใช้ Final status ถ้าไม่มีอีกใช้ข้อความทั้งก้อน"""
    p = split_message(message)
    body = p["current"] or p["final"] or clean(message, 20000)
    return {"title": clean(title, 250) or first_sentence(p["impact"])[:250],
            "summary": clip_sentence(body, SUMMARY_LIMIT), "final": clip_sentence(p["final"], SUMMARY_LIMIT),
            "next": p["next"], "start": p["start"], "end": p["end"]}


def period_field(rec):
    """field ช่วงเวลาของเหตุ สำหรับประกาศปิดเหตุ (มีเฉพาะเมื่อต้นทางระบุ Start/End time)"""
    rows = [f"{n} {fmt_next(rec[k])}" for n, k in (("เริ่ม", "start"), ("สิ้นสุด", "end")) if rec.get(k)]
    return [("ช่วงเวลาของเหตุ (ตามประกาศ)", "\n".join(rows), False)] if rows else []


def freshness(now, updated, next_text, was_code):
    """ประกาศที่ยังเปิดอยู่ดูค้างหรือไม่ — คืน (code, detail) หรือ None
    overdue = เลยเวลานัด update เกิน OVERDUE_MINUTES / stale = ไม่มี update เกิน STALE_HOURS
    ถ้าเคยแจ้งด้วย code ใดไว้และเงื่อนไขนั้นยังจริง จะคง code เดิมเพื่อไม่แจ้งซ้ำ"""
    due = parse_when(next_text)
    codes = []
    if due and now - due > timedelta(minutes=OVERDUE_MINUTES):
        codes.append("overdue")
    if updated and now - updated > timedelta(hours=STALE_HOURS):
        codes.append("stale")
    if not codes:
        return None
    detail = f"update ล่าสุด {fmt_ict(updated)}" + (f" / นัด update ถัดไป {fmt_ict(due)}" if due else "")
    return (was_code if was_code in codes else codes[0]), detail


def norm_status(value):
    return re.sub(r"[^a-z]", "", (value or "").lower())


def is_ok(value):
    return norm_status(value) in OK_STATUSES


def fetch(name, fixtures=None):
    """คืนข้อความดิบ หรือ raise Problem"""
    if fixtures is not None:
        path = Path(fixtures) / f"{name}.txt"
        if not path.exists():
            raise Problem("network", "ไม่มีไฟล์ fixture")
        text = path.read_text(encoding="utf-8")
        m = re.match(r"HTTP (\d{3})", text)
        if m:
            code = int(m.group(1))
            raise Problem("blocked" if code in (401, 403, 429) else "http_error", f"HTTP {code}")
        return text
    req = urllib.request.Request(SOURCES[name][1], headers={
        "User-Agent": UA, "Accept": "application/json, application/rss+xml, text/xml, text/html;q=0.8, */*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read(MAX_BYTES).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise Problem("blocked" if e.code in (401, 403, 429) else "http_error", f"HTTP {e.code}")
    except Exception as e:  # URLError, timeout, reset ฯลฯ
        raise Problem("network", type(e).__name__)


def rss_items(text):
    try:
        root = ET.fromstring(text.lstrip("﻿").strip())
    except ET.ParseError as e:
        raise Problem("parse", f"XML: {e}")
    if not root.tag.lower().endswith("rss") and root.find(".//channel") is None:
        raise Problem("shape", "ไม่พบ <channel>")
    out = []
    for item in root.iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()
        out.append({"title": get("title"), "link": get("link"), "guid": get("guid") or get("link") + get("title"),
                    "pub": parse_dt(get("pubDate")), "desc": get("description"), "status": get("status")})
    return out


# ---------------------------------------------------------------- ตัวอ่านแต่ละแหล่ง
EMPTY_POST = {"ok": True, "status": "ไม่มีประกาศ", "title": "", "summary": "", "final": "",
              "next": "", "start": "", "end": "", "updated": None}


def read_ms_json(text):
    try:
        data = json.loads(text)
    except ValueError as e:
        raise Problem("parse", f"JSON: {e}")
    if isinstance(data, list):
        if not data:
            return dict(EMPTY_POST)
        data = data[0]
    if not isinstance(data, dict) or "Status" not in data:
        raise Problem("shape", "ไม่พบ field Status")
    return {"ok": is_ok(data.get("Status")), "status": str(data.get("Status")),
            "updated": parse_dt(data.get("LastUpdatedTime") or data.get("AuthoredDateTime")),
            **post_fields(data.get("Title"), data.get("Message"))}


def read_ms_rss(text):
    items = rss_items(text)
    if not items:
        return dict(EMPTY_POST)
    bad = [i for i in items if i["status"] and not is_ok(i["status"])]
    if not bad and not any(i["status"] for i in items):
        raise Problem("shape", "ไม่พบ <status> ใน item")
    it = (bad or items)[0]
    # <title> ของ RSS เป็นชื่อกลาง ๆ (Microsoft Admin Center) จึงดึงหัวข้อจากเนื้อความแทน
    f = post_fields("", it["desc"])
    f["title"] = f["title"] or clean(it["title"], 250)
    return {"ok": not bad, "status": it["status"], "updated": it["pub"], **f}


def read_consumer(text):
    try:
        data = json.loads(text)
    except ValueError as e:
        raise Problem("parse", f"JSON: {e}")
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if lists else [data]
    rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    out = {}
    for r in rows:
        low = {k.lower(): v for k, v in r.items()}
        if "status" not in low:
            continue
        name = next((str(low[k]) for k in ("servicedisplayname", "serviceworkloadname", "servicename", "service",
                                            "workload", "name") if low.get(k)), "")
        if name:
            out[clean(name, 80)] = {"status": str(low["status"]),
                                    "updated": parse_dt(str(low.get("lastupdatedtime") or "")),
                                    **post_fields(low.get("title") or "", low.get("message") or "")}
    if not out:
        raise Problem("shape", "ไม่พบรายการบริการที่มี Status")
    return out


def read_claude(text):
    """Statuspage RSS: 1 item = 1 incident, pubDate = เวลา update ล่าสุด
    description เรียง update ใหม่สุดก่อน แต่ละก้อนคือ <p><small>เวลา</small><br><strong>สถานะ</strong> - ข้อความ</p>"""
    out, labelled = [], 0
    for i in rss_items(text):
        blocks = re.findall(r"<p>(.*?)</p>", i["desc"], re.S)
        m = re.search(r"<strong>(.*?)</strong>\s*-?\s*(.*)", blocks[0], re.S) if blocks else None
        label = clean(m.group(1), 40) if m else ""
        labelled += bool(label)
        body = clean(m.group(2) if m else i["desc"], 20000)
        out.append({"guid": i["guid"], "link": i["link"], "title": clean(i["title"], 250), "pub": i["pub"],
                    "label": label, "text": clip_sentence(body, SUMMARY_LIMIT),
                    "updates": len(blocks), "done": label.lower() in CLAUDE_CLOSED})
    if out and not labelled:
        raise Problem("shape", "ไม่พบป้ายสถานะ <strong> ใน description")
    return out


def aws_parts(guid):
    frag = guid.split("#", 1)[-1]
    m = re.match(r"^(?P<svc>.+?)(?:-(?P<region>[a-z]{2,3}(?:-gov|-iso[a-z]?)?-[a-z]+-\d+))?_(?P<ts>\d+)$", frag)
    if not m:
        return frag, "global"
    return m.group("svc"), m.group("region") or "global"


ID_RE = re.compile(r"\b([A-Z]{2}\d{5,8})\b")


def find_incident_id(page, title):
    """หา ID แบบ MO1234567 ที่อยู่ใกล้หัวข้อเดียวกันที่สุดในหน้า UPenn

    ไม่ผูกกับเครื่องหมายคั่น: รองรับ "ID - หัวข้อ", "ID: หัวข้อ" และแบบตาราง
    เลือก ID ที่อยู่ก่อนหัวข้อและใกล้ที่สุดก่อน ถ้าไม่มีจึงดู ID ที่ตามหลัง
    """
    want = re.sub(r"\s+", " ", title).strip().lower()[:70]
    if not want:
        return None
    flat = clean(page, limit=len(page) + 1)
    low, best = flat.lower(), None
    pos = low.find(want)
    while pos != -1:
        end = pos + len(want)
        before = list(ID_RE.finditer(flat, max(0, pos - 120), pos))
        if before:
            cand = (pos - before[-1].end(), before[-1].group(1))
        else:
            after = ID_RE.search(flat, end, end + 120)
            cand = (1000 + after.start() - end, after.group(1)) if after else None
        if cand and (best is None or cand < best):
            best = cand
        pos = low.find(want, pos + 1)
    return best[1] if best else None


# ---------------------------------------------------------------- Discord
def embed_size(e):
    return (len(e.get("title", "")) + len(e.get("description", "")) + len(e.get("footer", {}).get("text", ""))
            + sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", [])))


def batches(embeds):
    """แบ่งเป็นข้อความละไม่เกิน 10 embeds และไม่เกิน DISCORD_MAX_CHARS ตัวอักษร"""
    batch, size = [], 0
    for e in embeds:
        n = embed_size(e)
        if batch and (len(batch) == 10 or size + n > DISCORD_MAX_CHARS):
            yield batch
            batch, size = [], 0
        batch.append(e)
        size += n
    if batch:
        yield batch


def send_discord(webhook, embeds, dry_run):
    for chunk in batches(embeds):
        if dry_run:
            for e in chunk:
                print(f"\n--- [{e['title']}]\n{e.get('description', '')}")
                for f in e.get("fields", []):
                    print(f"  {f['name']}: {f['value']}")
                if e.get("footer"):
                    print(f"  ({e['footer']['text']})")
            continue
        body = json.dumps({"username": "cloud-status-watch", "embeds": chunk}).encode("utf-8")
        for attempt in (1, 2, 3):
            req = urllib.request.Request(webhook, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": UA})
            try:
                urllib.request.urlopen(req, timeout=TIMEOUT).read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(min(float(e.headers.get("Retry-After", "2")), 15))
                    continue
                raise
        time.sleep(0.6)


def embed(kind, title, desc="", fields=(), url=None, footer=None):
    e = {"title": title[:250], "description": desc[:3500], "color": COLOR[kind],
         "fields": [{"name": n, "value": str(v)[:1000] or "-", "inline": inline} for n, v, inline in fields]}
    if url:
        e["url"] = url
    if footer:
        e["footer"] = {"text": footer[:1900]}
    return e


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="พิมพ์ข้อความแทนการส่ง Discord และไม่บันทึก state/log")
    ap.add_argument("--fixtures", help="อ่านข้อมูลจากโฟลเดอร์นี้แทนการเรียกเว็บ (ใช้ทดสอบ)")
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--now", help="กำหนดเวลาปัจจุบัน (ISO 8601) สำหรับทดสอบ")
    ap.add_argument("--save", action="store_true", help="ใช้คู่กับ --dry-run เพื่อบันทึก state/log ด้วย")
    args = ap.parse_args()

    now = parse_dt(args.now) if args.now else datetime.now(timezone.utc)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and not args.dry_run:
        sys.exit("ไม่พบ DISCORD_WEBHOOK_URL — ตั้งค่าใน GitHub Secrets หรือใช้ --dry-run")
    aws_filter = [t.strip().lower() for t in os.environ.get("AWS_ALERT_FILTER", "").split(",") if t.strip()]

    state_path = Path(args.state_dir) / "state.json"
    first_run = not state_path.exists()
    state = {} if first_run else json.loads(state_path.read_text(encoding="utf-8"))
    health = state.setdefault("health", {})

    embeds, logs, problems, readings = [], [], {}, {}
    upenn_tried = False
    unchecked = set()   # remark ที่รอบนี้ตรวจไม่ได้ จึงยังไม่ถือว่าปิด

    def log(kind, source, **kw):
        logs.append({"ts_utc": iso(now), "ts_ict": now.astimezone(ICT).isoformat(timespec="seconds"),
                     "type": kind, "source": source, **kw})

    def load(name, reader):
        try:
            return reader(fetch(name, args.fixtures))
        except Problem as p:
            problems[name] = p
            return None

    # ---- Microsoft 365 (enterprise): รวม JSON + RSS เป็นเหตุการณ์เดียว
    mj, mr = load("ms_json", read_ms_json), load("ms_rss", read_ms_rss)
    recs = [(n, r) for n, r in (("JSON", mj), ("RSS", mr)) if r]
    prev = state.get("ms365", {"active": False})
    if recs:
        bad = [(n, r) for n, r in recs if not r["ok"]]
        active = bool(bad)
        src, rec = max(bad or recs, key=lambda x: x[1]["updated"] or datetime.min.replace(tzinfo=timezone.utc))
        # หัวข้อ: ใช้ของ JSON ก่อน เพราะเป็นชื่อ incident จริง (RSS ใช้ชื่อกลาง ๆ ว่า Microsoft Admin Center)
        title = mj["title"] if (mj and not mj["ok"] and mj["title"]) else rec["title"]
        cur = {"active": active, "status": rec["status"], "title": title,
               "updated": iso(rec["updated"]), "via": src, "incident_id": None}
        readings["Microsoft 365"] = f"{rec['status']} (จากช่องทาง {src})" if active else "ปกติ"
        if mj and mr:
            if mj["ok"] != mr["ok"]:
                problems["ms_crosscheck"] = Problem("mismatch_status", f"JSON={mj['status']} / RSS={mr['status']}")
            elif active and mj["updated"] and mr["updated"] and \
                    abs((mj["updated"] - mr["updated"]).total_seconds()) > MISMATCH_MINUTES * 60:
                problems["ms_crosscheck"] = Problem(
                    "mismatch_time", f"JSON {fmt_ict(mj['updated'])} / RSS {fmt_ict(mr['updated'])}")
        if active:
            late = freshness(now, rec["updated"], rec["next"], health.get("ms_stale", {}).get("code"))
            if late:
                problems["ms_stale"] = Problem(*late)

        kind = None
        if active and not prev.get("active"):
            kind = "new"
        elif active and any(prev.get(k) != cur[k] for k in ("status", "title", "updated")):
            kind = "update"
        elif not active and prev.get("active"):
            kind = "resolved"
        found_late = False
        if active:
            same = prev.get("title") == cur["title"]
            cur["incident_id"] = prev.get("incident_id") if same else None
            cur["id_tries"] = prev.get("id_tries", 0) if same else 0
            # ค้น ID เมื่อมี update และลองซ้ำอีกไม่เกิน ID_MAX_TRIES รอบถ้ายังไม่ได้
            if not cur["incident_id"] and cur["title"] and (kind or cur["id_tries"] < ID_MAX_TRIES):
                upenn_tried = True
                cur["id_tries"] = min(cur["id_tries"] + 1, ID_MAX_TRIES)
                try:
                    cur["incident_id"] = find_incident_id(fetch("upenn", args.fixtures), cur["title"])
                    result = "found" if cur["incident_id"] else "no_match"
                except Problem as p:
                    problems["upenn"] = p
                    result = f"fetch_failed:{p.code}"
                log("id_lookup", "ms365", result=result, incident_id=cur["incident_id"],
                    title=cur["title"], attempt=cur["id_tries"])
                found_late = bool(cur["incident_id"]) and not kind
        if found_late:
            embeds.append(embed("info", f"🔵 Microsoft 365: พบ Incident ID {cur['incident_id']}",
                                f"**{cur['title']}**", url=PUBLIC_PAGE["ms365"]))
        if kind:
            head = {"new": "🔴 Microsoft 365: พบ incident ใหม่", "update": "🟠 Microsoft 365: มี update",
                    "resolved": "🟢 Microsoft 365: กลับสู่สถานะปกติ"}[kind]
            shown = cur if active else prev
            fields = [("สถานะ", cur["status"], True),
                      ("Incident ID", shown.get("incident_id") or "ไม่มีในแหล่งข้อมูลสาธารณะ", True),
                      ("Update ล่าสุดของต้นทาง", fmt_ict(parse_dt(cur["updated"])), False)]
            if active:
                fields.append(("Update ถัดไป (ตามประกาศ)", fmt_next(rec.get("next")), False))
            else:
                fields += period_field(rec)
            fields.append(("ช่องทางที่ update ล่าสุด", src, True))
            body = rec["summary"] if active else rec["final"]
            embeds.append(embed(kind, head, f"**{shown.get('title') or '(ต้นทางไม่ระบุหัวข้อ)'}**\n{body}",
                                fields, PUBLIC_PAGE["ms365"]))
            log(kind, "ms365", status=cur["status"], title=shown.get("title"), updated=cur["updated"],
                incident_id=shown.get("incident_id"), via=src, notified=True)
        state["ms365"] = cur
    else:
        readings["Microsoft 365"] = "อ่านไม่ได้ทั้งสองช่องทาง"

    # ---- Microsoft 365 consumer / Outlook.com
    cons = load("ms_consumer", read_consumer)
    if cons is not None:
        prev_c = state.get("ms_consumer", {})
        down = [n for n, v in cons.items() if not is_ok(v["status"])]
        readings["Microsoft 365 consumer"] = f"มีปัญหา {len(down)} จาก {len(cons)} บริการ" if down else f"ปกติ ({len(cons)} บริการ)"
        for name, v in sorted(cons.items()):
            was = prev_c.get(name)
            if was == v["status"] or (was is None and is_ok(v["status"])):
                continue
            kind = "resolved" if is_ok(v["status"]) else ("update" if was and not is_ok(was) else "new")
            head = {"new": "🔴", "update": "🟠", "resolved": "🟢"}[kind] + f" Microsoft consumer: {name}"
            fields = [("สถานะ", v["status"], True), ("สถานะก่อนหน้า", was or "-", True)]
            if not is_ok(v["status"]):
                fields += [("Update ล่าสุดของต้นทาง", fmt_ict(v["updated"]), False),
                           ("Update ถัดไป (ตามประกาศ)", fmt_next(v["next"]), False)]
                body = v["summary"]
            else:
                fields += period_field(v)
                body = v["final"] or v["summary"]
            desc = f"**{v['title']}**\n{body}" if v["title"] else body
            embeds.append(embed(kind, head, desc, fields, PUBLIC_PAGE["ms_consumer"]))
            log(kind, "ms_consumer", key=name, status=v["status"], previous=was, title=v["title"], notified=True)
        state["ms_consumer"] = {n: v["status"] for n, v in cons.items()}
        was_code = health.get("consumer_stale", {}).get("code")
        late = {n: f for n in down for f in [freshness(now, cons[n]["updated"], cons[n]["next"], was_code)] if f}
        if late:
            codes = {c for c, _ in late.values()}
            code = was_code if was_code in codes else ("overdue" if "overdue" in codes else "stale")
            problems["consumer_stale"] = Problem(code, " | ".join(f"{n}: {d}" for n, (_, d) in sorted(late.items())))
    else:
        unchecked.add("consumer_stale")

    # ---- Azure (feed ว่าง = ไม่มีเหตุ)
    az = load("azure", rss_items)
    if az is not None:
        prev_a = state.get("azure", {})
        cur_a = {i["guid"]: {"title": clean(i["title"], 250), "pub": iso(i["pub"])} for i in az}
        readings["Azure"] = f"มีประกาศ {len(cur_a)} รายการ" if cur_a else "ปกติ (feed ว่าง)"
        for i in az:
            was, me = prev_a.get(i["guid"]), cur_a[i["guid"]]
            if was == me:
                continue
            kind = "update" if was else "new"
            embeds.append(embed(kind, ("🟠 Azure: มี update" if was else "🔴 Azure: พบประกาศใหม่"),
                                f"**{me['title']}**\n{clean(i['desc'])}",
                                [("เวลาประกาศ", fmt_ict(i["pub"]), False)], i["link"] or PUBLIC_PAGE["azure"]))
            log(kind, "azure", key=i["guid"], title=me["title"], updated=me["pub"], notified=True)
        for guid, was in prev_a.items():
            if guid not in cur_a:
                embeds.append(embed("resolved", "🟢 Azure: ประกาศถูกถอดออกจาก feed", f"**{was['title']}**",
                                    url=PUBLIC_PAGE["azure"]))
                log("resolved", "azure", key=guid, title=was["title"], notified=True)
        state["azure"] = cur_a

    # ---- AWS (feed เป็นประวัติ update — แจ้งเฉพาะรายการที่ยังไม่เคยเห็น)
    aws = load("aws", rss_items)
    if aws is not None:
        seeded = "aws" in state
        seen = set(state.get("aws", {}).get("seen", []))
        fresh = sorted((i for i in aws if i["guid"] not in seen),
                       key=lambda i: i["pub"] or datetime.min.replace(tzinfo=timezone.utc))
        sent = skipped = 0
        for i in fresh if seeded else []:
            svc, region = aws_parts(i["guid"])
            hay = " ".join((i["guid"], i["title"], i["desc"])).lower()
            too_old = bool(i["pub"]) and (now - i["pub"]) > timedelta(hours=AWS_MAX_AGE_HOURS)
            passes = region == "global" or not aws_filter or any(t in hay for t in aws_filter)
            notify = passes and not too_old and sent < AWS_MAX_ALERTS
            reason = None if notify else ("old" if too_old else "filtered" if not passes else "cap")
            if notify:
                done = "operating normally" in i["title"].lower() or "[resolved]" in i["title"].lower()
                kind = "resolved" if done else "new"
                embeds.append(embed(kind, ("🟢" if done else "🔴") + f" AWS: {svc} ({region})",
                                    f"**{clean(i['title'], 250)}**\n{clean(i['desc'])}",
                                    [("Region", region, True), ("Service", svc, True),
                                     ("เวลาประกาศ", fmt_ict(i["pub"]), False)], PUBLIC_PAGE["aws"]))
                sent += 1
            else:
                skipped += reason == "cap"
            log("aws", "aws", key=i["guid"], region=region, service=svc, title=clean(i["title"], 250),
                updated=iso(i["pub"]), notified=notify, skip_reason=reason)
        if skipped:
            embeds.append(embed("info", f"AWS: มีอีก {skipped} รายการที่ไม่ได้แสดง", "ดูรายละเอียดในไฟล์ log หรือหน้า AWS Health",
                                url=PUBLIC_PAGE["aws"]))
        readings["AWS"] = f"feed มี {len(aws)} รายการ, ใหม่รอบนี้ {len(fresh) if seeded else 0}"
        state["aws"] = {"seen": sorted(i["guid"] for i in aws)}

    # ---- Claude (feed เป็นประวัติ incident — ติดตามราย incident ด้วย guid, รอบแรกบันทึกโดยไม่แจ้ง)
    cl = load("claude", read_claude)
    if cl is not None:
        seeded = "claude" in state
        prev_cl = state.get("claude", {})
        cur_cl = {i["guid"]: {"label": i["label"], "pub": iso(i["pub"]), "title": i["title"]} for i in cl}
        open_n = sum(not i["done"] for i in cl)
        readings["Claude"] = (f"มี incident เปิดอยู่ {open_n} รายการ" if open_n
                              else f"ปกติ (ไม่มี incident เปิดอยู่, feed มี {len(cl)} รายการ)")
        order = sorted(cl, key=lambda i: i["pub"] or datetime.min.replace(tzinfo=timezone.utc))
        for i in order if seeded else []:
            was, me = prev_cl.get(i["guid"]), cur_cl[i["guid"]]
            if was and (was.get("label"), was.get("pub")) == (me["label"], me["pub"]):
                continue
            was_done = bool(was) and str(was.get("label", "")).lower() in CLAUDE_CLOSED
            if i["done"] and was_done:
                kind, head = "info", "🔵 Claude: มี update หลังปิดเหตุ"
            elif i["done"]:
                kind, head = "resolved", ("🟢 Claude: incident ปิดแล้ว" if was else "🟢 Claude: พบ incident ที่ปิดไปแล้ว")
            elif was:
                kind, head = "update", "🟠 Claude: มี update"
            else:
                kind, head = "new", "🔴 Claude: พบ incident ใหม่"
            too_old = bool(i["pub"]) and (now - i["pub"]) > timedelta(hours=CLAUDE_MAX_AGE_HOURS)
            if not too_old:
                embeds.append(embed(kind, head, f"**{i['title']}**\n{i['text']}",
                                    [("สถานะ", i["label"] or "ไม่ระบุ", True),
                                     ("จำนวน update ในประกาศ", i["updates"], True),
                                     ("Update ล่าสุดของต้นทาง", fmt_ict(i["pub"]), False)],
                                    i["link"] or PUBLIC_PAGE["claude"]))
            log(kind, "claude", key=i["guid"], status=i["label"], previous=(was or {}).get("label"),
                title=i["title"], updated=me["pub"], notified=not too_old, skip_reason="old" if too_old else None)
        if not seeded and not first_run:
            embeds.append(embed("info", "🔵 เริ่มเฝ้า Claude Status",
                                f"บันทึกรายการที่มีอยู่ {len(cl)} รายการโดยไม่แจ้งย้อนหลัง\nสถานะตอนนี้: {readings['Claude']}",
                                url=PUBLIC_PAGE["claude"]))
            log("source_added", "claude", reading=readings["Claude"])
        state["claude"] = cur_cl

    # ---- Remark เรื่องสุขภาพของแหล่งข้อมูล
    label = {**{k: v[0] for k, v in SOURCES.items()},
             "ms_crosscheck": "Microsoft 365 (เทียบ JSON กับ RSS)", "ms_stale": "Microsoft 365 (ความสดของข้อมูล)",
             "consumer_stale": "Microsoft 365 consumer (ความสดของข้อมูล)"}
    for name in label:
        p, was = problems.get(name), health.get(name, {})
        if p is None:
            # upenn ถูกเรียกเฉพาะบางรอบ จึงไม่ถือว่า "หาย" จนกว่าจะเรียกสำเร็จจริง
            if (name == "upenn" and not upenn_tried) or name in unchecked:
                continue
            if was.get("alerted"):
                note = ("ต้นทางมี update ใหม่ หรือปิดเหตุแล้ว" if was.get("code") in ("stale", "overdue")
                        else "แหล่งข้อมูลกลับมาอ่านได้ตามปกติ")
                embeds.append(embed("resolved", f"🟢 Remark ปิด: {label[name]}", note))
                log("remark_cleared", name, code=was.get("code"))
            health.pop(name, None)
            continue
        n = min(was.get("n", 0) + 1 if was.get("code") == p.code else 1, FAIL_THRESHOLD)
        alerted = was.get("alerted", False) and was.get("code") == p.code
        # upenn ถูกเรียกเป็นครั้งคราว จึงแจ้งทันทีโดยไม่รอให้พลาดติดกัน
        if not alerted and n >= (1 if (p.code in IMMEDIATE or name == "upenn") else FAIL_THRESHOLD):
            embeds.append(embed("remark", f"⚠️ Remark: {label[name]}", PROBLEM_TEXT[p.code],
                                [("รายละเอียด", p.detail or "-", False)],
                                footer="การแจ้งเตือนจากแหล่งนี้อาจไม่ครบหรือล่าช้า จนกว่า remark นี้จะปิด"))
            log("remark", name, code=p.code, detail=p.detail)
            alerted = True
        health[name] = {"code": p.code, "n": n, "alerted": alerted}

    open_remarks = [f"{label[k]}: {PROBLEM_TEXT[v['code']]}" for k, v in health.items() if v.get("alerted")]
    if open_remarks:
        for e in embeds:
            if e["color"] in (COLOR["new"], COLOR["update"]) and "footer" not in e:
                e["footer"] = {"text": ("Remark ที่ยังเปิดอยู่ — " + " | ".join(open_remarks))[:1900]}

    if first_run:
        lines = "\n".join(f"• **{k}**: {v}" for k, v in readings.items())
        embeds.insert(0, embed("info", "🔵 cloud-status-watch เริ่มเฝ้าระวัง",
                               f"ค่าที่อ่านได้ในรอบแรก ({fmt_ict(now)})\n{lines}"))
        log("startup", "system", readings=readings)

    # ---- ส่งก่อน แล้วค่อยบันทึก (ถ้าส่งไม่สำเร็จ รอบหน้าจะลองใหม่)
    if embeds:
        send_discord(webhook, embeds, args.dry_run)
    if args.dry_run and not args.save:
        print(f"\n[dry-run] {len(embeds)} ข้อความ, {len(logs)} บรรทัด log, ไม่บันทึก state")
        return
    state["last_run_date"] = now.astimezone(ICT).date().isoformat()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if logs:
        log_path = Path(args.log_dir) / f"events-{now.astimezone(ICT):%Y-%m}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            for row in logs:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"ส่ง {len(embeds)} ข้อความ, บันทึก log {len(logs)} บรรทัด")


if __name__ == "__main__":
    main()

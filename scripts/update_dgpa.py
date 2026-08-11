#!/usr/bin/env python3
from __future__ import annotations
import json, os, re, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
import requests
from bs4 import BeautifulSoup

ROOT = "https://www.dgpa.gov.tw"
REGIONS = {"高雄市", "臺南市"}
INCREMENTAL_PAGES = 2
OUT = Path(__file__).resolve().parents[1] / "data" / "dgpa_closures.json"
ACCEPTED_TEXTS = {
    "今天停止上班、停止上課。",
    "今天停止上班、停止上課。明天照常上班、照常上課。",
    "今天停止上班、停止上課。明天停止上班、停止上課。",
    "今天已達停止上班及上課標準。",
    "今天已達停止上班及上課標準。明天照常上班、照常上課。",
    "今天已達停止上班及上課標準。明天已達停止上班及上課標準。",
}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 GitHubActions DGPA public-data updater"})

def get(url: str) -> str:
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    # DGPA 的公告頁與 nds.html 附件均宣告 UTF-8；apparent_encoding
    # 會把部分繁體中文附件誤判成 Latin-1，造成地區名稱與判定文字亂碼。
    r.encoding = "utf-8"
    time.sleep(0.12)
    return r.text

def clean(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", s or "").strip()

def county_sentence(s: str) -> str:
    parts = [x for x in clean(s).split("。") if x]
    kept = []
    for part in parts:
        if (":" in part or "：" in part) and re.search(r"[區鄉鎮村里]", part):
            break
        kept.append(part)
    return "。".join(kept) + ("。" if kept else "")

def roc_date(text: str):
    m = re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", clean(text))
    return tuple(map(int, m.groups())) if m else None

def load_existing() -> dict:
    try:
        parsed = json.loads(OUT.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}

def main():
    existing = load_existing()
    existing_events = existing.get("events") if isinstance(existing.get("events"), list) else []
    mode = str(os.environ.get("DGPA_MODE", "incremental")).strip().lower()
    full_scan = mode == "full" or os.environ.get("DGPA_FULL_SCAN") == "1"
    max_pages = None if full_scan else max(1, int(os.environ.get("DGPA_INCREMENTAL_PAGES", str(INCREMENTAL_PAGES))))
    announcements = {}
    pages_scanned = 0
    scan_complete = False
    failures = []
    page = 1
    while max_pages is None or page <= max_pages:
        url = f"{ROOT}/informationlist?uid=374" + ("" if page == 1 else f"&page={page}")
        soup = BeautifulSoup(get(url), "html.parser")
        before = len(announcements)
        for a in soup.select("a[href]"):
            href = urljoin(ROOT, a.get("href", ""))
            if "information?uid=374" in href and "pid=" in href:
                announcements[href] = a.get_text(" ", strip=True)
        pages_scanned = page
        if page > 1 and len(announcements) == before:
            scan_complete = True
            pages_scanned = page - 1
            break
        page += 1

    if full_scan and (not scan_complete or not announcements):
        raise RuntimeError("DGPA full scan did not reach the end of the official archive")

    events = {}
    seen_months = []
    for detail_url, title in announcements.items():
        try:
            detail = BeautifulSoup(get(detail_url), "html.parser")
            nds_url = ""
            for a in detail.select("a[href]"):
                if a.get_text(strip=True).lower() == "nds.html":
                    nds_url = urljoin(ROOT, a.get("href", "")); break
            if not nds_url:
                continue
            nds = BeautifulSoup(get(nds_url), "html.parser")
            header = nds.select_one("div.Header_YMD")
            date = roc_date(header.get_text(" ", strip=True) if header else "")
            if not date:
                continue
            ry, month, day = date
            seen_months.append((ry, month))
            pid = parse_qs(urlparse(detail_url).query).get("pid", [""])[0]
            for tr in nds.select("tr"):
                cells = tr.select("td")
                if len(cells) < 2:
                    continue
                region = cells[0].get_text(" ", strip=True)
                if region not in REGIONS:
                    continue
                text = county_sentence(cells[1].get_text(" ", strip=True))
                if not text or "未達停止上班" in text or "照常上班、照常上課。今天" in text or text not in ACCEPTED_TEXTS:
                    continue
                key = f"{ry}-{month:02d}-{day:02d}-{region}"
                events[key] = {"rocYear": ry, "month": month, "day": day, "region": region, "officialText": text, "officialUrl": detail_url, "ndsUrl": nds_url, "pid": pid}
        except Exception as exc:
            failures.append((detail_url, exc))
            print(f"WARN {detail_url}: {exc}")

    if full_scan and failures:
        raise RuntimeError(f"DGPA full scan incomplete: {len(failures)} announcement(s) failed")
    if not seen_months and not existing.get("coverage"):
        raise RuntimeError("No dated DGPA announcements were parsed")
    if not full_scan:
        for event in existing_events:
            try:
                event_key = f"{int(event['rocYear'])}-{int(event['month']):02d}-{int(event['day']):02d}-{event['region']}"
            except (KeyError, TypeError, ValueError):
                continue
            if event_key not in events:
                events[event_key] = event
    existing_min = existing.get("coverage") if isinstance(existing.get("coverage"), dict) else {}
    existing_min_month = None
    try:
        existing_min_month = (int(existing_min["minRocYear"]), int(existing_min["minMonth"]))
    except (KeyError, TypeError, ValueError):
        pass
    existing_max_month = None
    try:
        existing_max_month = (int(existing_min["maxRocYear"]), int(existing_min["maxMonth"]))
    except (KeyError, TypeError, ValueError):
        pass
    min_candidates = [existing_min_month] if existing_min_month else []
    if seen_months:
        min_candidates.append(min(seen_months))
    if not min_candidates:
        raise RuntimeError("No DGPA coverage range could be determined")
    min_month = min(min_candidates) if not full_scan else min(seen_months)
    max_candidates = [x for x in (existing_max_month, max(seen_months) if seen_months else None) if x]
    last_event_month = max_candidates[-1] if max_candidates else min_month
    tz = timezone(timedelta(hours=8))
    updated_at = datetime.now(tz)
    current_month = (updated_at.year - 1911, updated_at.month)
    coverage_max = max(max_candidates + [current_month])
    sorted_events = sorted(events.values(), key=lambda x: (x["rocYear"], x["month"], x["day"], x["region"]), reverse=True)
    last_event = None
    if sorted_events:
        newest = sorted_events[0]
        last_event = {"rocYear": newest["rocYear"], "month": newest["month"], "day": newest["day"]}
    reported_pages_scanned = pages_scanned
    if not full_scan and existing.get("historyComplete") is True:
        try:
            reported_pages_scanned = max(pages_scanned, int(existing.get("pagesScanned", 0)))
        except (TypeError, ValueError):
            reported_pages_scanned = pages_scanned
    payload = {
        "schemaVersion": 2,
        "updatedAt": updated_at.isoformat(timespec="seconds"),
        "source": ROOT,
        "pagesScanned": reported_pages_scanned,
        "historyComplete": True if full_scan else bool(existing.get("historyComplete", False)),
        "coverage": {"minRocYear": min_month[0], "minMonth": min_month[1], "maxRocYear": coverage_max[0], "maxMonth": coverage_max[1]},
        "lastEvent": last_event,
        "events": sorted_events,
    }
    comparable_existing = {key: value for key, value in existing.items() if key != "updatedAt"}
    comparable_payload = {key: value for key, value in payload.items() if key != "updatedAt"}
    if comparable_existing == comparable_payload:
        print(f"No DGPA data change: {len(payload['events'])} events")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}: {len(payload['events'])} events, coverage {min_month}..{coverage_max}, last event {last_event}")

if __name__ == "__main__":
    main()

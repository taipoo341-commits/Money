#!/usr/bin/env python3
from __future__ import annotations
import json, re, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
import requests
from bs4 import BeautifulSoup

ROOT = "https://www.dgpa.gov.tw"
REGIONS = {"高雄市", "臺南市"}
MAX_PAGES = 30
OUT = Path(__file__).resolve().parents[1] / "data" / "dgpa_closures.json"
ACCEPTED = {
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
    r.encoding = r.apparent_encoding or "utf-8"
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

def main():
    announcements = {}
    pages_scanned = 0
    for page in range(1, MAX_PAGES + 1):
        url = f"{ROOT}/informationlist?uid=374" + ("" if page == 1 else f"&page={page}")
        soup = BeautifulSoup(get(url), "html.parser")
        before = len(announcements)
        for a in soup.select("a[href]"):
            href = urljoin(ROOT, a.get("href", ""))
            if "information?uid=374" in href and "pid=" in href:
                announcements[href] = a.get_text(" ", strip=True)
        pages_scanned = page
        if len(announcements) == before and page > 1:
            break

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
                if not text or "未達停止上班" in text or "照常上班、照常上課。今天" in text or text not in ACCEPTED:
                    continue
                key = f"{ry}-{month:02d}-{day:02d}-{region}"
                events[key] = {"rocYear": ry, "month": month, "day": day, "region": region, "officialText": text, "officialUrl": detail_url, "ndsUrl": nds_url, "pid": pid}
        except Exception as exc:
            print(f"WARN {detail_url}: {exc}")

    if not seen_months:
        raise RuntimeError("No dated DGPA announcements were parsed")
    min_month = min(seen_months)
    max_month = max(seen_months)
    tz = timezone(timedelta(hours=8))
    payload = {
        "schemaVersion": 1,
        "updatedAt": datetime.now(tz).isoformat(timespec="seconds"),
        "source": ROOT,
        "pagesScanned": pages_scanned,
        "coverage": {"minRocYear": min_month[0], "minMonth": min_month[1], "maxRocYear": max_month[0], "maxMonth": max_month[1]},
        "events": sorted(events.values(), key=lambda x: (x["rocYear"], x["month"], x["day"], x["region"]), reverse=True),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}: {len(payload['events'])} events, coverage {min_month}..{max_month}")

if __name__ == "__main__":
    main()

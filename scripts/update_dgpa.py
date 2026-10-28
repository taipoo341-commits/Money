#!/usr/bin/env python3
from __future__ import annotations
import codecs, hashlib, json, os, re, subprocess, time, uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
import requests
from bs4 import BeautifulSoup

ROOT = "https://www.dgpa.gov.tw"
DAILY_URL = f"{ROOT}/typh/daily/nds.html"
REGIONS = {"高雄市", "臺南市"}
COUNTIES = {"臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市", "基隆市",
            "新竹市", "嘉義市", "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣",
            "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣",
            "臺北縣", "桃園縣", "臺中縣", "臺南縣", "高雄縣"}
INCREMENTAL_PAGES = 2
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT = PROJECT_ROOT / "data" / "dgpa_closures.json"
STATE_PATH = OUT.with_name("dgpa_review_state.json")
REPORT_PATH = OUT.with_name("dgpa_review_report.json")
PARSER_VERSION = "4-" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
DEADLINE = None
DAILY_ERROR_MESSAGE = "網頁異常，請改手動輸入"
MANUAL_TYPE_MESSAGE = "請手動選擇停止辦公日類別"

# 保留標準句庫以維持相容性，並加入 114/7/6 常見之組合
ACCEPTED_TEXTS = {
    "今天停止上班、停止上課。",
    "今天停止上班、停止上課。明天照常上班、照常上課。",
    "今天停止上班、停止上課。明天停止上班、停止上課。",
    "今天已達停止上班及上課標準。",
    "今天已達停止上班及上課標準。明天照常上班、照常上課。",
    "今天已達停止上班及上課標準。明天停止上班、停止上課。",
    "今天已達停止上班及上課標準。明天已達停止上班及上課標準。",
}

SESSION = requests.Session()
SESSION.verify = True
SESSION.headers.update({"User-Agent": "Mozilla/5.0 GitHubActions DGPA public-data updater"})

class BudgetReached(Exception):
    """正常收尾並保留進度，不把時間預算耗盡當成資料錯誤."""


class CheckpointError(RuntimeError):
    pass


def get(url: str, retries: int = 3) -> str:
    """有上限的重試；HTTPS 憑證錯誤不得降級成略過驗證."""
    if urlparse(url).scheme != "https" or urlparse(url).netloc != urlparse(ROOT).netloc:
        raise ValueError("Unexpected non-official source URL")
    for attempt in range(retries):
        remaining = DEADLINE - time.monotonic() if DEADLINE is not None else 120
        if remaining < 8:
            raise BudgetReached()
        try:
            response = SESSION.get(url, timeout=(min(10, remaining), min(30, remaining)))
            response.raise_for_status()
            if urlparse(response.url).netloc != urlparse(ROOT).netloc:
                raise ValueError("Unexpected cross-site redirect")
            data = response.content
            meta = re.search(rb"<meta\b[^>]*charset\s*=\s*[\"']?\s*([a-zA-Z0-9_-]+)", data[:8192], re.I)
            declared = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)",
                                 response.headers.get("Content-Type", ""), re.I)
            encoding = ("utf-8-sig" if data.startswith(codecs.BOM_UTF8) else
                        meta.group(1).decode("ascii") if meta else
                        declared.group(1) if declared else "utf-8")
            encoding = "cp950" if encoding.lower() in {"big5", "big-5", "x-big5"} else encoding
            decoded = data.decode(encoding, errors="strict")
            if "\ufffd" in decoded:
                raise ValueError("Official source contains replacement characters")
            time.sleep(0.12)
            return decoded
        except requests.RequestException:
            if attempt + 1 == retries:
                raise
            time.sleep(1.2 * (attempt + 1))

def clean(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", s or "").strip()

def county_sentence(s: str) -> str:
    parts = [x for x in clean(s).split("。") if x]
    kept = []
    for part in parts:
        if (":" in part or "：" in part) and re.search(r"[區鄉鎮村里校園站]", part):
            break
        kept.append(part)
    return "。".join(kept) + ("。" if kept else "")

def roc_date(text: str):
    m = re.search(r"(?<!\d)(\d{2,3})年(\d{1,2})月(\d{1,2})日", clean(text))
    return tuple(map(int, m.groups())) if m else None

def nds_region_texts(soup: BeautifulSoup) -> dict[str, str]:
    table = soup.select_one("table#Table")
    if table is None:
        raise RuntimeError("DGPA nds table was not found")
    rows = {}
    for tr in table.select("tr"):
        cells = tr.select("td")
        if len(cells) < 2:
            continue
        # 正規化縣市名稱（相容 台南市 -> 臺南市）
        region = cells[0].get_text(" ", strip=True).replace("台", "臺")
        if region in REGIONS:
            rows[region] = county_sentence(cells[1].get_text(" ", strip=True))
    return rows

def is_full_county_stop_closure(text: str) -> bool:
    """
    判定全縣市主幹文字是否為全日停止上班。
    只要今日首句為「今天停止上班、停止上課」或「今天已達停止上班及上課標準」，即判定為停班日，
    不受明日接續敘述（如明天停止上班、明天照常上班）影響。
    """
    if not text:
        return False
    parts = [x for x in clean(text).split("。") if x]
    if not parts:
        return False
    first = parts[0].replace("辦公", "上班")
    if any(k in first for k in ["未達停止上班", "照常上班", "尚未列入警戒區"]):
        return False
    if first in {"今天停止上班、停止上課", "今天已達停止上班及上課標準"}:
        return True
    return text in ACCEPTED_TEXTS

def is_manual_type_text(text: str) -> bool:
    """
    包含停止上班但非全日停班（例如時段性停班：中午12:00起、下午、晚上起等）
    """
    if not text:
        return False
    if is_full_county_stop_closure(text):
        return False
    parts = [x for x in clean(text).split("。") if x]
    first = (parts[0] if parts else text).replace("辦公", "上班")
    if any(k in first for k in ["未達停止上班", "照常上班", "尚未列入警戒區"]):
        return False
    return "今天" in first and "停止上班" in first

def official_updated_at(soup: BeautifulSoup) -> str:
    match = re.search(
        r"更新時間[：:]\s*(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2})",
        soup.get_text(" ", strip=True),
    )
    return match.group(1) if match else ""

def regional_disposition(raw_text):
    text = county_sentence(raw_text)
    if clean(raw_text) == "無停班停課訊息。":
        return "no-announcement"
    first = clean(text).split("。")[0].replace("辦公", "上班")
    if is_full_county_stop_closure(text):
        return "full-day"
    if is_manual_type_text(text):
        return "manual-closure-type"
    if first.startswith("明天"):
        return "tomorrow-only"
    if any(word in first for word in ("未達停止上班", "照常上班", "尚未列入警戒區",
                                      "尚未宣布", "尚未發布", "尚未宣佈")):
        return "no-full-day-closure"
    if "停止上班" in clean(raw_text).replace("辦公", "上班") or "停班" in clean(raw_text):
        return "manual-closure-type"
    if "停止上課" in clean(raw_text) or "照常上課" in clean(raw_text):
        return "school-only"
    raise ValueError(f"Unrecognized regional announcement: {raw_text[:120]}")

def load_daily_snapshot(checked_at: datetime):
    daily_date = None
    daily_events = {}
    daily_review_items = []
    try:
        daily = BeautifulSoup(get(DAILY_URL), "html.parser")
        header = daily.select_one("div.Header_YMD")
        daily_date = roc_date(header.get_text(" ", strip=True) if header else "")
        if not daily_date:
            raise RuntimeError("DGPA daily date was not found")
        ry, month, day = daily_date
        if daily_date != (checked_at.year - 1911, checked_at.month, checked_at.day):
            raise RuntimeError("DGPA daily source date is stale")
        daily_rows = raw_region_texts(daily)
        for region, raw_text in daily_rows.items():
            disposition = regional_disposition(raw_text)
            text = county_sentence(raw_text) or clean(raw_text)
            key = f"{ry}-{month:02d}-{day:02d}-{region}"
            if disposition == "full-day":
                daily_events[key] = {"rocYear": ry, "month": month, "day": day, "region": region, "officialText": text, "officialUrl": DAILY_URL, "ndsUrl": DAILY_URL, "pid": "", "sourceKind": "daily"}
            elif disposition == "manual-closure-type":
                daily_review_items.append({"rocYear": ry, "month": month, "day": day, "region": region, "officialText": text, "officialUrl": DAILY_URL, "reason": "manual-closure-type", "message": MANUAL_TYPE_MESSAGE})
        daily_status = {
            "ok": True,
            "sourceDate": {"rocYear": ry, "month": month, "day": day},
            "officialUpdatedAt": official_updated_at(daily),
            "checkedAt": checked_at.isoformat(timespec="seconds"),
            "message": "",
            "regionsCovered": sorted(daily_rows),
        }
    except BudgetReached:
        raise
    except Exception as exc:
        daily_status = {
            "ok": False,
            "sourceDate": None,
            "officialUpdatedAt": "",
            "checkedAt": checked_at.isoformat(timespec="seconds"),
            "message": DAILY_ERROR_MESSAGE,
            "errorCode": "network-error" if isinstance(exc, requests.RequestException) else "format-error",
        }
        print(f"WARN {DAILY_URL}: {exc}")
    return daily_date, daily_events, daily_review_items, daily_status


def now_text():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="microseconds")


def read_json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid JSON object: {path.name}")
    return value


def load_existing():
    value = read_json(OUT)
    if value and not isinstance(value.get("events"), list):
        raise ValueError("Existing official snapshot has no events array")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def event_key(event):
    return (int(event["rocYear"]), int(event["month"]), int(event["day"]), event["region"])


def item_key(item):
    return event_key(item) + (item.get("reason", ""),)


def date_key(value):
    return (int(value["rocYear"]), int(value["month"]), int(value["day"]))


def archive_links(soup):
    found = {}
    for anchor in soup.select("a[href]"):
        parsed = urlparse(urljoin(ROOT, anchor.get("href", "")))
        query = parse_qs(parsed.query)
        pid = query.get("pid", [""])[0]
        if (parsed.netloc == urlparse(ROOT).netloc and parsed.path == "/information"
                and query.get("uid") == ["374"] and pid.isdigit()):
            found[pid] = {"pid": pid, "url": f"{ROOT}/information?uid=374&pid={pid}",
                          "title": anchor.get_text(" ", strip=True)}
    return found


def list_page(page):
    url = f"{ROOT}/informationlist?uid=374" + (f"&page={page}" if page > 1 else "")
    soup = BeautifulSoup(get(url), "html.parser")
    text = soup.get_text(" ", strip=True)
    count = re.search(r"共\s*([\d,]+)\s*筆", text)
    pagination = re.search(r"第\s*(\d+)\s*/\s*(\d+)\s*頁", text)
    if not count or not pagination:
        raise ValueError("Archive pagination/count cannot be verified")
    actual, pages = map(int, pagination.groups())
    if actual != page or pages < 1:
        raise ValueError("Archive returned a different page")
    links = archive_links(soup)
    if not links:
        raise ValueError("Archive page has no announcement links")
    return links, int(count.group(1).replace(",", "")), pages


def normalized_label(value):
    return clean(value).replace("台", "臺").replace("巿", "市")


def has_official_headers(table):
    # 早期使用 td+rowspan、後期使用 th；仍要求兩個明確欄名。
    for row in table.find_all("tr", recursive=False) or table.select("tr"):
        labels = {normalized_label(cell.get_text(" ", strip=True))
                  for cell in row.find_all(["td", "th"], recursive=False)}
        if "縣市名稱" in labels and any(
                re.fullmatch(r"是否停止(?:上班|辦公)(?:及)?上課情形", label) for label in labels):
            return True
    return False


def official_table(soup):
    table = soup.select_one("table#Table")
    if table is not None:
        return table
    candidates = [table for table in soup.select("table") if has_official_headers(table)]
    if not candidates:
        raise ValueError("Official announcement table was not found")
    # 舊網頁外層排版 table 也包含內層資料，選最內層的有效表。
    return min(candidates, key=lambda table: len(table.select("table")))


def raw_region_texts(soup):
    table = official_table(soup)
    rows, all_rows, unknown_rows = {}, {}, []
    for row in table.select("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        labels = [normalized_label(cell.get_text(" ", strip=True)) for cell in cells]
        if "縣市名稱" in labels:
            continue  # 舊版 td 表頭。
        county_indices = [index for index, label in enumerate(labels) if label in COUNTIES]
        if len(county_indices) > 1:
            raise ValueError("Misaligned/duplicate county cells in an official row")
        if county_indices:
            index = county_indices[0]
            region = labels[index]
            if index + 1 >= len(cells):
                raise ValueError("Official region row has no status cell")
            if region in all_rows:
                raise ValueError(f"Duplicate region row: {region}")
            text = cells[index + 1].get_text(" ", strip=True)
            if not clean(text):
                raise ValueError(f"Empty official region row: {region}")
            all_rows[region] = text
            if region in REGIONS:
                rows[region] = text
        elif len(cells) >= 2:
            unknown_rows.append(labels[0])
    global_empty = any(clean(cell.get_text(" ", strip=True)) == "無停班停課訊息。"
                       for cell in table.select("td[colspan]"))
    if global_empty and (all_rows or unknown_rows):
        raise ValueError("Conflicting global-empty marker and regional announcement rows")
    if global_empty:
        return {region: "無停班停課訊息。" for region in REGIONS}
    if all_rows and not unknown_rows and has_official_headers(table):
        return rows  # 只列已發布縣市時，未列目標是 not-listed，不能視為正常上班。
    if unknown_rows or set(rows) != REGIONS:
        raise ValueError("DGPA target region rows are missing/empty")
    return rows


def attachment_date(soup):
    header = soup.select_one("div.Header_YMD")
    if header is not None:
        result = roc_date(header.get_text(" ", strip=True))
        if result:
            return result, "Header_YMD"
    # 舊版日期放在 title / 大字體 / 外層排版儲存格，必須在附件本文找到，不能用列表標題代替。
    candidates = soup.find_all(["title", "h1", "h2", "h3", "td", "p", "span", "font"])
    for node in sorted(candidates, key=lambda node: len(node.get_text(" ", strip=True))):
        text = node.get_text(" ", strip=True)
        if len(clean(text)) < 200 and "天然災害" in text and "情形" in text:
            result = roc_date(text)
            if result:
                return result, "attachment-heading"
    raise ValueError("Missing announcement date in attachment")


def chinese_attachments(detail):
    found = {}
    for anchor in detail.select("a[href]"):
        href = urljoin(ROOT, anchor.get("href", ""))
        label = anchor.get_text(" ", strip=True)
        name = parse_qs(urlparse(href).query).get("name", [""])[0]
        names = [label, name]
        if any("english" in text.lower() or "英文" in text
               or re.search(r"nds[eE]", text) for text in names):
            continue
        if "daily/nds.html" in href.lower():
            continue
        recognized = any(re.fullmatch(r"nds(?:_\d+)?\.html?", text.strip(), re.I)
                         or (text.lower().endswith((".html", ".htm"))
                             and "天然災害" in text and "停止" in text) for text in names)
        if recognized:
            found[href] = {"url": href, "label": label or name}
    return list(found.values())


def parse_inline_announcement(entry, detail, detail_html):
    heading = detail.select_one("h2.main-title")
    paragraphs = [node.get_text(" ", strip=True) for node in detail.select("#editor p.word_p1")
                  if clean(node.get_text(" ", strip=True))]
    title = heading.get_text(" ", strip=True) if heading else ""
    parsed_date = roc_date(title)
    if not parsed_date or "天然災害" not in title or "情形" not in title or not paragraphs:
        raise ValueError("Missing Chinese attachment or verifiable official inline content")
    body = "\n".join(paragraphs)
    normalized = normalized_label(body).replace("辦公", "上班")
    if any(word in normalized for word in ("全國", "全臺", "全台", "全省")):
        raise ValueError("Unresolved nationwide scope in inline notice")
    mentioned = {county for county in COUNTIES if county in normalized}
    if not mentioned:
        raise ValueError("Inline notice has no explicit recognized county scope")
    body_dates = {tuple(map(int, match)) for match in
                  re.findall(r"(?<!\d)(\d{2,3})年(\d{1,2})月(\d{1,2})日", clean(body))}
    if len(body_dates) > 1:
        raise ValueError("Inline notice has multiple effective dates; requires investigation")
    date_source = "inline-body" if body_dates else "inline-official-heading"
    if body_dates:
        parsed_date = next(iter(body_dates))
    ry, month, day = parsed_date
    datetime(ry + 1911, month, day)
    date = {"rocYear": ry, "month": month, "day": day}
    observations, reviews, dispositions = [], [], {}
    regional_texts = {region: [] for region in REGIONS}
    county_pattern = re.compile("|".join(re.escape(county) for county in sorted(COUNTIES)))
    for paragraph in paragraphs:
        normalized_paragraph = paragraph.replace("台", "臺").replace("巿", "市")
        matches = list(county_pattern.finditer(normalized_paragraph))
        for index, match in enumerate(matches):
            if match.group() in REGIONS:
                end = matches[index + 1].start() if index + 1 < len(matches) else len(paragraph)
                regional_texts[match.group()].append(paragraph[match.start():end])
    for region in REGIONS:
        relevant = regional_texts[region]
        if not relevant:
            if region in mentioned:
                raise ValueError(f"Cannot isolate inline county scope: {region}")
            dispositions[region] = "not-listed"
            continue
        raw_text = "\n".join(relevant)
        comparable = normalized_label(raw_text).replace("辦公", "上班")
        # 正文往往同時有「照常上班」與特定學校停班；局部正向訊息不可被負向句吞掉。
        positive = comparable.replace("不停止上班", "").replace("不停班", "")
        if "停止上班" in positive or "停班" in positive:
            disposition = "manual-closure-type"
        elif any(word in comparable for word in ("照常上班", "不停班", "不停止上班", "停止上課", "停課")):
            disposition = "school-only"
        else:
            raise ValueError(f"Unrecognized inline regional text: {region}")
        review = ({**date, "region": region, "officialText": clean(raw_text),
                   "officialUrl": entry["url"], "ndsUrl": entry["url"], "pid": entry["pid"],
                   "sourceKind": "archive", "reason": "manual-closure-type", "message": MANUAL_TYPE_MESSAGE}
                  if disposition == "manual-closure-type" else None)
        if review:
            reviews.append(review)
        dispositions[region] = disposition
        observations.append({"pid": entry["pid"], "date": date, "region": region,
                             "disposition": disposition, "officialUpdatedAt": "",
                             "attachmentOrder": 0, "ndsUrl": entry["url"],
                             "rawText": raw_text, "event": None, "reviewItem": review})
    digest = hashlib.sha256(detail_html.encode("utf-8")).hexdigest()
    return {**entry, "status": "done", "checkedAt": now_text(), "parserVersion": PARSER_VERSION,
            "date": date, "dates": [date], "detailSha256": digest, "events": [],
            "reviewItems": reviews, "observations": observations, "dispositions": dispositions,
            "attachments": [{"kind": "inline", "url": entry["url"], "label": title,
                             "date": date, "dateSource": date_source, "rawBody": paragraphs,
                             "sha256": digest}],
            "rawRegionTexts": {region: "\n".join(texts) for region, texts in regional_texts.items()}}


def select_observation(candidates):
    # 同一組必須採一致全序，不能兩兩混用時間/PID，否則結果取決於處理順序。
    dated = all(value.get("officialUpdatedAt") for value in candidates)
    return max(candidates, key=lambda value: (
        value.get("officialUpdatedAt", "") if dated else "",
        int(value.get("pid", 0)), value.get("attachmentOrder", 0)))


def parse_announcement(entry):
    detail_html = get(entry["url"])
    detail = BeautifulSoup(detail_html, "html.parser")
    source_attachments = chinese_attachments(detail)
    if not source_attachments:
        return parse_inline_announcement(entry, detail, detail_html)
    observations, attachments, all_dates = {}, [], set()
    for index, attachment in enumerate(source_attachments):
        nds_html = get(attachment["url"])
        nds = BeautifulSoup(nds_html, "html.parser")
        parsed_date, date_source = attachment_date(nds)
        ry, month, day = parsed_date
        datetime(ry + 1911, month, day)
        all_dates.add(parsed_date)
        rows = raw_region_texts(nds)
        updated = official_updated_at(nds)
        if updated:
            updated = datetime.strptime(updated, "%Y/%m/%d %H:%M:%S").isoformat()
        attachments.append({**attachment, "date": {"rocYear": ry, "month": month, "day": day},
                            "dateSource": date_source, "officialUpdatedAt": updated,
                            "sha256": hashlib.sha256(nds_html.encode("utf-8")).hexdigest(),
                            "rawRegionTexts": rows,
                            "dispositions": {region: regional_disposition(rows[region])
                                             if region in rows else "not-listed" for region in REGIONS}})
        for region, raw_text in rows.items():
            disposition = regional_disposition(raw_text)
            observation = {"pid": entry["pid"], "date": {"rocYear": ry, "month": month, "day": day},
                           "region": region, "disposition": disposition, "officialUpdatedAt": updated,
                           "attachmentOrder": index, "ndsUrl": attachment["url"], "rawText": raw_text}
            base = {**observation["date"], "region": region, "officialText": county_sentence(raw_text) or clean(raw_text),
                    "officialUrl": entry["url"], "ndsUrl": attachment["url"],
                    "pid": entry["pid"], "sourceKind": "archive"}
            observation["event"] = base if disposition == "full-day" else None
            observation["reviewItem"] = ({**base, "officialText": clean(raw_text),
                                          "reason": "manual-closure-type", "message": MANUAL_TYPE_MESSAGE}
                                         if disposition == "manual-closure-type" else None)
            key = (*parsed_date, region)
            observations.setdefault(key, []).append(observation)
    first_date = min(all_dates)
    winners = [select_observation(values) for values in observations.values()]
    return {**entry, "status": "done", "checkedAt": now_text(),
            "parserVersion": PARSER_VERSION, "date": dict(zip(("rocYear", "month", "day"), first_date)),
            "dates": [dict(zip(("rocYear", "month", "day"), date)) for date in sorted(all_dates)],
            "attachments": attachments, "observations": winners,
            "dispositions": {region: next((value["disposition"] for value in reversed(winners)
                                           if value["region"] == region), "not-listed") for region in REGIONS},
            "rawRegionTexts": {region: next((value["rawText"] for value in reversed(winners)
                                            if value["region"] == region), "") for region in REGIONS},
            "detailSha256": hashlib.sha256(detail_html.encode("utf-8")).hexdigest(),
            "events": [value["event"] for value in winners if value["event"]],
            "reviewItems": [value["reviewItem"] for value in winners if value["reviewItem"]]}

def error_record(entry, error, previous=None):
    return {**entry, "status": "error", "checkedAt": now_text(),
            "parserVersion": PARSER_VERSION,
            "attempts": int((previous or {}).get("attempts", 0)) + 1,
            "error": f"{type(error).__name__}: {str(error)[:400]}"}


def sorted_events(values):
    return sorted(values, key=lambda x: event_key(x), reverse=True)


def merge_snapshot(existing, records, checked_at, daily):
    """僅對成功重讀的公告/日期撤回舊資料；失敗與過期 daily 不可刪除既有事件."""
    good = [record for record in records.values() if record["status"] == "done"]
    daily_date, daily_events, daily_reviews, daily_status = daily
    fresh_daily = daily_status.get("ok") is True
    daily_regions = set(daily_status.get("regionsCovered", REGIONS))
    events = {event_key(event): event for event in existing.get("events", [])}
    reviews = {item_key(item): item for item in existing.get("reviewItems", [])}
    cache = dict(existing.get("archiveObservations") or {})
    # 初次升級時從既有事件/明確負向修訂建立保守觀察；不把它們算成本輪已 REVIEW。
    for value, review in ([(value, False) for value in events.values()]
                          + [(value, True) for value in reviews.values()]):
        pid = str(value.get("pid", ""))
        if not pid.isdigit() or pid in cache:
            continue
        cache[pid] = {"pid": pid, "observations": []}
    for value, review in ([(value, False) for value in events.values()]
                          + [(value, True) for value in reviews.values()]):
        pid = str(value.get("pid", ""))
        if pid not in cache or "checkedAt" in cache[pid]:
            continue
        key = event_key(value)
        if not any((*date_key(item["date"]), item["region"]) == key for item in cache[pid]["observations"]):
            cache[pid]["observations"].append({"pid": pid, "date": dict(zip(("rocYear", "month", "day"), key[:3])),
                "region": key[3], "disposition": "manual-closure-type" if review else "full-day",
                "officialUpdatedAt": "", "event": None if review else value,
                "reviewItem": value if review else None})
    for key, revision in (existing.get("archiveRevisions") or {}).items():
        pid = str(revision.get("pid", ""))
        if not pid.isdigit():
            continue
        year, month, day, region = key.split("|")
        legacy = cache.setdefault(pid, {"pid": pid, "observations": []})
        if "checkedAt" in legacy or any(
                (*date_key(value["date"]), value["region"]) == (int(year), int(month), int(day), region)
                for value in legacy["observations"]):
            continue
        legacy["observations"].append({"pid": pid,
            "date": {"rocYear": int(year), "month": int(month), "day": int(day)}, "region": region,
            "disposition": revision["disposition"], "officialUpdatedAt": revision.get("officialUpdatedAt", ""),
            "event": None, "reviewItem": None})
    for record in good:
        cache[record["pid"]] = {"pid": record["pid"], "checkedAt": record["checkedAt"],
                               "observations": record["observations"]}
    cached_pids = set(cache)
    events = {key: value for key, value in events.items() if value.get("pid") not in cached_pids}
    reviews = {key: value for key, value in reviews.items() if value.get("pid") not in cached_pids}
    grouped = {}
    for value in cache.values():
        for observation in value["observations"]:
            key = (*date_key(observation["date"]), observation["region"])
            grouped.setdefault(key, []).append(observation)
    winners = {key: select_observation(values) for key, values in grouped.items()}
    revisions = {}
    for key, observation in winners.items():
        events.pop(key, None)
        reviews = {item_id: value for item_id, value in reviews.items() if item_id[:4] != key}
        if observation.get("event"):
            events[key] = observation["event"]
        if observation.get("reviewItem"):
            reviews[item_key(observation["reviewItem"])] = observation["reviewItem"]
        revisions["|".join(map(str, key))] = {
            "pid": observation["pid"], "checkedAt": cache[observation["pid"]].get("checkedAt", ""),
            "officialUpdatedAt": observation.get("officialUpdatedAt", ""),
            "disposition": observation["disposition"]}
    if fresh_daily:
        events = {key: value for key, value in events.items()
                  if key[:3] != daily_date or key[3] not in daily_regions}
        events.update({event_key(event): event for event in daily_events.values()})
        reviews = {key: value for key, value in reviews.items()
                   if key[:3] != daily_date or key[3] not in daily_regions}
        reviews.update({item_key(item): item for item in daily_reviews})
    months = [(date["rocYear"], date["month"]) for record in good
              for date in record.get("dates", [record["date"]])]
    old_coverage = existing.get("coverage") or {}
    if old_coverage:
        months.append((int(old_coverage["minRocYear"]), int(old_coverage["minMonth"])))
    if not months:
        if not existing:
            raise ValueError("No usable official snapshot or dated archive records")
        minimum = (checked_at.year - 1911, checked_at.month)
    else:
        minimum = min(months)
    maximum = max(months + [(checked_at.year - 1911, checked_at.month),
                            (int(old_coverage.get("maxRocYear", minimum[0])),
                             int(old_coverage.get("maxMonth", minimum[1])))])
    result = {**existing, "schemaVersion": 3, "source": ROOT,
              "updatedAt": checked_at.isoformat(timespec="seconds"),
              "historyComplete": existing.get("historyComplete") is True,
              "coverage": {"minRocYear": minimum[0], "minMonth": minimum[1],
                           "maxRocYear": maximum[0], "maxMonth": maximum[1]},
              "events": sorted_events(events.values()),
              "reviewItems": sorted(reviews.values(), key=item_key, reverse=True),
              "dailyStatus": daily_status, "archiveRevisions": revisions, "archiveObservations": cache}
    result["lastEvent"] = ({key: result["events"][0][key] for key in ("rocYear", "month", "day")}
                           if result["events"] else None)
    result.setdefault("pagesScanned", 0)
    return result


def incremental(existing, pages, checked_at):
    # 當日頁優先，歷史下載遇到預算上限仍可保存當日結果。
    daily = load_daily_snapshot(checked_at)
    records, errors = {}, []
    scanned = 0
    for page in range(1, pages + 1):
        try:
            entries, _, total_pages = list_page(page)
            scanned += 1
        except BudgetReached:
            break
        except Exception as error:
            errors.append({"url": f"{ROOT}/informationlist?uid=374&page={page}",
                           "error": f"{type(error).__name__}: {str(error)[:400]}"})
            break
        for pid, entry in entries.items():
            if pid in records:
                continue
            try:
                records[pid] = parse_announcement(entry)
            except BudgetReached:
                break
            except Exception as error:
                records[pid] = error_record(entry, error)
                errors.append({"url": entry["url"], "error": records[pid]["error"]})
        if time.monotonic() >= DEADLINE or page >= total_pages:
            break
    result = merge_snapshot(existing, records, checked_at, daily)
    result["pagesScanned"] = max(scanned, int(existing.get("pagesScanned", 0))) if result["historyComplete"] else scanned
    if daily[3].get("ok") is not True:
        errors.append({"url": DAILY_URL, "error": daily[3].get("message", DAILY_ERROR_MESSAGE)})
    return result, records, errors, daily


def new_scan():
    return {"nextPage": 1, "expectedCount": None, "expectedPages": None,
            "items": {}, "complete": False, "lastError": ""}


def new_review():
    return {"schemaVersion": 1, "reviewId": datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8],
            "parserVersion": PARSER_VERSION, "status": "in-progress", "phase": "discover",
            "startedAt": now_text(), "updatedAt": now_text(), "completedAt": None,
            "announcements": {}, "records": {}, "scan": new_scan(), "pagesScanned": 0,
            "verificationPasses": 0, "incrementalErrors": []}


def review_counts(state):
    entries, records = state.get("announcements", {}), state.get("records", {})
    completed = sum(records.get(pid, {}).get("status") == "done" for pid in entries)
    failed = sum(records.get(pid, {}).get("status") == "error" for pid in entries)
    return {"total": len(entries), "completed": completed, "failed": failed,
            "pending": len(entries) - completed - failed,
            "manualItems": sum(len(records.get(pid, {}).get("reviewItems", [])) for pid in entries)}


def review_report(state, errors):
    if not state:
        return {"schemaVersion": 1, "status": "idle" if not errors else "blocked",
                "phase": "incremental", "reviewId": None, "updatedAt": now_text(),
                "counts": {}, "incrementalErrors": errors, "announcements": []}
    records = state["records"]
    return {"schemaVersion": 1, "reviewId": state["reviewId"], "status": state["status"],
            "phase": state["phase"], "parserVersion": state["parserVersion"],
            "startedAt": state["startedAt"], "updatedAt": now_text(),
            "completedAt": state.get("completedAt"), "counts": review_counts(state),
            "scan": {key: value for key, value in state.get("scan", {}).items() if key != "items"},
            "incrementalErrors": errors, "verificationPasses": state.get("verificationPasses", 0),
            "changes": state.get("changes"),
            "announcements": [{**entry, **records.get(pid, {"status": "pending"})}
                              for pid, entry in state["announcements"].items()]}


def git_checkpoint():
    if os.environ.get("DGPA_CHECKPOINT_GIT") != "1":
        return
    paths = [str(path.resolve().relative_to(PROJECT_ROOT).as_posix())
             for path in (OUT, STATE_PATH, REPORT_PATH) if path.exists()]
    branch = os.environ.get("DGPA_BRANCH", "")
    if not branch:
        raise CheckpointError("DGPA_BRANCH is required for durable Git checkpoints")
    commands = [
        ["git", "add", "--", *paths],
        ["git", "diff", "--cached", "--quiet", "--", *paths],
    ]
    try:
        for index, command in enumerate(commands):
            run = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, timeout=60)
            if index == 1 and run.returncode == 0:
                return
            if run.returncode != (1 if index == 1 else 0):
                raise CheckpointError("Could not stage durable checkpoint")
        for command in (["git", "commit", "--only", "-m", "chore: checkpoint DGPA review", "--", *paths],
                        ["git", "push", "origin", f"HEAD:refs/heads/{branch}"]):
            run = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, timeout=60)
            if run.returncode:
                raise CheckpointError("Checkpoint commit/push rejected; local recovery files preserved")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckpointError(f"Checkpoint Git operation failed: {type(error).__name__}") from error


def persist(snapshot, state, errors):
    if state:
        state["updatedAt"] = now_text()
        state["incrementalErrors"] = errors
    if snapshot:
        # 正式資料與候選/進度分離；審核中不將既有 true 降為 false。
        atomic_json(OUT, snapshot)
    if state:
        atomic_json(STATE_PATH, state)
    atomic_json(REPORT_PATH, review_report(state, errors))
    git_checkpoint()


def scan_review_page(state):
    scan = state["scan"]
    page = scan["nextPage"]
    entries, count, pages = list_page(page)
    if scan["expectedCount"] is not None and (count != scan["expectedCount"] or pages != scan["expectedPages"]):
        state["scan"] = new_scan()
        raise ValueError("Archive changed during enumeration; restart list verification next run")
    scan["expectedCount"], scan["expectedPages"] = count, pages
    scan["items"].update(entries)
    scan["nextPage"] += 1
    if page == pages:
        if len(scan["items"]) != count:
            state["scan"] = new_scan()
            raise ValueError("Archive item count mismatch; restart enumeration next run")
        scan["complete"] = True
    elif page > pages:
        raise ValueError("Unexpected archive page range")
    return scan["complete"]


def finish_scan(state):
    scan = state["scan"]
    new_entries = scan["items"]
    old_entries = state["announcements"]
    changed = {pid for pid, entry in new_entries.items()
               if pid not in old_entries or entry["title"] != old_entries[pid]["title"]}
    for pid in changed:
        state["records"].pop(pid, None)
    state["records"] = {pid: record for pid, record in state["records"].items() if pid in new_entries}
    state["announcements"] = new_entries
    state["pagesScanned"] = scan["expectedPages"]
    was_verification = state["phase"] == "verify"
    if was_verification:
        state["verificationPasses"] += 1
    counts = review_counts(state)
    state["phase"] = "publish" if was_verification and not changed and not counts["pending"] and not counts["failed"] else "review"
    if state["phase"] == "publish":
        state["verifiedAt"] = now_text()
    state["scan"] = new_scan()


def candidate_snapshot(snapshot, state, daily):
    records = state["records"]
    # 已重新讀取的歷史為權威來源，不能把已撤回的舊 archive 事件再併回來。
    base = {**snapshot, "events": [event for event in snapshot.get("events", [])
                                  if event.get("sourceKind") == "daily"],
            "reviewItems": [], "coverage": None, "archiveRevisions": {}, "archiveObservations": {}}
    candidate = merge_snapshot(base, records, datetime.now(timezone(timedelta(hours=8))), daily)
    candidate["historyComplete"] = True
    candidate["pagesScanned"] = state["pagesScanned"]
    candidate["historyReview"] = {"reviewId": state["reviewId"], "completedAt": now_text(),
                                 "parserVersion": PARSER_VERSION, "counts": review_counts(state)}
    before = {event_key(event): event for event in snapshot.get("events", [])}
    after = {event_key(event): event for event in candidate["events"]}
    state["changes"] = {
        "added": [list(key) for key in after.keys() - before.keys()],
        "removed": [list(key) for key in before.keys() - after.keys()],
        "changed": [list(key) for key in before.keys() & after.keys()
                    if before[key].get("officialText") != after[key].get("officialText")],
    }
    return candidate


def positive_env(name, default, maximum):
    value = int(os.environ.get(name, str(default)))
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def main():
    global DEADLINE
    mode = os.environ.get("DGPA_MODE", "incremental").strip().lower()
    if os.environ.get("DGPA_FULL_SCAN") == "1":
        mode = "full"
    if mode not in {"incremental", "full", "resume"}:
        raise ValueError("DGPA_MODE must be incremental, full or resume")
    pages = positive_env("DGPA_INCREMENTAL_PAGES", INCREMENTAL_PAGES, 100)
    batch_size = positive_env("DGPA_REVIEW_BATCH_SIZE", 50, 500)
    budget = positive_env("DGPA_REVIEW_BUDGET_SECONDS", 240, 900)
    checkpoint_every = positive_env("DGPA_CHECKPOINT_EVERY", 10, 50)
    DEADLINE = time.monotonic() + budget
    existing = load_existing()
    state = read_json(STATE_PATH) or None
    if state and state.get("schemaVersion") != 1:
        raise ValueError("Unsupported review state schema")
    if mode == "full" and (not state or state.get("status") == "completed"):
        state = new_review()
    if state and state["parserVersion"] != PARSER_VERSION and state["status"] != "completed":
        # 解析器變更時不能沿用舊解析結果冒充新版本全量複核。
        state = new_review()
    checked_at = datetime.now(timezone(timedelta(hours=8)))
    try:
        snapshot, live_records, errors, daily = incremental(existing, pages, checked_at)
    except BudgetReached:
        if not existing:
            raise
        persist(existing, state, [{"url": DAILY_URL, "error": "Time budget exhausted before daily update"}])
        return 1
    persist(snapshot, state, errors)
    if not state or state["status"] == "completed":
        return int(bool(errors))
    state["status"] = "in-progress"
    processed = 0
    run_errors = list(errors)
    try:
        while time.monotonic() < DEADLINE - 8:
            if state["phase"] in {"discover", "verify"}:
                try:
                    scan_complete = scan_review_page(state)
                    if scan_complete:
                        finish_scan(state)
                    if scan_complete or (state["scan"]["nextPage"] - 1) % min(5, checkpoint_every) == 0:
                        persist(snapshot, state, run_errors)
                except BudgetReached:
                    break
                except CheckpointError:
                    raise
                except Exception as error:
                    state["scan"]["lastError"] = f"{type(error).__name__}: {str(error)[:400]}"
                    run_errors.append({"url": f"{ROOT}/informationlist?uid=374",
                                       "error": state["scan"]["lastError"]})
                    break
            elif state["phase"] == "review":
                remaining = [pid for pid in state["announcements"]
                             if state["records"].get(pid, {}).get("status") != "done"]
                remaining.sort(key=lambda pid: (state["records"].get(pid, {}).get("status") == "error",
                                                 state["records"].get(pid, {}).get("attempts", 0)))
                if not remaining:
                    state["phase"], state["scan"] = "verify", new_scan()
                    persist(snapshot, state, run_errors)
                    continue
                for pid in remaining:
                    if processed >= batch_size or time.monotonic() >= DEADLINE - 8:
                        break
                    entry = state["announcements"][pid]
                    try:
                        previous = state["records"].get(pid, {})
                        record = parse_announcement(entry)
                        record["attempts"] = int(previous.get("attempts", 0)) + 1
                        state["records"][pid] = record
                    except BudgetReached:
                        break
                    except Exception as error:
                        state["records"][pid] = error_record(entry, error, state["records"].get(pid))
                    processed += 1
                    if processed % checkpoint_every == 0:
                        persist(snapshot, state, run_errors)
                counts = review_counts(state)
                if not counts["pending"] and not counts["failed"]:
                    state["phase"], state["scan"] = "verify", new_scan()
                    continue
                break  # 此輪每個項目最多嘗試一次，失敗下輪再試。
            elif state["phase"] == "publish":
                # 取得本輪最新來源結果，避免候選檔覆蓋近期增量修訂。
                verified_at = state.get("verifiedAt", "")
                observations = list(live_records.values()) + list(snapshot.get("archiveRevisions", {}).values())
                if not verified_at or any(record.get("pid") not in state["announcements"]
                                          and record.get("checkedAt", "") > verified_at
                                          for record in observations if record.get("pid")):
                    state["phase"], state["scan"] = "verify", new_scan()
                    persist(snapshot, state, run_errors)
                    continue
                for pid, record in live_records.items():
                    if record["status"] != "done" or pid not in state["announcements"]:
                        continue
                    if record["checkedAt"] > state["records"][pid]["checkedAt"]:
                        state["records"][pid] = record
                if run_errors:
                    break
                counts = review_counts(state)
                if counts["pending"] or counts["failed"]:
                    raise ValueError("Review is incomplete; refusing publication")
                # 非工作流程環境也檢查磁碟是否被其他程序改寫，拒絕拿舊候選直接覆蓋。
                if load_existing() != snapshot:
                    raise ValueError("Official snapshot changed concurrently; retry from latest data")
                snapshot = candidate_snapshot(snapshot, state, daily)
                state["status"], state["phase"], state["completedAt"] = "completed", "completed", now_text()
                persist(snapshot, state, run_errors)
                print(f"DGPA review {state['reviewId']} published: {len(snapshot['events'])} events")
                return 0
            else:
                raise ValueError("Unknown review phase")
    except BudgetReached:
        pass
    if review_counts(state)["failed"] or run_errors:
        state["status"] = "blocked"
    persist(snapshot, state, run_errors)
    print(f"DGPA review {state['reviewId']}: {state['phase']}, {review_counts(state)}")
    return int(state["status"] == "blocked")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # 可公開來源不含憑證；Git stderr 不輸出，避免遠端 URL/授權資訊外洩。
        print(f"ERROR {type(error).__name__}: {str(error)[:500]}")
        raise SystemExit(1)

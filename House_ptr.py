"""미 하원 PTR(Periodic Transaction Report)을 Clerk 공식 소스에서 직접 수집.

파이프라인:
  {year}FD.ZIP  ->  {year}FD.xml (연도별 전체 공시 인덱스)
    -> FilingType == 'P' 이고 FilingDate 가 기간 내인 건만
    -> public_disc/ptr-pdfs/{year}/{DocID}.pdf 다운로드
    -> pdfplumber 텍스트 추출 -> 정규식 파싱

스캔 제출본(텍스트 레이어 없음)은 OCR 하지 않고 별도로 표시만 한다.
필기 서식에 대한 OCR 오인식은 티커를 조용히 틀리게 만들어, 누락보다 해롭다.
"""
import datetime as dt
import io
import re
import sys
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import pdfplumber
import requests

BASE = "https://disclosures-clerk.house.gov/public_disc"
UA = "insider-feed/1.0 (personal research)"

WORKERS = 4
RATE = 3.0            # req/sec. 공식 명시 한도는 없으나 보수적으로.
SCANNED_MIN_CHARS = 400   # 이 미만이면 텍스트 레이어 없음으로 간주

_lock = threading.Lock()
_next = [0.0]
_local = threading.local()


def _throttle():
    with _lock:
        now = time.monotonic()
        if _next[0] > now:
            time.sleep(_next[0] - now)
            now = _next[0]
        _next[0] = now + 1.0 / RATE


def _get(url, timeout=60):
    _throttle()
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = UA
        _local.s = s
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return r


# ---------- 인덱스 ----------

def _child(el, *names):
    """XML 태그명이 서식 버전에 따라 다를 수 있어 방어적으로 조회."""
    for n in names:
        v = el.findtext(n)
        if v is not None and v.strip():
            return v.strip()
    return ""


def index_ptrs(year, since):
    """해당 연도 인덱스에서 since 이후 제출된 PTR 목록."""
    url = f"{BASE}/financial-pdfs/{year}FD.ZIP"
    try:
        blob = _get(url).content
    except requests.HTTPError as e:
        print(f"[warn] {year} 인덱스 실패: {e}", file=sys.stderr)
        return []

    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
        if not name:
            print(f"[warn] {year}FD.ZIP 안에 XML 없음: {z.namelist()}", file=sys.stderr)
            return []
        root = ET.fromstring(z.read(name))

    out = []
    for m in root.iter():
        if not len(m):
            continue
        ftype = _child(m, "FilingType")
        if ftype.upper() != "P":
            continue
        doc = _child(m, "DocID")
        filed = _parse_date(_child(m, "FilingDate"))
        if not doc or not filed or filed < since:
            continue
        first = _child(m, "First")
        last = _child(m, "Last")
        out.append({
            "doc_id": doc,
            "year": year,
            "who": f"{first} {last}".strip() or last or doc,
            "district": _child(m, "StateDst"),
            "filed": filed,
            "url": f"{BASE}/ptr-pdfs/{year}/{doc}.pdf",
        })
    return out


def _parse_date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            pass
    return None


# ---------- PDF ----------

TXN_RE = re.compile(
    r"\(([A-Za-z][A-Za-z0-9.\-]{0,6})\)\s*"        # (티커)
    r"\[([A-Za-z]{2})\]\s*"                          # [자산유형]
    r"(S\s*\(partial\)|S\s*\(full\)|P|S|E)\s+"       # 거래유형
    r"(\d{2}/\d{2}/\d{4})\s+"                        # 거래일
    r"(\d{2}/\d{2}/\d{4})\s+"                        # 통지일
    r"(\$[\d,]+\s*-\s*\$?[\d,]+|Over\s*\$[\d,]+)",   # 금액 구간
    re.I,
)
AMT_RE = re.compile(r"\$?([\d,]+)")


def parse_txns(text):
    """추출된 PTR 텍스트에서 거래 행을 뽑는다.

    티커가 괄호로 표기된 건만 잡힌다. 지방채/국채/펀드 등 티커 없는 자산은
    의도적으로 버린다 (주식 시그널만 본다).
    """
    rows = []
    for m in TXN_RE.finditer(text):
        tk, atype, ttype, traded, notified, amt = m.groups()
        nums = [float(x.replace(",", "")) for x in AMT_RE.findall(amt)]
        t = re.sub(r"\s+", " ", ttype).lower()
        rows.append({
            "ticker": tk.upper(),
            "asset_type": atype.upper(),
            "type": "purchase" if t.startswith("p") else ("exchange" if t.startswith("e") else "sale"),
            "traded": traded,
            "notified": notified,
            "amount": re.sub(r"\s+", " ", amt),
            "value": max(nums) if nums else 0.0,
        })
    return rows


def fetch_and_parse(f):
    """PTR 하나를 받아 파싱. 스캔본이면 scanned=True 로 표시하고 거래는 비운다."""
    try:
        blob = _get(f["url"]).content
    except requests.HTTPError as e:
        print(f"[warn] PDF 실패 {f['doc_id']}: {e}", file=sys.stderr)
        return f | {"scanned": False, "txns": []}

    try:
        with pdfplumber.open(io.BytesIO(blob)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        print(f"[warn] 파싱 실패 {f['doc_id']}: {e}", file=sys.stderr)
        return f | {"scanned": True, "txns": []}

    if len(text.strip()) < SCANNED_MIN_CHARS:
        return f | {"scanned": True, "txns": []}
    return f | {"scanned": False, "txns": parse_txns(text)}


# ---------- 진입점 ----------

def collect_house(since):
    """since 이후 제출된 하원 PTR 거래 목록과 스캔본 목록을 반환."""
    years = {since.year, dt.date.today().year}   # 연초 경계 대응
    filings = []
    for y in sorted(years):
        filings.extend(index_ptrs(y, since))
    print(f"[info] 하원 PTR {len(filings)}건 인덱싱", file=sys.stderr)

    rows, scanned = [], []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for f in ex.map(fetch_and_parse, filings):
            if f["scanned"]:
                scanned.append(f)
                continue
            for t in f["txns"]:
                rows.append({
                    "chamber": "House",
                    "who": f["who"],
                    "district": f["district"],
                    "filed": f["filed"],
                    "link": f["url"],
                    "key": f"{f['doc_id']}|{t['ticker']}|{t['type']}|{t['traded']}|{t['amount']}",
                    **t,
                })
    print(f"[info] 거래 {len(rows)}건 / 스캔본 {len(scanned)}건", file=sys.stderr)
    return rows, scanned

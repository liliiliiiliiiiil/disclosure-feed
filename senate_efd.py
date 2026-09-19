"""미 상원 PTR(Periodic Transaction Report)을 EFD 검색에서 직접 수집.

파이프라인 (실제 응답을 확인하고 맞춘 것이다):
  GET  /search/home/          -> csrfmiddlewaretoken
  POST /search/home/          -> prohibition_agreement=1 로 접속 동의. 이걸
                                 통과해야 세션에 검색 권한이 붙는다.
  POST /search/report/data/   -> {"result":"ok","recordsTotal":N,"data":[...]}
       행 5칸: [first, last, "Last, First (Senator)", "<a href=...>", "MM/DD/YYYY"]
  GET  /search/view/ptr/<uuid>/ -> 거래 테이블이 든 HTML

동의 절차가 고지하는 것은 윤리정부법상 사용 제한(신용평가·영업 목적 사용
금지 등)이며, 이 프로젝트의 용도(개인 연구·투명성)는 그 제한 안에 있다.
README 의 고지와 같은 내용이다.

하원과 달리 PDF 가 아니라 HTML 테이블이라 텍스트 추출이 필요 없다. 대신
'Ticker' 칸이 비어 있는 행이 흔해서 자산명에서 보조로 읽는다(아래 _ticker).

물량이 30일에 10여 건 수준이라 순차로 받는다. 하원처럼 스레드를 쓸 이유가 없다.
"""
import datetime as dt
import re
import sys
import threading
import time
from html.parser import HTMLParser

import requests

BASE = "https://efdsearch.senate.gov"
UA = "disclosure-feed/1.0 (personal research)"

RATE = 2.0            # req/sec. 공식 명시 한도는 없으나 보수적으로.
PTR_REPORT_TYPE = 11  # 실측: report_types=[11] 이 Periodic Transaction Report
PAGE = 100

HTTP_RETRIES = 3
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

_lock = threading.Lock()
_next = [0.0]


def _throttle():
    with _lock:
        now = time.monotonic()
        if _next[0] > now:
            time.sleep(_next[0] - now)
            now = _next[0]
        _next[0] = now + 1.0 / RATE


def _request(s, method, url, **kw):
    """일시적 오류(429/5xx/네트워크)는 지수 백오프로 재시도한다."""
    kw.setdefault("timeout", 60)
    for attempt in range(HTTP_RETRIES + 1):
        last = attempt == HTTP_RETRIES
        _throttle()
        try:
            r = s.request(method, url, **kw)
            if r.status_code not in RETRY_STATUS or last:
                r.raise_for_status()
                return r
        except (requests.ConnectionError, requests.Timeout):
            if last:
                raise
        time.sleep(2 ** attempt)


CSRF_RE = re.compile(
    r"name=['\"]csrfmiddlewaretoken['\"][^>]*value=['\"]([^'\"]+)")


def _open_session():
    """동의 절차를 통과한 세션을 만든다."""
    s = requests.Session()
    s.headers["User-Agent"] = UA

    r = _request(s, "GET", f"{BASE}/search/home/")
    m = CSRF_RE.search(r.text)
    if not m:
        raise RuntimeError("EFD 홈에서 csrfmiddlewaretoken 을 찾지 못함 — 양식 변경 의심")

    _request(s, "POST", f"{BASE}/search/home/",
             data={"csrfmiddlewaretoken": m.group(1), "prohibition_agreement": "1"},
             headers={"Referer": f"{BASE}/search/home/"})

    # 검색 페이지의 토큰이 동의 후 갱신되므로 여기서 다시 읽는다.
    r = _request(s, "GET", f"{BASE}/search/")
    m = CSRF_RE.search(r.text)
    if not m:
        raise RuntimeError("EFD 검색 페이지에서 csrfmiddlewaretoken 을 찾지 못함")
    return s, m.group(1)


# ---------- 검색 ----------

LINK_RE = re.compile(r'href=[\'"]([^\'"]+)')


def _parse_date(s):
    try:
        return dt.datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except (ValueError, TypeError, AttributeError):
        return None


def search_ptrs(since):
    """since 이후 제출된 상원 PTR 목록."""
    s, token = _open_session()
    out, start, total = [], 0, None
    while total is None or start < total:
        r = _request(
            s, "POST", f"{BASE}/search/report/data/",
            data={
                "start": str(start), "length": str(PAGE),
                "report_types": f"[{PTR_REPORT_TYPE}]", "filer_types": "[]",
                "submitted_start_date": f"{since:%m/%d/%Y} 00:00:00",
                "submitted_end_date": "", "candidate_state": "",
                "senator_state": "", "office_id": "",
                "first_name": "", "last_name": "",
                "csrfmiddlewaretoken": token,
            },
            headers={"Referer": f"{BASE}/search/",
                     "X-Requested-With": "XMLHttpRequest"})
        try:
            j = r.json()
        except ValueError:
            raise RuntimeError(f"EFD 검색이 JSON 을 주지 않음: {r.text[:200]!r}")
        if j.get("result") != "ok":
            raise RuntimeError(f"EFD 검색 실패: {j.get('result')!r}")

        rows = j.get("data") or []
        total = j.get("recordsTotal", 0)
        for row in rows:
            if len(row) < 5:
                continue
            m = LINK_RE.search(row[3])
            filed = _parse_date(row[4])
            if not m or not filed or filed < since:
                continue
            link = m.group(1)
            doc = link.rstrip("/").rsplit("/", 1)[-1]
            out.append({
                "doc_id": doc,
                "who": f"{row[0].strip()} {row[1].strip()}".strip() or doc,
                "filed": filed,
                # 지면 제출본은 /search/view/paper/<uuid>/ 로 간다.
                "paper": "/paper/" in link,
                "url": BASE + link if link.startswith("/") else link,
            })
        if not rows:
            break
        start += len(rows)
    return s, out


# ---------- 상세 페이지 ----------

class _Rows(HTMLParser):
    """거래 테이블의 <td> 행만 모은다. 헤더가 9칸이라 9칸 행만 받는다."""

    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._in = [], None, None, False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "td":
            self._cell, self._in = [], True

    def handle_data(self, data):
        if self._in and data.strip():
            self._cell.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "td" and self._row is not None:
            self._row.append(" ".join(self._cell))
            self._in = False
        elif tag == "tr":
            if self._row and len(self._row) == 9:
                self.rows.append(self._row)
            self._row = None


TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,5}")
PAREN_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,5})\)")
AMT_RE = re.compile(r"\$?([\d,]+)")


def _ticker(cell, asset_name):
    """거래 종목 심볼.

    Ticker 칸이 비어 있는 행이 흔하다('--'). 그 경우 자산명의 괄호 심볼을
    보조로 쓰되, 서로 다른 심볼이 둘 이상이면 버린다. 교환 거래는 내준 종목과
    받은 종목이 한 줄에 같이 적혀 어느 쪽이 대상인지 판정할 수 없다.
    """
    t = (cell or "").strip().upper()
    if t and t not in ("--", "N/A", "NONE"):
        return t if TICKER_RE.fullmatch(t) else ""
    found = PAREN_TICKER_RE.findall(asset_name or "")
    return found[0] if len(set(found)) == 1 else ""


def _txn_type(cell):
    t = (cell or "").strip().lower()
    if t.startswith("purchase"):
        return "purchase"
    if t.startswith("sale"):
        return "sale"
    if t.startswith("exchange"):
        return "exchange"
    return ""


def parse_txns(html):
    """상세 페이지 HTML 에서 거래 행을 뽑는다.

    티커를 특정할 수 없는 자산(지방채·국채·비상장 펀드 등)은 하원과 같은
    이유로 의도적으로 버린다.
    """
    p = _Rows()
    p.feed(html)
    out = []
    for _, traded, owner, tk, asset, atype, ttype, amount, _comment in p.rows:
        ticker = _ticker(tk, asset)
        kind = _txn_type(ttype)
        if not ticker or not kind:
            continue
        amount = re.sub(r"\s+", " ", amount).strip()
        nums = [float(x.replace(",", "")) for x in AMT_RE.findall(amount)]
        if not nums:
            continue
        out.append({
            "ticker": ticker,
            "asset_type": (atype or "").strip(),
            "owner": (owner or "").strip(),
            "type": kind,
            "traded": traded.strip(),
            "notified": "",          # 상원 서식에는 통지일 칸이 없다
            "amount": amount,
            "value": max(nums),
            "value_min": min(nums),
        })
    return out


# ---------- 진입점 ----------

def collect_senate(since, skip_docs=()):
    """since 이후 제출된 상원 PTR 을 수집.

    반환: (거래 행, 지면 제출본, 이번에 받아 파싱한 DocID 집합)
    house_ptr.collect_house 와 같은 형태라 호출 측이 두 원을 똑같이 다룬다.
    """
    s, filings = search_ptrs(since)

    total = len(filings)
    filings = [f for f in filings if f["doc_id"] not in skip_docs]
    print(f"[info] 상원 PTR {total}건 인덱싱 (신규 {len(filings)}건, "
          f"처리완료 {total - len(filings)}건 건너뜀)", file=sys.stderr)

    rows, scanned, fetched = [], [], set()
    for f in filings:
        base = {"chamber": "Senate", "doc_id": f["doc_id"], "who": f["who"],
                "district": "Senate", "filed": f["filed"], "link": f["url"]}
        if f["paper"]:
            fetched.add(f["doc_id"])
            scanned.append({**base, **f})
            continue
        try:
            html = _request(s, "GET", f["url"],
                            headers={"Referer": f"{BASE}/search/"}).text
        except requests.RequestException as e:
            print(f"[warn] 상세 실패 {f['doc_id']}: {e}", file=sys.stderr)
            continue     # fetched 에 넣지 않아 다음 실행에서 재시도
        fetched.add(f["doc_id"])
        for t in parse_txns(html):
            rows.append({
                **base,
                "key": f"{f['doc_id']}|{t['ticker']}|{t['type']}|{t['traded']}|{t['amount']}",
                **t,
            })
    print(f"[info] 상원 거래 {len(rows)}건 / 지면 {len(scanned)}건 / "
          f"수집실패 {len(filings) - len(fetched)}건", file=sys.stderr)
    return rows, scanned, fetched

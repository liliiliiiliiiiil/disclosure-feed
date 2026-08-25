"""SEC Form 4 내부자 거래 + 미 의회 PTR 피드.

두 소스는 신고지연(2영업일 vs 45일)과 금액 정밀도(실액 vs 구간)가 달라
서로 다른 필터를 적용하고 별도 메시지로 전송한다.

하원 PTR 수집은 house_ptr 모듈 참조 (Clerk 공식 소스 직접 파싱).
상원(efdsearch)은 접속 동의 절차가 필요해 아직 미포함.

환경변수:
  SEC_UA           SEC 필수 User-Agent. 예: "Yunchan Kim yunchan@example.com"
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID
  STATE_PATH       (선택) 의회 공시 중복 발송 방지용. 기본 .state/seen.json
"""
import json
import os
import re
import sys
import threading
import time
import datetime as dt
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests

import house_ptr

SEC_UA = os.environ["SEC_UA"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
STATE_PATH = os.environ.get("STATE_PATH", ".state/seen.json")

# --- 내부자(Form 4) 필터 ---
MIN_BUY_VALUE = 100_000       # 생색내기 매수 컷
BIG_BUY_VALUE = 1_000_000     # 직급 무관 통과
MIN_SELL_VALUE = 5_000_000    # 매도는 신호가 약해 문턱을 높인다
CLUSTER_MIN = 3               # 동일 종목 서로 다른 신고자 수
TOP_N = 12

# --- 의회 PTR 필터 ---
CONGRESS_LOOKBACK_DAYS = 7
CONGRESS_MIN_AMOUNT = 50_000  # 구간 상단 기준

SEC_WORKERS = 6
SEC_RATE = 8.0                # req/sec 상한 (SEC 공식 한도 10)


# ---------- HTTP ----------

_rate_lock = threading.Lock()
_next_slot = [0.0]
_local = threading.local()


def _throttle():
    with _rate_lock:
        now = time.monotonic()
        wait = _next_slot[0] - now
        if wait > 0:
            time.sleep(wait)
            now = _next_slot[0]
        _next_slot[0] = now + 1.0 / SEC_RATE


def _session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = SEC_UA
        _local.s = s
    return s


def _get(url):
    _throttle()
    r = _session().get(url, timeout=30)
    r.raise_for_status()
    return r


# ---------- SEC Form 4 ----------

SENIOR_PAT = re.compile(
    r"chief exec|\bceo\b|chief financ|\bcfo\b|chief oper|\bcoo\b|"
    r"\bpresident\b|chairman|chair of the board",
    re.I,
)


def _prev_business_day(d):
    """d 직전의 평일. 주말 수동 실행 시 빈 인덱스를 조회하는 것을 막는다.

    미 공휴일은 달력을 들고 있지 않아 걸러내지 못한다. 공휴일 다음 날
    실행하면 해당일 인덱스가 없어 0건이 나오는데, 이는 오류가 아니다.
    """
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:      # 5=토, 6=일
        d -= dt.timedelta(days=1)
    return d


def form4_filings(date):
    """해당 날짜 daily-index에서 Form 4 제출 경로 목록."""
    q = (date.month - 1) // 3 + 1
    url = (f"https://www.sec.gov/Archives/edgar/daily-index/"
           f"{date.year}/QTR{q}/master.{date:%Y%m%d}.idx")
    try:
        text = _get(url).text
    except requests.HTTPError:
        return []  # 주말/공휴일

    # 공동신고는 신고자 CIK 마다 한 줄씩 나열되고, Filename 의 디렉터리가
    # 그 CIK 라 경로 문자열이 서로 다르다. 같은 문서인지는 경로 끝의
    # 접수번호(제출당 전역 고유)로만 판별할 수 있다.
    seen, paths = set(), []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5 or parts[2].strip() != "4":
            continue
        p = parts[4].strip()
        acc = p.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if acc in seen:
            continue
        seen.add(acc)
        paths.append(p)
    return paths


PLAN_FOOTNOTE_RE = re.compile(r"10b5\s*-?\s*1\b", re.I)


NON_COMMON_RE = re.compile(
    r"preferred|warrant|\boption\b|\bright[s]?\b|debenture|\bbond\b|"
    r"convertible note|restricted stock unit|\brsu\b|performance (share|stock|unit)|"
    r"partnership interest|membership interest|\bdeferred\b|phantom",
    re.I,
)


def _is_plan_trade(doc):
    """10b5-1 사전약정 매매 여부.

    실측 기준 태그는 <aff10b5One>1</aff10b5One> 이며 문서 단위 체크박스다.
    숫자 1 이 영단어 One 으로 표기되어 있어 '10b51' 로는 매칭되지 않는다.

    체크박스는 2022년 12월 규칙 개정으로 신설되어 그 이전 신고서에는 없다.
    구형 건은 각주 본문의 10b5-1 언급으로 보완한다. 한 신고서에 계획매매와
    재량매매가 섞인 경우 전부 계획매매로 간주하는데, 애매한 건을 흘려보내는
    것보다 버리는 쪽이 신호 품질에 유리하다.
    """
    for el in doc.iter():
        if "10b5" in el.tag.lower().replace("-", "").replace("_", ""):
            v = (el.findtext("value") or el.text or "").strip().lower()
            if v in ("1", "true"):
                return True
    for fn in doc.iter("footnote"):
        if fn.text and PLAN_FOOTNOTE_RE.search(fn.text):
            return True
    return False


def _rank(title, is_director, is_ten_pct):
    if title and SENIOR_PAT.search(title):
        return "SENIOR"
    if title:
        return "OFFICER"
    if is_ten_pct:
        return "10%"
    if is_director:
        return "DIRECTOR"
    return ""


def parse_form4(path):
    """제출 파일 하나에서 P/S 거래를 추출."""
    try:
        raw = _get(f"https://www.sec.gov/Archives/{path}").text
    except requests.HTTPError:
        return []

    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", raw, re.S)
    if not m:
        return []
    try:
        doc = ET.fromstring(m.group(0))
    except ET.ParseError:
        return []

    ticker = (doc.findtext("issuer/issuerTradingSymbol") or "").strip().upper()
    issuer = (doc.findtext("issuer/issuerName") or "").strip()
    # 집합투자기구 등 상장 종목이 아닌 발행인은 심볼 자리에 N/A/NONE 을 넣는다.
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,5}", ticker) or ticker in ("N/A", "NONE"):
        return []

    # 공동신고 대응: reportingOwner가 복수일 수 있다.
    owners, titles = [], []
    is_director = is_ten_pct = False
    for ro in doc.findall("reportingOwner"):
        name = (ro.findtext("reportingOwnerId/rptOwnerName") or "").strip()
        if name:
            owners.append(name)
        rel = ro.find("reportingOwnerRelationship")
        if rel is None:
            continue
        t = (rel.findtext("officerTitle") or "").strip()
        if t:
            titles.append(t)
        if rel.findtext("isDirector") in ("1", "true"):
            is_director = True
        if rel.findtext("isTenPercentOwner") in ("1", "true"):
            is_ten_pct = True
    if not owners:
        return []

    title = max(titles, key=len) if titles else ""
    rank = _rank(title, is_director, is_ten_pct)
    is_plan = _is_plan_trade(doc)   # 문서 단위 체크박스이므로 한 번만 본다

    out = []
    for t in doc.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = t.findtext("transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        shares = t.findtext("transactionAmounts/transactionShares/value")
        price = t.findtext("transactionAmounts/transactionPricePerShare/value")
        if not shares or not price:
            continue
        # nonDerivativeTable 에는 보통주 외에 우선주·워런트·유닛·LP지분도 들어간다.
        # 이들은 티커가 가리키는 종목과 다른 증권이라 주당 가격이 시장가와
        # 무관하다. 사모 우선주 인수가 자사주 매수로 잡히는 것을 막는다.
        if NON_COMMON_RE.search(t.findtext("securityTitle/value") or ""):
            continue
        try:
            sh, px = float(shares), float(price)
        except ValueError:
            continue
        value = sh * px
        if value <= 0:
            continue
        out.append({
            "ticker": ticker,
            "src": path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
            "owners": tuple(owners),
            "title": title or (rank.title() if rank in ("DIRECTOR",) else ""),
            "rank": rank,
            "code": code,
            "shares": sh,
            "value": value,
            "plan": is_plan,
            "date": t.findtext("transactionDate/value") or "",
        })
    return out


def collect_form4(date):
    paths = form4_filings(date)
    print(f"[info] Form 4 제출 {len(paths)}건 수집 시작", file=sys.stderr)
    rows = []
    with ThreadPoolExecutor(max_workers=SEC_WORKERS) as ex:
        for chunk in ex.map(parse_form4, paths):
            rows.extend(chunk)
    return rows


def aggregate(rows):
    """같은 신고서 내 동일 종목·동일 코드의 분할 체결을 한 건으로 합친다.

    하루치를 여러 가격대로 나눠 체결하면 tranche 마다 행이 생기는데,
    이를 개별 거래로 두면 상위 목록을 한 사람이 잠식하고 각 조각이
    금액 하한에 걸려 전량 탈락하는 일이 생긴다. 단가는 가중평균을 쓴다.
    """
    merged = {}
    for r in rows:
        k = (r["src"], r["ticker"], r["code"])
        m = merged.get(k)
        if m is None:
            merged[k] = dict(r)
            continue
        m["shares"] += r["shares"]
        m["value"] += r["value"]
        m["plan"] = m["plan"] or r["plan"]
        if r["date"] < m["date"]:
            m["date"] = r["date"]
    for m in merged.values():
        m["price"] = m["value"] / m["shares"] if m["shares"] else 0.0
    return list(merged.values())


def annotate_cluster(rows):
    """동일 종목을 매수한 '독립적인 신고 건수'를 각 행에 붙인다.

    신고자 이름 수가 아니라 제출 파일 수를 센다. 계열 펀드 여러 곳이
    한 장에 공동신고한 것은 하나의 판단이지 여러 사람의 합의가 아니므로,
    이름으로 세면 가짜 클러스터가 만들어진다.
    """
    by_ticker = {}
    for r in rows:
        if r["code"] == "P":
            by_ticker.setdefault(r["ticker"], set()).add(r["src"])
    for r in rows:
        r["cluster"] = len(by_ticker.get(r["ticker"], ())) if r["code"] == "P" else 0


def filter_buys(rows):
    """실전 필터: P + 사전약정 제외 + 금액 하한, 그 뒤 셋 중 하나 충족.

    - 임원 상위직(CEO/CFO/COO/President/Chairman)
    - 클러스터 매수 (서로 다른 신고자 CLUSTER_MIN명 이상)
    - 단건 대형 매수
    """
    out = [
        r for r in rows
        if r["code"] == "P"
        and not r["plan"]
        and r["value"] >= MIN_BUY_VALUE
        and (r["rank"] == "SENIOR"
             or r["cluster"] >= CLUSTER_MIN
             or r["value"] >= BIG_BUY_VALUE)
    ]
    out.sort(key=lambda r: (r["cluster"] >= CLUSTER_MIN, r["value"]), reverse=True)
    return out[:TOP_N]


def filter_sells(rows):
    out = [
        r for r in rows
        if r["code"] == "S" and not r["plan"] and r["value"] >= MIN_SELL_VALUE
    ]
    out.sort(key=lambda r: -r["value"])
    return out[:TOP_N // 2]


# ---------- 의회 PTR ----------

def filter_congress(rows):
    return [r for r in rows if r["value"] >= CONGRESS_MIN_AMOUNT]


# ---------- 중복 발송 방지 ----------

def load_seen():
    try:
        with open(STATE_PATH) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_seen(seen):
    d = os.path.dirname(STATE_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(sorted(seen)[-50_000:], f)  # 무한 증가 방지


# ---------- 출력 ----------

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tv(ticker):
    """트레이딩뷰 심볼 페이지. 거래소 접두어 없이도 해석된다."""
    return f'<a href="https://www.tradingview.com/symbols/{esc(ticker)}/">{esc(ticker)}</a>'


def price(v):
    return f"${v:,.2f}" if v < 1000 else f"${v:,.0f}"


def money(v):
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    return f"${v/1e3:.0f}K"


def who_of(r):
    n = esc(r["owners"][0])
    if len(r["owners"]) > 1:
        n += f" 외 {len(r['owners']) - 1}"
    return n


def clamp(lines, limit=3900):
    out, n = [], 0
    for ln in lines:
        if n + len(ln) + 1 > limit:
            out.append("…")
            break
        out.append(ln)
        n += len(ln) + 1
    return "\n".join(out)


def build_insider_message(buys, sells, date, total):
    lines = [
        f"<b>🏢 내부자 거래 · Form 4</b> — {date:%Y-%m-%d}",
        f"<i>신고지연 2영업일 · 원시 {total}건 중 필터 통과분</i>",
        "",
        f"<b>매수 (P)</b>  <i>10b5-1 제외 · ≥{money(MIN_BUY_VALUE)}</i>",
    ]
    if not buys:
        lines.append("<i>없음</i>")
    for r in buys:
        tag = f" 🔥x{r['cluster']}" if r["cluster"] >= CLUSTER_MIN else ""
        t = f" · {esc(r['title'])}" if r["title"] else ""
        lines.append(f"{tv(r['ticker'])} {money(r['value'])} @ {price(r['price'])}{tag} — {who_of(r)}{t}")

    lines += ["", f"<b>매도 (S)</b>  <i>10b5-1 제외 · ≥{money(MIN_SELL_VALUE)}</i>"]
    if not sells:
        lines.append("<i>없음</i>")
    for r in sells:
        t = f" · {esc(r['title'])}" if r["title"] else ""
        lines.append(f"{tv(r['ticker'])} {money(r['value'])} @ {price(r['price'])} — {who_of(r)}{t}")

    return clamp(lines)


def build_congress_message(rows, scanned, since):
    buys = sorted([r for r in rows if r["type"] == "purchase"], key=lambda r: -r["value"])
    sells = sorted([r for r in rows if r["type"] == "sale"], key=lambda r: -r["value"])

    lines = [
        f"<b>🏛 하원 PTR</b> — {since:%m/%d} 이후 신규 제출",
        "<i>신고지연 최대 45일 · 금액은 구간 공시(상단 기준 정렬)</i>",
        "",
    ]
    for label, group in (("매수", buys[:TOP_N]), ("매도", sells[:TOP_N // 2])):
        lines.append(f"<b>{label}</b>")
        if not group:
            lines.append("<i>없음</i>")
        for r in group:
            lines.append(
                f'{tv(r["ticker"])} {esc(r["amount"])} — {esc(r["who"])} '
                f'<i>({esc(r["district"])}, 거래 {esc(r["traded"])})</i> '
                f'<a href="{r["link"]}">PTR</a>'
            )
        lines.append("")

    if scanned:
        lines.append(f"<b>⚠️ 스캔 제출본 {len(scanned)}건</b> <i>(자동 파싱 불가)</i>")
        for f in scanned[:5]:
            lines.append(f'· <a href="{f["url"]}">{esc(f["who"])}</a> {f["filed"]:%m/%d}')
    return clamp(lines)


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not r.ok:
        print(f"[error] telegram {r.status_code}: {r.text}", file=sys.stderr)
    r.raise_for_status()


def run_insider():
    target = _prev_business_day(dt.date.today())
    rows = aggregate(collect_form4(target))
    annotate_cluster(rows)
    buys, sells = filter_buys(rows), filter_sells(rows)
    print(f"form4 target={target} raw={len(rows)} buys={len(buys)} sells={len(sells)}")
    send(build_insider_message(buys, sells, target, len(rows)))


def run_congress():
    since = dt.date.today() - dt.timedelta(days=CONGRESS_LOOKBACK_DAYS)
    seen = load_seen()
    rows, scanned = house_ptr.collect_house(since)
    fresh = [r for r in filter_congress(rows) if r["key"] not in seen]
    scanned = [f for f in scanned if f["doc_id"] not in seen]
    print(f"congress new={len(fresh)} scanned={len(scanned)}")
    if fresh or scanned:
        send(build_congress_message(fresh, scanned, since))
        seen.update(r["key"] for r in fresh)
        seen.update(f["doc_id"] for f in scanned)
        save_seen(seen)


def main():
    """두 피드는 서로 독립이다. 한쪽이 실패해도 다른 쪽은 시도한다."""
    failed = []
    for name, fn in (("내부자", run_insider), ("하원", run_congress)):
        try:
            fn()
        except Exception as e:
            failed.append(name)
            print(f"[error] {name} 피드 실패: {e!r}", file=sys.stderr)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

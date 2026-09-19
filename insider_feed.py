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

# import 시점에 죽으면 테스트조차 불러올 수 없어 여기서는 읽기만 하고,
# 실제 유효성은 main() 에서 한 번에 확인한다.
SEC_UA = os.environ.get("SEC_UA", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_PATH = os.environ.get("STATE_PATH", ".state/seen.json")

# --- 내부자(Form 4) 필터 ---
MIN_BUY_VALUE = 100_000       # 생색내기 매수 컷
BIG_BUY_VALUE = 1_000_000     # 직급 무관 통과
MIN_SELL_VALUE = 5_000_000    # 매도는 신호가 약해 문턱을 높인다
CLUSTER_MIN = 3               # 동일 종목 서로 다른 신고자 수
TOP_N = 12

# --- 의회 PTR 필터 ---
# 구간 공시의 '하단' 기준. 하단을 쓰는 이유는 "최소 이만큼은 거래했다"가
# 확실한 수치이기 때문이다. 상단 기준 + 50,000 이던 이전 설정은 실질적으로
# $15,001-$50,000 구간까지 통과시키고 있었고, 여기서는 그 실제 동작을
# 그대로 두되 상수가 사실을 말하도록 맞췄다.
CONGRESS_LOOKBACK_DAYS = 7
CONGRESS_MIN_AMOUNT = 15_000

SEC_WORKERS = 6
SEC_RATE = 8.0                # req/sec 상한 (SEC 공식 한도 10)

HTTP_RETRIES = 3
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# --- 이상 감지 ---
# 소스 포맷이 바뀌면 파싱 결과가 0건이 되는데, 그대로 두면 "조용한 날"과
# "고장 난 날"이 구분되지 않는다. 표본이 충분한데 0건이면 고장으로 본다.
FORM4_ALERT_MIN_FILINGS = 100
PTR_ALERT_MIN_DOCS = 20


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
    """일시적 오류(429/5xx/네트워크)는 지수 백오프로 재시도한다.

    재시도가 없으면 SEC 가 순간 503 을 던질 때 해당 제출이 조용히 빠지고
    다이제스트는 멀쩡한 얼굴로 발송된다. 끝내 실패한 건은 호출 측에서
    개수를 세어 메시지에 표시한다.
    """
    for attempt in range(HTTP_RETRIES + 1):
        last = attempt == HTTP_RETRIES
        _throttle()
        try:
            r = _session().get(url, timeout=30)
            if r.status_code not in RETRY_STATUS or last:
                r.raise_for_status()
                return r
        except (requests.ConnectionError, requests.Timeout):
            if last:
                raise
        time.sleep(2 ** attempt)


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
    """제출 파일 하나에서 P/S 거래를 추출.

    반환: 거래 목록. 수집 자체에 실패하면 None (누락 집계용으로 빈 목록과 구분).
    """
    try:
        raw = _get(f"https://www.sec.gov/Archives/{path}").text
    except requests.RequestException as e:
        print(f"[warn] 제출 수집 실패 {path}: {e}", file=sys.stderr)
        return None

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

    # 공동신고 대응: reportingOwner 가 복수일 수 있다.
    owners = []                  # (이름, 직함)
    is_director = is_ten_pct = False
    for ro in doc.findall("reportingOwner"):
        name = (ro.findtext("reportingOwnerId/rptOwnerName") or "").strip()
        if not name:
            continue
        rel = ro.find("reportingOwnerRelationship")
        t = ""
        if rel is not None:
            t = (rel.findtext("officerTitle") or "").strip()
            if rel.findtext("isDirector") in ("1", "true"):
                is_director = True
            if rel.findtext("isTenPercentOwner") in ("1", "true"):
                is_ten_pct = True
        owners.append((name, t))
    if not owners:
        return []

    # 직함은 그 직함을 가진 사람에게 붙어야 한다. 공동신고에서 가장 긴 직함만
    # 뽑아 첫 신고자 옆에 찍으면 남의 직책이 오귀속되고, 그 직함으로 rank 까지
    # 올라가 필터를 통과한다. 표시 대상을 직함 주인으로 맞춘다.
    primary = next((o for o in owners if o[1] and SENIOR_PAT.search(o[1])), None)
    if primary is None:
        primary = next((o for o in owners if o[1]), owners[0])
    names = [primary[0]] + [n for n, _ in owners if n != primary[0]]
    title = primary[1]

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
            "owners": tuple(names),
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
    """반환: (거래 행, 수집 실패 제출 수, 전체 제출 수)."""
    paths = form4_filings(date)
    print(f"[info] Form 4 제출 {len(paths)}건 수집 시작", file=sys.stderr)
    rows, failed = [], 0
    with ThreadPoolExecutor(max_workers=SEC_WORKERS) as ex:
        for chunk in ex.map(parse_form4, paths):
            if chunk is None:
                failed += 1
            else:
                rows.extend(chunk)
    return rows, failed, len(paths)


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


def _buy_eligible(r):
    """클러스터 집계와 최종 필터가 공유하는 '유효 매수' 판정."""
    return r["code"] == "P" and not r["plan"] and r["value"] >= MIN_BUY_VALUE


def annotate_cluster(rows):
    """동일 종목을 매수한 '독립적인 신고 건수'를 각 행에 붙인다.

    신고자 이름 수가 아니라 제출 파일 수를 센다. 계열 펀드 여러 곳이
    한 장에 공동신고한 것은 하나의 판단이지 여러 사람의 합의가 아니므로,
    이름으로 세면 가짜 클러스터가 만들어진다.

    금액 하한과 10b5-1 제외를 통과한 매수만 센다. 필터 이전 원시 P 를 세면
    $500 짜리 매수나 계획매매가 정원을 채워, 자격 없는 건을 클러스터 조건으로
    통과시키고 🔥 까지 붙인다.
    """
    by_ticker = {}
    for r in rows:
        if _buy_eligible(r):
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
        if _buy_eligible(r)
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
    """구간 하단 기준. 상단(value)은 정렬·표시에만 쓴다."""
    return [r for r in rows if r["value_min"] >= CONGRESS_MIN_AMOUNT]


# ---------- 중복 발송 방지 ----------

def load_state():
    """{"keys": 발송 완료 거래, "docs": 처리 완료 PTR 문서}.

    구버전은 평면 키 목록이었으므로 그 형태도 읽는다.
    """
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set(), set()
    if isinstance(data, list):
        return set(data), set()
    return set(data.get("keys", ())), set(data.get("docs", ()))


def save_state(keys, docs):
    d = os.path.dirname(STATE_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(STATE_PATH, "w") as f:                      # 무한 증가 방지
        json.dump({"keys": sorted(keys)[-50_000:],
                   "docs": sorted(docs)[-50_000:]}, f)


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
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:,.0f}"


def who_of(r):
    n = esc(r["owners"][0])
    if len(r["owners"]) > 1:
        n += f" 외 {len(r['owners']) - 1}"
    return n


TAG_RE = re.compile(r"<[^>]+>")
# Bot API sendMessage 는 "1-4096 characters after entities parsing" 이므로
# 상한은 마크업을 뺀 가시 길이에 걸린다. <a href="...tradingview..."> 한 줄이
# 태그만으로 70자 넘게 먹는데, 원문 길이로 세면 실제 여유의 절반도 못 쓴다.
MSG_VISIBLE_LIMIT = 3800
MAX_MESSAGES = 4
CONT = "<i>(이어서)</i>"


def paginate(items):
    """(줄, 키) 목록을 여러 텔레그램 메시지로 나눈다.

    반환: (메시지 목록, 실제로 담긴 키 집합)

    한 번에 잘라내지 않고 나누는 이유는, 잘린 줄까지 발송 완료로 기록해
    영구 누락을 만들었기 때문이다. MAX_MESSAGES 를 넘긴 분량은 키를 돌려주지
    않으므로 상태에 기록되지 않고 다음 실행에서 다시 잡힌다.
    """
    msgs, sent = [], set()
    cur, cur_keys, vis = [], set(), 0

    def flush():
        nonlocal cur, cur_keys, vis
        msgs.append("\n".join(cur))
        sent.update(cur_keys)
        cur, cur_keys, vis = [], set(), 0

    for line, key in items:
        lv = len(TAG_RE.sub("", line)) + 1
        if cur and vis + lv > MSG_VISIBLE_LIMIT:
            flush()
            if len(msgs) >= MAX_MESSAGES:
                msgs[-1] += "\n…"
                return msgs, sent
            cur, vis = [CONT], len(TAG_RE.sub("", CONT)) + 1
        cur.append(line)
        vis += lv
        if key:
            cur_keys.add(key)
    if cur:
        flush()
    return msgs, sent


def build_insider_message(buys, sells, date, total, filings, failed, broken):
    """(줄, 키) 목록. 내부자 피드는 상태를 쓰지 않으므로 키는 전부 None."""
    lines = [
        f"<b>🏢 내부자 거래 · Form 4</b> — {date:%Y-%m-%d}",
        f"<i>신고지연 2영업일 · 제출 {filings}건 → 매매행 {total}건 중 필터 통과분</i>",
    ]
    if broken:
        lines.append(f"<i>⚠️ 제출 {filings}건에서 거래 0건 추출 — 파서 점검 필요</i>")
    elif failed:
        lines.append(f"<i>⚠️ 수집 실패 {failed}건은 이번 집계에서 빠짐</i>")
    lines += ["", f"<b>매수 (P)</b>  <i>10b5-1 제외 · ≥{money(MIN_BUY_VALUE)}</i>"]
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

    return [(ln, None) for ln in lines]


def build_congress_message(rows, scanned, since):
    """(줄, 키) 목록. 각 거래 줄에는 그 거래의 상태 키를 붙인다.

    TOP_N 으로 잘라내지 않는다. 잘린 분까지 발송 완료로 기록되어 영영
    사라지던 문제 때문이며, 분량은 paginate 가 메시지 분할로 처리한다.
    """
    def group(kind):
        return sorted([r for r in rows if r["type"] == kind], key=lambda r: -r["value"])

    items = [
        (f"<b>🏛 하원 PTR</b> — {since:%m/%d} 이후 신규 제출", None),
        ("<i>신고지연 최대 45일 · 금액은 구간 공시(상단 기준 정렬)</i>", None),
        ("", None),
    ]
    # 교환(E)은 드물어 항상 비어 있는 칸을 만들 필요가 없다. 다만 예전처럼
    # 조용히 버리면 상태에는 기록되고 화면에는 없는 건이 생긴다.
    for label, kind, always in (("매수", "purchase", True),
                                ("매도", "sale", True),
                                ("교환 (E)", "exchange", False)):
        g = group(kind)
        if not g and not always:
            continue
        items.append((f"<b>{label}</b>", None))
        if not g:
            items.append(("<i>없음</i>", None))
        for r in g:
            items.append((
                f'{tv(r["ticker"])} {esc(r["amount"])} — {esc(r["who"])} '
                f'<i>({esc(r["district"])}, 거래 {esc(r["traded"])})</i> '
                f'<a href="{esc(r["link"])}">PTR</a>',
                r["key"],
            ))
        items.append(("", None))

    if scanned:
        items.append((f"<b>⚠️ 스캔 제출본 {len(scanned)}건</b> <i>(자동 파싱 불가)</i>", None))
        for f in scanned:
            items.append((
                f'· <a href="{esc(f["url"])}">{esc(f["who"])}</a> {f["filed"]:%m/%d}',
                f["doc_id"],
            ))
    return items


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
    # raise_for_status() 의 예외 메시지에는 요청 URL 이 그대로 들어가고,
    # 그 URL 에 봇 토큰이 박혀 있다. 상태코드와 본문만 남긴다.
    if not r.ok:
        raise RuntimeError(f"telegram {r.status_code}: {r.text[:300]}")


def run_insider():
    target = _prev_business_day(dt.date.today())
    raw, failed, filings = collect_form4(target)
    rows = aggregate(raw)
    annotate_cluster(rows)
    buys, sells = filter_buys(rows), filter_sells(rows)

    broken = filings >= FORM4_ALERT_MIN_FILINGS and not rows
    print(f"form4 target={target} filings={filings} failed={failed} "
          f"rows={len(rows)} buys={len(buys)} sells={len(sells)}")

    msgs, _ = paginate(
        build_insider_message(buys, sells, target, len(rows), filings, failed, broken))
    for m in msgs:
        send(m)
    if broken:
        raise RuntimeError(f"Form 4 제출 {filings}건에서 거래 0건 추출 — 파서 점검 필요")


def run_congress():
    since = dt.date.today() - dt.timedelta(days=CONGRESS_LOOKBACK_DAYS)
    seen, done_docs = load_state()
    rows, scanned, fetched = house_ptr.collect_house(since, skip_docs=done_docs)

    if len(fetched) >= PTR_ALERT_MIN_DOCS and not rows and not scanned:
        raise RuntimeError(f"PTR {len(fetched)}건에서 거래 0건 파싱 — 파서 점검 필요")

    eligible = filter_congress(rows)
    fresh = [r for r in eligible if r["key"] not in seen]

    sent = set()
    if fresh or scanned:
        msgs, sent = paginate(build_congress_message(fresh, scanned, since))
        for m in msgs:
            send(m)
    seen |= sent
    print(f"congress fetched={len(fetched)} new={len(fresh)} "
          f"scanned={len(scanned)} sent={len(sent)}")

    # 발송이 끝난 문서만 완료 처리한다. 페이지 상한에 걸려 남은 행이 있는
    # 문서를 완료로 적으면 그 행을 다시는 받아보지 못한다.
    pending = {r["doc_id"] for r in eligible if r["key"] not in seen}
    pending |= {f["doc_id"] for f in scanned if f["doc_id"] not in sent}
    save_state(seen, done_docs | (fetched - pending))


def main():
    """두 피드는 서로 독립이다. 한쪽이 실패해도 다른 쪽은 시도한다."""
    missing = [n for n, v in (("SEC_UA", SEC_UA),
                              ("TELEGRAM_TOKEN", TG_TOKEN),
                              ("TELEGRAM_CHAT_ID", TG_CHAT)) if not v]
    if missing:
        sys.exit(f"[error] 환경변수 미설정: {', '.join(missing)}")

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

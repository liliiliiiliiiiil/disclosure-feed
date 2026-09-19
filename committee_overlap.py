"""의원의 위원회 관할과 거래 종목의 업종이 겹치는지 표시.

의회 거래에서 가장 말이 되는 신호는 "이 사람이 규제하는 업종을 샀는가"다.
그걸 판정하려면 세 가지가 필요하다.

  1. 신고자 -> 의원          unitedstates/congress-legislators (YAML)
  2. 의원 -> 소속 상임위      같은 소스의 committee-membership
  3. 티커 -> 업종            SEC company_tickers.json -> data.sec.gov 의 SIC 코드

1·3 은 공식 데이터지만 2 와 3 을 잇는 "위원회 관할 <-> 업종" 대응표는 어디에도
없다. 아래 COMMITTEE_SECTORS 는 손으로 쓴 것이고, 이 파일에서 유일하게 사실이
아니라 판단인 부분이다. 좁게 잡았다. 관할이 명확한 조합만 넣고, 세제·예산처럼
사실상 전 업종에 걸치는 위원회는 통째로 뺐다. 넣으면 거의 모든 거래에 표식이
붙어 표식 자체가 무의미해진다.

판정에 실패하면 (이름이 모호하거나, 티커가 SEC 목록에 없거나, 업종을 모르면)
표식을 달지 않는다. 틀린 표식은 없는 표식보다 나쁘다.
"""
import re
import sys
import unicodedata

import yaml

LEG_BASE = ("https://raw.githubusercontent.com/unitedstates/"
            "congress-legislators/main/")
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"


# ---------- 위원회 -> 업종 (손으로 쓴 판단) ----------

# 일부러 뺀 위원회와 그 이유:
#   Ways and Means / Senate Finance   세제·무역. 전 업종에 걸친다.
#   Appropriations / Budget           예산. 전 업종에 걸친다.
#   Judiciary                         반독점이 어디에나 닿는다.
#   Foreign Affairs / Relations       업종으로 좁혀지지 않는다.
#   Oversight, Rules, Ethics, Small Business, Veterans', Education,
#   House Administration, Indian Affairs, Aging
#                                     특정 업종 관할이라 보기 어렵다.
COMMITTEE_SECTORS = {
    # 하원
    "HSAG": ("하원 농업위", {"agriculture"}),
    "HSAS": ("하원 군사위", {"defense"}),
    "HSBA": ("하원 금융위", {"finance", "realestate"}),
    "HSHM": ("하원 국토안보위", {"defense"}),
    "HSIF": ("하원 에너지·통상위", {"energy", "utilities", "pharma", "health", "telecom"}),
    "HSII": ("하원 천연자원위", {"energy", "mining"}),
    "HSPW": ("하원 교통위", {"transport"}),
    "HSSY": ("하원 과학기술위", {"tech"}),
    "HLIG": ("하원 정보위", {"defense"}),
    # 상원
    "SSAF": ("상원 농업위", {"agriculture"}),
    "SSAS": ("상원 군사위", {"defense"}),
    "SSBK": ("상원 은행위", {"finance", "realestate"}),
    "SSCM": ("상원 통상위", {"telecom", "transport", "tech"}),
    "SSEG": ("상원 에너지위", {"energy", "mining"}),
    "SSEV": ("상원 환경위", {"utilities"}),
    "SSGA": ("상원 국토안보위", {"defense"}),
    "SSHR": ("상원 보건위", {"pharma", "health"}),
    "SLIN": ("상원 정보위", {"defense"}),
}


# ---------- SIC -> 업종 ----------

# 접두사 대응. 긴 접두사가 이긴다(3812 는 defense, 38 은 tech).
SIC_SECTORS = (
    (("2833", "2834", "2835", "2836", "8731"), "pharma"),
    (("6324",), "health"),                       # 건강보험은 금융이 아니라 보건으로
    (("3721", "3724", "3728", "3760", "3761", "3764", "3769",
      "3795", "3812", "3480", "3483", "3489"), "defense"),
    (("4922", "4923", "4924", "4925"), "energy"),   # 가스 파이프라인은 에너지
    (("6798", "1531"), "realestate"),
    (("01", "02", "07", "08", "09", "20", "21"), "agriculture"),
    (("10", "12", "14"), "mining"),
    (("13", "29", "46"), "energy"),
    (("35", "36", "38", "73"), "tech"),
    (("48",), "telecom"),
    (("49",), "utilities"),
    (("40", "41", "42", "44", "45", "47"), "transport"),
    (("80",), "health"),
    (("60", "61", "62", "63", "64", "67"), "finance"),
    (("65",), "realestate"),
)
_SIC_LOOKUP = sorted(
    ((p, tag) for prefixes, tag in SIC_SECTORS for p in prefixes),
    key=lambda x: -len(x[0]),
)


def sector_of_sic(sic):
    """SIC 코드의 업종 태그. 모르면 빈 문자열."""
    sic = (sic or "").strip()
    if not sic.isdigit():
        return ""
    for prefix, tag in _SIC_LOOKUP:
        if sic.startswith(prefix):
            return tag
    return ""


# ---------- 신고자 -> 의원 ----------

SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v|md|phd|dds|esq)\b\.?")


def _tokens(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z ]", " ", SUFFIX_RE.sub(" ", s)).split()


def _district(s):
    """'CA01' / 'CA-1' -> ('CA', 1). 파싱 못 하면 (None, None)."""
    m = re.fullmatch(r"\s*([A-Za-z]{2})\s*-?\s*(\d{1,2})\s*", s or "")
    return (m.group(1).upper(), int(m.group(2))) if m else (None, None)


class Members:
    """congress-legislators 로 만든 조회표."""

    def __init__(self, legislators, membership):
        self.by_last = {}
        self.committees = {}
        for l in legislators:
            term = l["terms"][-1]
            rec = {
                "bioguide": l["id"]["bioguide"],
                "name": l["name"].get("official_full")
                        or f"{l['name']['first']} {l['name']['last']}",
                "first": _tokens(l["name"]["first"]),
                "nickname": _tokens(l["name"].get("nickname")),
                "last": _tokens(l["name"]["last"]),
                "chamber": "Senate" if term["type"] == "sen" else "House",
                "state": term.get("state"),
                "district": term.get("district"),
            }
            if rec["last"]:
                self.by_last.setdefault(rec["last"][-1], []).append(rec)
        for tid, members in membership.items():
            if tid not in COMMITTEE_SECTORS:
                continue           # 소위원회와 매핑 없는 위원회는 볼 필요가 없다
            for m in members:
                self.committees.setdefault(m["bioguide"], []).append(
                    (tid, (m.get("title") or "")))

    def match(self, name, chamber, district=""):
        """신고자 이름으로 의원을 찾는다. 확정하지 못하면 None.

        성으로 후보를 좁히고, 원(院)과 지역구로 거르고, 마지막에 이름을 본다.
        그래도 둘 이상 남으면 포기한다. 엉뚱한 사람의 위원회를 붙이는 것보다
        아무것도 안 붙이는 편이 낫다.
        """
        toks = _tokens(name)
        if not toks:
            return None
        cands = [c for c in self.by_last.get(toks[-1], [])
                 if c["chamber"] == chamber]
        st, dist = _district(district)
        if st:
            exact = [c for c in cands
                     if c["state"] == st and c["district"] == dist]
            cands = exact or [c for c in cands if c["state"] == st] or cands
        if len(cands) > 1:
            named = [c for c in cands
                     if c["first"][:1] == toks[:1] or c["nickname"][:1] == toks[:1]]
            cands = named or cands
        return cands[0] if len(cands) == 1 else None

    def sectors_for(self, bioguide):
        """(위원회 라벨, 위원장 여부, 업종집합) 목록."""
        out = []
        for tid, title in self.committees.get(bioguide, ()):
            label, sectors = COMMITTEE_SECTORS[tid]
            out.append((label, "chair" in title.lower(), sectors))
        return out


def load_members(get):
    """get(url) -> requests.Response 를 받아 조회표를 만든다."""
    leg = yaml.safe_load(get(LEG_BASE + "legislators-current.yaml").content)
    mem = yaml.safe_load(get(LEG_BASE + "committee-membership-current.yaml").content)
    return Members(leg, mem)


# ---------- 티커 -> 업종 ----------

def sector_lookup(tickers, sec_get, cache):
    """티커별 업종 태그. cache 는 {티커: SIC} 로, 호출 측이 보존한다.

    SEC 의 티커 목록에는 ETF·뮤추얼펀드가 없다. 그래서 지수 ETF 매수가
    금융위원회 표식으로 잡히는 일이 자연히 걸러진다. 반대로 일부 상장사도
    빠져 있어(실측: EA, AVB) 그런 종목은 표식 없이 지나간다.
    """
    unknown = sorted(t for t in tickers if t not in cache)
    if unknown:
        try:
            table = sec_get(SEC_TICKERS).json()
        except Exception as e:
            print(f"[warn] SEC 티커 목록 실패: {e}", file=sys.stderr)
            table = {}
        by_ticker = {v["ticker"].upper(): v["cik_str"]
                     for v in table.values() if v.get("ticker")}
        for t in unknown:
            cik = by_ticker.get(t)
            if cik is None:
                cache[t] = ""           # 없는 종목도 기록해 매번 다시 찾지 않는다
                continue
            try:
                d = sec_get(SEC_SUBMISSIONS.format(cik=str(cik).zfill(10))).json()
                cache[t] = str(d.get("sic") or "")
            except Exception as e:
                print(f"[warn] SIC 조회 실패 {t}: {e}", file=sys.stderr)
    return {t: sector_of_sic(cache.get(t, "")) for t in tickers}


# ---------- 진입점 ----------

def annotate(rows, members, sectors):
    """각 행에 'overlap' 을 붙인다: [(위원회 라벨, 위원장 여부)].

    겹치지 않거나 판정할 수 없으면 빈 목록이다.
    """
    for r in rows:
        r["overlap"] = []
        sector = sectors.get(r["ticker"], "")
        if not sector:
            continue
        m = members.match(r["who"], r["chamber"], r.get("district", ""))
        if not m:
            continue
        r["overlap"] = [(label, chair)
                        for label, chair, s in members.sectors_for(m["bioguide"])
                        if sector in s]
    return rows

"""committee_overlap 회귀 테스트 (네트워크 불필요).

의원 픽스처는 congress-legislators 의 실제 레코드 형태를 따랐고, 이름과
소속은 2026-09-19 실측에서 확인한 값이다.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import committee_overlap as co


def _leg(bioguide, first, last, chamber, state, district=None, nickname=None):
    t = {"type": "sen" if chamber == "Senate" else "rep", "state": state,
         "district": district}
    n = {"first": first, "last": last, "official_full": f"{first} {last}"}
    if nickname:
        n["nickname"] = nickname
    return {"id": {"bioguide": bioguide}, "name": n, "terms": [t]}


LEGISLATORS = [
    _leg("B001236", "John", "Boozman", "Senate", "AR"),
    _leg("K000383", "Angus", "King", "Senate", "ME"),
    _leg("S001184", "Tim", "Scott", "Senate", "SC"),
    _leg("S001217", "Rick", "Scott", "Senate", "FL"),
    _leg("D000001", "Jane", "Doe", "House", "CA", 12),
    _leg("D000002", "John", "Doe", "House", "NY", 3),
]
MEMBERSHIP = {
    "SSAF": [{"bioguide": "B001236", "title": "Chairman", "name": "John Boozman"}],
    "SSEV": [{"bioguide": "B001236", "title": "", "name": "John Boozman"}],
    "SSAS": [{"bioguide": "K000383", "title": "", "name": "Angus King"}],
    "SSBK": [{"bioguide": "S001184", "title": "", "name": "Tim Scott"}],
    "HSBA": [{"bioguide": "D000001", "title": "", "name": "Jane Doe"}],
    # 매핑 없는 위원회와 소위원회는 무시되어야 한다
    "SSFI": [{"bioguide": "B001236", "title": "", "name": "John Boozman"}],
    "SSAF13": [{"bioguide": "K000383", "title": "Chairman", "name": "Angus King"}],
}


class SicSectors(unittest.TestCase):
    def test_longest_prefix_wins(self):
        # 3812 는 방산, 38 일반은 tech
        self.assertEqual(co.sector_of_sic("3812"), "defense")
        self.assertEqual(co.sector_of_sic("3826"), "tech")
        # 6324 는 건강보험이라 finance 가 아니라 health
        self.assertEqual(co.sector_of_sic("6324"), "health")
        self.assertEqual(co.sector_of_sic("6311"), "finance")
        # 가스 파이프라인은 에너지, 나머지 49xx 는 유틸리티
        self.assertEqual(co.sector_of_sic("4922"), "energy")
        self.assertEqual(co.sector_of_sic("4911"), "utilities")

    def test_observed_codes(self):
        # 프로브가 실제로 받아온 값들
        for sic, want in (("2834", "pharma"), ("3674", "tech"), ("3760", "defense"),
                          ("6021", "finance"), ("6798", "realestate")):
            self.assertEqual(co.sector_of_sic(sic), want, sic)

    def test_unknown_is_empty(self):
        self.assertEqual(co.sector_of_sic(""), "")
        self.assertEqual(co.sector_of_sic("9999"), "")
        self.assertEqual(co.sector_of_sic("abcd"), "")


class Matching(unittest.TestCase):
    def setUp(self):
        self.m = co.Members(LEGISLATORS, MEMBERSHIP)

    def test_plain_name(self):
        self.assertEqual(self.m.match("John Boozman", "Senate")["bioguide"], "B001236")

    def test_suffix_and_middle_initial(self):
        # EFD 가 실제로 주는 표기
        self.assertEqual(self.m.match("Angus S King, Jr.", "Senate")["bioguide"],
                         "K000383")

    def test_all_caps(self):
        self.assertEqual(self.m.match("JOHN BOOZMAN", "Senate")["bioguide"], "B001236")

    def test_chamber_separates(self):
        self.assertIsNone(self.m.match("John Boozman", "House"))

    def test_district_disambiguates_house(self):
        self.assertEqual(self.m.match("Jane Doe", "House", "CA12")["bioguide"],
                         "D000001")
        self.assertEqual(self.m.match("John Doe", "House", "NY-3")["bioguide"],
                         "D000002")

    def test_first_name_disambiguates_senate_collision(self):
        self.assertEqual(self.m.match("Tim Scott", "Senate")["bioguide"], "S001184")
        self.assertEqual(self.m.match("Rick Scott", "Senate")["bioguide"], "S001217")

    def test_gives_up_when_ambiguous(self):
        # 성만으로는 두 Scott 을 가를 수 없다 -> 아무것도 붙이지 않는다
        self.assertIsNone(self.m.match("Scott", "Senate"))

    def test_unknown_name(self):
        self.assertIsNone(self.m.match("Nobody Here", "Senate"))

    def test_only_mapped_committees_are_kept(self):
        got = {label for label, _, _ in self.m.sectors_for("B001236")}
        # SSFI(재무위)는 매핑에서 일부러 뺐다
        self.assertEqual(got, {"상원 농업위", "상원 환경위"})

    def test_subcommittees_ignored(self):
        got = {label for label, _, _ in self.m.sectors_for("K000383")}
        self.assertEqual(got, {"상원 군사위"})

    def test_chair_is_flagged(self):
        chairs = {label: chair for label, chair, _ in self.m.sectors_for("B001236")}
        self.assertTrue(chairs["상원 농업위"])
        self.assertFalse(chairs["상원 환경위"])


class Annotate(unittest.TestCase):
    def setUp(self):
        self.m = co.Members(LEGISLATORS, MEMBERSHIP)

    def row(self, who, ticker, chamber="Senate", district=""):
        return {"who": who, "ticker": ticker, "chamber": chamber,
                "district": district}

    def test_real_overlap_is_flagged(self):
        # 실측: Boozman 이 CEG(4911 Electric Services -> utilities)를 팔았고
        # 그는 상원 환경위(utilities) 소속이다.
        rows = [self.row("John Boozman", "CEG")]
        co.annotate(rows, self.m, {"CEG": "utilities"})
        self.assertEqual(rows[0]["overlap"], [("상원 환경위", False)])

    def test_chair_overlap(self):
        rows = [self.row("John Boozman", "ADM")]
        co.annotate(rows, self.m, {"ADM": "agriculture"})
        self.assertEqual(rows[0]["overlap"], [("상원 농업위", True)])

    def test_no_overlap_when_sector_differs(self):
        rows = [self.row("John Boozman", "NVDA")]
        co.annotate(rows, self.m, {"NVDA": "tech"})
        self.assertEqual(rows[0]["overlap"], [])

    def test_unknown_sector_is_not_flagged(self):
        # ETF 처럼 업종을 모르는 종목
        rows = [self.row("John Boozman", "IVV")]
        co.annotate(rows, self.m, {"IVV": ""})
        self.assertEqual(rows[0]["overlap"], [])

    def test_unmatched_member_is_not_flagged(self):
        rows = [self.row("Scott", "JPM")]
        co.annotate(rows, self.m, {"JPM": "finance"})
        self.assertEqual(rows[0]["overlap"], [])


class SectorLookup(unittest.TestCase):
    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def fake_sec(self, calls):
        def get(url):
            calls.append(url)
            if url == co.SEC_TICKERS:
                return self._Resp({"0": {"cik_str": 19617, "ticker": "JPM"},
                                   "1": {"cik_str": 936468, "ticker": "LMT"}})
            return self._Resp({"sic": "6021" if "0000019617" in url else "3760"})
        return get

    def test_resolves_and_caches(self):
        calls, cache = [], {}
        out = co.sector_lookup({"JPM", "LMT"}, self.fake_sec(calls), cache)
        self.assertEqual(out, {"JPM": "finance", "LMT": "defense"})
        self.assertEqual(cache, {"JPM": "6021", "LMT": "3760"})

        # 두 번째 호출은 캐시만 쓰고 SEC 를 다시 부르지 않는다
        calls2 = []
        out2 = co.sector_lookup({"JPM", "LMT"}, self.fake_sec(calls2), cache)
        self.assertEqual(out2, out)
        self.assertEqual(calls2, [])

    def test_unlisted_ticker_is_remembered_as_missing(self):
        # ETF 등은 SEC 목록에 없다. 매번 다시 찾지 않도록 빈 값으로 기록한다.
        cache = {}
        out = co.sector_lookup({"IVV"}, self.fake_sec([]), cache)
        self.assertEqual(out, {"IVV": ""})
        self.assertEqual(cache, {"IVV": ""})
        calls = []
        co.sector_lookup({"IVV"}, self.fake_sec(calls), cache)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

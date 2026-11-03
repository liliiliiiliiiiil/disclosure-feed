"""insider_feed 의 파싱·필터·출력 회귀 테스트 (네트워크 불필요)."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import insider_feed as f
from tests.fixtures import MASTER_IDX, form4


class _Resp:
    def __init__(self, text):
        self.text = text


class _Patched:
    """f._get 을 고정 응답으로 바꾼다."""

    def __init__(self, text):
        self.text = text

    def __enter__(self):
        self.orig = f._get
        f._get = lambda url: _Resp(self.text)
        return self

    def __exit__(self, *a):
        f._get = self.orig


class Form4Index(unittest.TestCase):
    def test_dedupes_joint_filing_by_accession(self):
        with _Patched(MASTER_IDX):
            paths = f.form4_filings(dt.date(2025, 1, 2))
        # 공동신고 2줄은 같은 접수번호라 1건, 8-K 는 제외.
        self.assertEqual(paths, ["edgar/data/1000/0001000-25-000001.txt",
                                 "edgar/data/3000/0001000-25-000002.txt"])


class ParseForm4(unittest.TestCase):
    def parse(self, raw):
        with _Patched(raw):
            return f.parse_form4("edgar/data/1/0001-25-1.txt")

    def test_basic_purchase(self):
        rows = self.parse(form4())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["ticker"], r["code"], r["value"], r["rank"]),
                         ("ACME", "P", 50_000.0, "SENIOR"))
        self.assertFalse(r["plan"])

    def test_reads_post_transaction_holding(self):
        rows = self.parse(form4(after="2578192", direct="D"))
        self.assertEqual(rows[0]["after"], 2_578_192.0)
        self.assertTrue(rows[0]["direct"])

    def test_reads_indirect_ownership(self):
        self.assertFalse(self.parse(form4(after="100", direct="I"))[0]["direct"])

    def test_absent_holding_is_none_not_zero(self):
        # 0 으로 떨어뜨리면 "전량 매도"로 잘못 읽힌다.
        r = self.parse(form4(after=None, direct=None))[0]
        self.assertIsNone(r["after"])
        self.assertIsNone(r["direct"])

    def test_non_listed_issuer_dropped(self):
        self.assertEqual(self.parse(form4(ticker="NONE")), [])

    def test_non_common_securities_dropped(self):
        rows = self.parse(form4(txns=(
            ("P", "Series A Preferred Stock", "1000", "50.00", "2025-01-02"),
            ("P", "Common Stock", "10", "5.00", "2025-01-02"),
        )))
        self.assertEqual([r["value"] for r in rows], [50.0])

    def test_aff10b5one_marks_plan_trade(self):
        self.assertTrue(self.parse(form4(aff10b5="1"))[0]["plan"])
        self.assertFalse(self.parse(form4(aff10b5="0"))[0]["plan"])

    def test_footnote_fallback_for_pre_2022_filings(self):
        rows = self.parse(form4(footnote="Sold pursuant to a Rule 10b5-1 plan."))
        self.assertTrue(rows[0]["plan"])

    def test_joint_filing_attributes_title_to_its_owner(self):
        # 직함 없는 신고자가 먼저 나와도, 표시되는 이름은 직함 주인이어야 한다.
        rows = self.parse(form4(owners=(
            ("Acme Holdings LLC", "", "0", "1"),
            ("Doe Jane", "Chief Financial Officer", "0", "0"),
        )))
        r = rows[0]
        self.assertEqual(r["owners"][0], "Doe Jane")
        self.assertEqual(r["title"], "Chief Financial Officer")
        self.assertEqual(r["rank"], "SENIOR")

    def test_see_remarks_title_is_resolved_from_remarks(self):
        # 실측 하루 7건 꼴. 직함 자리에 "See Remarks" 만 들어와 상위직
        # 관문을 통과하지 못하고 있었다.
        r = self.parse(form4(
            owners=(("Smith John", "See Remarks", "0", "0"),),
            remarks="Chief Executive Officer"))[0]
        self.assertEqual(r["title"], "Chief Executive Officer")
        self.assertEqual(r["rank"], "SENIOR")

    def test_see_remarks_wins_primary_in_a_joint_filing(self):
        r = self.parse(form4(owners=(
            ("Acme Holdings LLC", "", "0", "1"),
            ("Doe Jane", "See Remarks", "0", "0"),
        ), remarks="Chief Financial Officer"))[0]
        self.assertEqual(r["owners"][0], "Doe Jane")
        self.assertEqual(r["rank"], "SENIOR")

    def test_see_remarks_sentence_sets_rank_but_is_not_displayed(self):
        # remarks 는 직함 칸이 아니라 자유 서술이다. 등급은 올라가야 하지만
        # 문장이 직함 자리에 찍히면 안 된다.
        r = self.parse(form4(
            owners=(("Smith John", "See Remarks", "0", "0"),),
            remarks="The reporting person serves as the Chief Executive "
                    "Officer of the Issuer."))[0]
        self.assertEqual(r["rank"], "SENIOR")
        self.assertEqual(r["title"], "")

    def test_see_remarks_without_remarks_does_not_fake_a_title(self):
        # 이전에는 "See Remarks" 가 직함으로 남아 OFFICER 를 만들어냈다.
        r = self.parse(form4(
            owners=(("Smith John", "See Remarks", "1", "0"),), remarks=None))[0]
        self.assertEqual(r["title"], "Director")
        self.assertEqual(r["rank"], "DIRECTOR")

    def test_a_long_real_title_is_not_truncated(self):
        # 길이 규칙은 remarks 에서 온 직함에만 걸린다. 실제 직함에는
        # 이만큼 긴 것이 정상으로 들어온다.
        long = "Executive Vice President and Chief Administrative Officer"
        self.assertGreater(len(long), f.TITLE_MAX)
        r = self.parse(form4(owners=(("Smith John", long, "0", "0"),)))[0]
        self.assertEqual(r["title"], long)

    def test_a_real_title_ignores_remarks(self):
        r = self.parse(form4(
            owners=(("Smith John", "Chief Executive Officer", "0", "0"),),
            remarks="Shares sold to cover taxes."))[0]
        self.assertEqual(r["title"], "Chief Executive Officer")

    def test_see_remarks_inside_a_longer_title_is_left_alone(self):
        # "Officer - See Remarks" 는 직함 정보를 이미 담고 있다. 통째로
        # 갈아치우면 그쪽을 잃는다.
        r = self.parse(form4(
            owners=(("Smith John", "VP - See Remarks", "0", "0"),),
            remarks="Chief Executive Officer"))[0]
        self.assertEqual(r["title"], "VP - See Remarks")
        self.assertEqual(r["rank"], "OFFICER")

    def test_fetch_failure_returns_none(self):
        import requests

        orig = f._get
        try:
            def boom(url):
                raise requests.ConnectionError("down")
            f._get = boom
            self.assertIsNone(f.parse_form4("edgar/data/1/x.txt"))
        finally:
            f._get = orig


def _congress_row(doc, **kw):
    r = {"chamber": "House", "doc_id": doc, "key": f"{doc}|AAPL|purchase",
         "ticker": "AAPL", "type": "purchase", "amount": "$50,001 - $100,000",
         "value": 100_000.0, "value_min": 50_001.0, "who": "Rep A",
         "district": "CA01", "traded": "01/02/2025", "link": "http://x/a.pdf"}
    r.update(kw)
    return r


def _row(**kw):
    base = {"ticker": "AAA", "src": "s1", "owners": ("X",), "title": "", "rank": "",
            "code": "P", "shares": 100.0, "value": 200_000.0, "plan": False,
            "date": "2025-01-02", "after": None, "direct": True, "nat": ""}
    base.update(kw)
    return base


class Aggregate(unittest.TestCase):
    def test_merges_tranches_with_weighted_price(self):
        rows = f.aggregate([
            _row(shares=100.0, value=1_000.0, date="2025-01-03"),
            _row(shares=300.0, value=9_000.0, date="2025-01-02"),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shares"], 400.0)
        self.assertEqual(rows[0]["value"], 10_000.0)
        self.assertEqual(rows[0]["price"], 25.0)
        self.assertEqual(rows[0]["date"], "2025-01-02")


class Stake(unittest.TestCase):
    """지분 변화율. 숫자는 2026-10-27 제출분 실측값이다."""

    def test_real_aggregated_buy(self):
        # MSM: 세 tranche 합계 250,000주, 최종 보유 2,578,192
        self.assertEqual(f._stake("P", 250_000, 2_578_192)[0], 2_328_192)
        self.assertAlmostEqual(f._stake("P", 250_000, 2_578_192)[1], 0.10738, 4)

    def test_real_aggregated_sell(self):
        # NFLX: 세 tranche 합계 17,980주, 최종 보유 79,690
        before, stake = f._stake("S", 17_980, 79_690)
        self.assertEqual(before, 97_670)
        self.assertAlmostEqual(stake, -0.18409, 4)

    def test_sign_follows_direction(self):
        self.assertGreater(f._stake("P", 10, 110)[1], 0)
        self.assertLess(f._stake("S", 10, 90)[1], 0)

    def test_new_position_has_no_ratio(self):
        # 보유가 0 이었으면 비율의 분모가 없다.
        self.assertEqual(f._stake("P", 500, 500), (0.0, None))

    def test_full_exit(self):
        self.assertEqual(f._stake("S", 1000, 0), (1000, -1.0))

    def test_missing_holding_is_not_guessed(self):
        self.assertEqual(f._stake("P", 100, None), (None, None))

    def test_inconsistent_filing_is_dropped(self):
        # 산 주식 수가 거래 후 보유보다 많으면 숫자가 어긋난 것이다.
        self.assertEqual(f._stake("P", 500, 100), (None, None))


class StakeText(unittest.TestCase):
    def fmt(self, code, shares, after, direct=True):
        before, stake = f._stake(code, shares, after)
        return f.stake_str({"code": code, "shares": shares, "after": after,
                            "before": before, "stake": stake, "direct": direct})

    def test_percent(self):
        self.assertEqual(self.fmt("P", 250_000, 2_578_192), "지분 +11%")
        self.assertEqual(self.fmt("S", 17_980, 79_690), "지분 -18%")

    def test_sub_one_percent_keeps_a_decimal(self):
        # CBKM 실측: 500주, 거래후 81,853 -> 0.6%. 반올림하면 "+0%" 가 된다.
        self.assertEqual(self.fmt("P", 500, 81_853), "지분 +0.6%")

    def test_large_ratio_reads_as_a_multiple(self):
        self.assertEqual(self.fmt("P", 10_000, 10_100), "지분 +100배")

    def test_new_and_exit(self):
        self.assertEqual(self.fmt("P", 500, 500), "신규")
        self.assertEqual(self.fmt("S", 1000, 0), "전량 매도")

    def test_indirect_is_marked(self):
        # UTHR 실측: 간접 보유 600주 매도
        self.assertEqual(self.fmt("S", 600, 333_343, direct=False),
                         "지분 -0.2% (간접)")

    def test_unknown_renders_nothing(self):
        self.assertEqual(self.fmt("P", 100, None), "")


class AggregateStake(unittest.TestCase):
    """합산 거래의 지분 변화율. 숫자는 2026-10-30 제출분 실측값이다."""

    def rows(self, *specs):
        """(거래량, 거래후잔고, 직접여부, 보유형태서술) 목록 -> 원시 행"""
        return [dict(_row(code=self.CODE, shares=sh, value=sh * 10.0,
                          after=a, direct=d, nat=n), date="2025-01-02")
                for sh, a, d, n in specs]

    CODE = "P"

    def test_single_pool_tranches(self):
        # MSM 실측: 한 풀 안의 3회 체결. 잔고가 거래량만큼 이어진다.
        out = f.aggregate(self.rows(
            (60_951, 2_389_143, True, ""),
            (134_662, 2_523_805, True, ""),
            (54_387, 2_578_192, True, "")))
        self.assertEqual(out[0]["shares"], 250_000)
        # 분모는 첫 행 직전의 잔고 = 2,389,143 - 60,951
        self.assertEqual(out[0]["before"], 2_328_192)
        self.assertAlmostEqual(out[0]["stake"], 0.10738, 4)

    def test_several_indirect_pools_are_summed(self):
        # RAPP 실측: 네 행 모두 간접이지만 서술이 달라 별개 풀이다.
        # 이전 코드는 마지막 풀 잔고만 써서 분모가 음수가 되어 표기를 버렸다.
        out = f.aggregate(self.rows(
            (952_380, 2_815_707, False, "By Sofinnova Venture Partners XI"),
            (6_395, 17_990, False, "By SummitTX Master, SPC -SummitTX A"),
            (3_772, 14_924, False, "By SummitTX Master, SPC - SummitTX B"),
            (6_933, 25_267, False, "By SummitTX Pinnacle Master, L.P.")))
        self.assertEqual(out[0]["shares"], 969_480)
        # 1,863,327 + 11,595 + 11,152 + 18,334
        self.assertEqual(out[0]["before"], 1_904_408)
        self.assertEqual(f.stake_str(out[0]), "지분 +51% (간접)")

    def test_two_pools_are_not_overstated(self):
        # BROS 실측 구조: 한 신고자가 두 법인으로 나눠 매도.
        # 마지막 풀 잔고만 쓰면 분모가 작아져 비율이 뻥튀김한다.
        self.CODE = "S"
        rows = self.rows((600, 400, False, "By DM Trust Aggregator, LLC"),
                         (400, 600, False, "By DM Individual Aggregator, LLC"))
        out = f.aggregate(rows)
        # 올바른 분모 = (400+600) + (600+400) = 2,000, 1,000/2,000 = -50%
        self.assertEqual(out[0]["before"], 2_000)
        self.assertAlmostEqual(out[0]["stake"], -0.5, 6)
        # 옛 방식(마지막 잔고 600 + 1,000 = 1,600)이면 -63% 로 과대 표시됐다
        self.assertNotAlmostEqual(out[0]["stake"], -1_000 / 1_600, 3)

    def test_broken_chain_in_one_pool_is_dropped(self):
        # MCO·OTLC·MED 실측: 풀 서술이 같은데 잔고가 이어지지 않는다.
        # 중간에 부여·행사·원천징수가 끼었을 수도, 같은 서술 아래 다른 풀이
        # 숨었을 수도 있다. 가릴 수 없으니 버린다.
        out = f.aggregate(self.rows((100, 5_000, True, ""),
                                    (100, 90_000, True, "")))
        self.assertIsNone(out[0]["stake"])
        self.assertEqual(f.stake_str(out[0]), "")

    def test_direct_and_indirect_mix_still_computes(self):
        # 섞여도 풀별로 나누면 분모가 분명하다. 이전에는 통째로 버렸다.
        out = f.aggregate(self.rows((100, 1_100, True, ""),
                                    (100, 600, False, "By spouse")))
        # (1,100-100) + (600-100) = 1,500
        self.assertEqual(out[0]["before"], 1_500)
        self.assertAlmostEqual(out[0]["stake"], 200 / 1_500, 6)
        # 다만 표기상 직접/간접을 한쪽으로 말할 수는 없다
        self.assertIsNone(out[0]["direct"])
        self.assertEqual(f.stake_str(out[0]), "지분 +13%")

    def test_missing_holding_propagates(self):
        out = f.aggregate(self.rows((100, None, True, "")))
        self.assertIsNone(out[0]["stake"])

    def test_other_merged_fields_unchanged(self):
        rows = self.rows((10, 110, True, ""), (20, 130, True, ""))
        rows[0]["plan"], rows[1]["date"] = True, "2025-01-01"
        out = f.aggregate(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["shares"], 30)
        self.assertEqual(out[0]["value"], 300.0)
        self.assertEqual(out[0]["price"], 10.0)
        self.assertTrue(out[0]["plan"])
        self.assertEqual(out[0]["date"], "2025-01-01")


class Chains(unittest.TestCase):
    def rs(self, code, *pairs):
        return [{"code": code, "shares": sh, "after": a} for sh, a in pairs]

    def test_buy_chain(self):
        self.assertTrue(f._chains(self.rs("P", (10, 110), (20, 130))))

    def test_sell_chain(self):
        self.assertTrue(f._chains(self.rs("S", (10, 90), (20, 70))))

    def test_gap_breaks(self):
        self.assertFalse(f._chains(self.rs("P", (10, 110), (20, 500))))

    def test_single_row_chains(self):
        self.assertTrue(f._chains(self.rs("P", (10, 110))))

    def test_missing_holding_breaks(self):
        self.assertFalse(f._chains(self.rs("P", (10, 110), (20, None))))


class Cluster(unittest.TestCase):
    def test_ineligible_buys_do_not_form_a_cluster(self):
        # 소액 2건 + 계획매매 1건은 정원을 채우면 안 된다.
        rows = [
            _row(src="a", value=500.0),
            _row(src="b", value=500.0),
            _row(src="c", value=200_000.0, plan=True),
            _row(src="d", value=200_000.0),
        ]
        f.annotate_cluster(rows)
        self.assertEqual({r["src"]: r["cluster"] for r in rows}["d"], 1)
        self.assertEqual(f.filter_buys(rows), [])

    def test_real_cluster_passes(self):
        rows = [_row(src=s, value=200_000.0) for s in ("a", "b", "c")]
        f.annotate_cluster(rows)
        self.assertEqual(len(f.filter_buys(rows)), 3)
        self.assertTrue(all(r["cluster"] == 3 for r in rows))


class CongressFilter(unittest.TestCase):
    def rows(self, *bounds):
        return [{"value_min": lo, "value": hi} for lo, hi in bounds]

    def test_lower_bound_decides(self):
        kept = f.filter_congress(self.rows((1_001, 15_000), (15_001, 50_000)))
        # $1,001-$15,000 은 빠지고 $15,001-$50,000 부터 통과한다.
        self.assertEqual([r["value_min"] for r in kept], [15_001])


class Paginate(unittest.TestCase):
    def test_returns_only_keys_actually_sent(self):
        long_line = "x" * 900
        items = [(long_line, f"k{i}") for i in range(4 * 5)]
        msgs, sent = f.paginate(items)
        self.assertLessEqual(len(msgs), f.MAX_MESSAGES)
        # 잘린 분의 키는 반환되지 않아야 다음 실행에서 다시 잡힌다.
        self.assertLess(len(sent), len(items))
        self.assertTrue(msgs[-1].endswith("…"))

    def test_markup_does_not_eat_the_budget(self):
        # 가시 길이 기준이므로, 태그가 긴 줄도 개수대로 들어가야 한다.
        line = f.tv("AAAA") + " " + "y" * 60
        msgs, sent = f.paginate([(line, f"k{i}") for i in range(40)])
        self.assertEqual(len(msgs), 1)
        self.assertEqual(len(sent), 40)

    def test_stays_under_telegram_limit(self):
        # 상한은 엔티티 파싱 후 길이에 걸린다.
        line = f.tv("AAAA") + " " + "y" * 120
        msgs, _ = f.paginate([(line, f"k{i}") for i in range(200)])
        for m in msgs:
            self.assertLessEqual(len(f.TAG_RE.sub("", m)), 4096)


class CongressMessage(unittest.TestCase):
    def base(self, **kw):
        r = {"ticker": "AAA", "amount": "$50,001 - $100,000", "who": "Rep A",
             "district": "CA01", "traded": "01/02/2025", "link": "http://x/1.pdf",
             "key": "k1", "doc_id": "1", "type": "purchase", "value": 100_000.0,
             "value_min": 50_001.0}
        r.update(kw)
        return r

    def test_exchange_rows_are_shown_and_keyed(self):
        items = f.build_congress_message(
            [self.base(type="exchange", ticker="NVDA", key="ke")], [], dt.date(2025, 1, 1))
        text = "\n".join(ln for ln, _ in items)
        self.assertIn("교환 (E)", text)
        self.assertIn("NVDA", text)
        self.assertIn("ke", {k for _, k in items if k})

    def test_exchange_section_hidden_when_empty(self):
        items = f.build_congress_message([self.base()], [], dt.date(2025, 1, 1))
        self.assertNotIn("교환", "\n".join(ln for ln, _ in items))

    def test_every_row_carries_its_key(self):
        rows = [self.base(key=f"k{i}", ticker=f"T{i}") for i in range(30)]
        items = f.build_congress_message(rows, [], dt.date(2025, 1, 1))
        _, sent = f.paginate(items)
        # TOP_N 으로 잘리지 않아야 한다 (잘린 분이 발송 완료로 기록되던 버그).
        self.assertEqual(sent, {f"k{i}" for i in range(30)})

    def test_scanned_filings_are_all_listed(self):
        scanned = [{"doc_id": f"d{i}", "url": f"http://x/{i}.pdf", "who": f"Rep {i}",
                    "filed": dt.date(2025, 1, 2)} for i in range(9)]
        items = f.build_congress_message([], scanned, dt.date(2025, 1, 1))
        _, sent = f.paginate(items)
        self.assertEqual(sent, {f"d{i}" for i in range(9)})


class InsiderMessage(unittest.TestCase):
    def build(self, **kw):
        a = {"buys": [], "sells": [], "date": dt.date(2025, 1, 2), "total": 0,
             "filings": 0, "failed": 0, "broken": False}
        a.update(kw)
        return "\n".join(ln for ln, _ in f.build_insider_message(
            a["buys"], a["sells"], a["date"], a["total"],
            a["filings"], a["failed"], a["broken"]))

    def test_broken_parser_is_announced(self):
        # 조용한 날과 고장 난 날이 구분되어야 한다.
        self.assertIn("파서 점검 필요", self.build(filings=800, broken=True))
        self.assertNotIn("파서 점검 필요", self.build(filings=800))

    def test_dropped_filings_are_announced(self):
        self.assertIn("수집 실패 7건", self.build(filings=800, failed=7))


class State(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig = f.STATE_PATH
        f.STATE_PATH = os.path.join(self.tmp, "seen.json")

    def tearDown(self):
        f.STATE_PATH = self.orig

    def test_round_trip(self):
        f.save_state({"k1", "k2"}, {"d1"}, {"house": 7, "senate": 3},
                     {"AAPL": "3571"}, {"house": 0, "senate": 4})
        self.assertEqual(f.load_state(),
                         ({"k1", "k2"}, {"d1"}, {"house": 7, "senate": 3},
                          {"AAPL": "3571"}, {"house": 0, "senate": 4}))

    def test_reads_legacy_flat_list(self):
        with open(f.STATE_PATH, "w") as fh:
            json.dump(["k1", "k2"], fh)
        self.assertEqual(f.load_state(),
                         ({"k1", "k2"}, set(), {"house": 0, "senate": 0}, {},
                          {"house": 0, "senate": 0}))

    def test_reads_state_written_before_the_counter_existed(self):
        with open(f.STATE_PATH, "w") as fh:
            json.dump({"keys": ["k1"], "docs": ["d1"]}, fh)
        self.assertEqual(f.load_state(),
                         ({"k1"}, {"d1"}, {"house": 0, "senate": 0}, {},
                          {"house": 0, "senate": 0}))

    def test_reads_counter_from_before_the_senate_was_added(self):
        # 당시 dry 는 하원 하나뿐이라 정수였다.
        with open(f.STATE_PATH, "w") as fh:
            json.dump({"keys": [], "docs": [], "dry": 12}, fh)
        self.assertEqual(f.load_state()[2], {"house": 12, "senate": 0})

    def test_missing_file(self):
        f.STATE_PATH = os.path.join(self.tmp, "nope", "seen.json")
        self.assertEqual(f.load_state(),
                         (set(), set(), {"house": 0, "senate": 0}, {},
                          {"house": 0, "senate": 0}))


class DryStreak(unittest.TestCase):
    """하원 PTR 파서 붕괴 감지: 실행이 아니라 문서를 누적해서 센다."""

    def test_accumulates_across_runs(self):
        streak = 0
        for parsed in (2, 3, 1):          # 실행마다 신규 문서 몇 건씩
            streak = f.dry_streak(streak, parsed, 0)
        self.assertEqual(streak, 6)

    def test_any_transaction_resets(self):
        self.assertEqual(f.dry_streak(29, 4, 1), 0)

    def test_run_with_no_new_documents_is_not_evidence(self):
        # 전부 처리 완료라 파싱한 문서가 없는 실행은 값을 바꾸지 않는다.
        self.assertEqual(f.dry_streak(12, 0, 0), 12)

    def test_scanned_only_run_does_not_accumulate(self):
        # run_congress 는 parsed 를 fetched - scanned 로 넘긴다.
        fetched, scanned = 5, 5
        self.assertEqual(f.dry_streak(3, fetched - scanned, 0), 3)


class CongressAlert(unittest.TestCase):
    """경보가 텔레그램으로 나가고, 종료코드도 실패로 남는지."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_path, self.orig_send = f.STATE_PATH, f.send
        self.orig_collectors = f.COLLECTORS
        self.orig_annotate = f.annotate_overlap
        f.STATE_PATH = os.path.join(self.tmp, "seen.json")
        self.sent = []
        f.send = self.sent.append
        f.annotate_overlap = lambda rows, sic: [r.update(overlap=[]) for r in rows]

    def tearDown(self):
        f.STATE_PATH, f.send = self.orig_path, self.orig_send
        f.COLLECTORS = self.orig_collectors
        f.annotate_overlap = self.orig_annotate

    def feed(self, docs=(), rows=(), senate=None):
        """두 수집기를 모두 대체한다. 하나라도 빠뜨리면 실제 EFD 를 때린다."""
        def house(since, skip_docs=()):
            return ([r for r in rows], [],
                    {d for d in docs if d not in skip_docs})
        f.COLLECTORS = (("house", house),
                        ("senate", senate or (lambda since, skip_docs=(): ([], [], set()))))

    def test_alerts_only_after_the_threshold(self):
        f.save_state(set(), set(), {"house": f.PTR_DRY_DOCS_ALERT - 2})
        self.feed({"a"})
        f.run_congress()
        self.assertEqual(self.sent, [])
        self.assertEqual(f.load_state()[2]["house"], f.PTR_DRY_DOCS_ALERT - 1)

        self.feed({"b"})
        with self.assertRaises(RuntimeError):
            f.run_congress()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("하원", self.sent[0])
        self.assertIn("파서 점검 필요", self.sent[0])

    def test_counter_survives_the_raised_alert(self):
        f.save_state(set(), set(), {"house": f.PTR_DRY_DOCS_ALERT})
        self.feed({"a"})
        with self.assertRaises(RuntimeError):
            f.run_congress()
        self.assertEqual(f.load_state()[2]["house"], f.PTR_DRY_DOCS_ALERT + 1)

    def test_chambers_are_counted_separately(self):
        # 물량 많은 쪽이 고장 난 쪽을 가리면 안 된다.
        def senate(since, skip_docs=()):
            return [], [], {"s1"}
        f.save_state(set(), set(), {"house": 0, "senate": f.PTR_DRY_DOCS_ALERT - 1})
        self.feed({"h1"}, rows=[_congress_row("h1")], senate=senate)
        with self.assertRaises(RuntimeError) as cm:
            f.run_congress()
        self.assertIn("상원", str(cm.exception))
        state = f.load_state()[2]
        self.assertEqual(state["house"], 0)       # 하원은 거래가 나와 리셋
        self.assertEqual(state["senate"], f.PTR_DRY_DOCS_ALERT)

    def test_one_chamber_failing_does_not_block_the_other(self):
        def senate(since, skip_docs=()):
            raise RuntimeError("EFD 양식 변경")
        self.feed({"h1"}, rows=[_congress_row("h1")], senate=senate)
        with self.assertRaises(RuntimeError) as cm:
            f.run_congress()
        self.assertIn("수집 실패", str(cm.exception))
        # 하원 공시는 그래도 나갔어야 한다.
        self.assertEqual(len(self.sent), 1)
        self.assertIn("AAPL", self.sent[0])

    def test_repeated_failure_goes_quiet_after_announcing_once(self):
        def senate(since, skip_docs=()):
            raise RuntimeError("403")

        # 1회: 그대로 시끄럽게. 일시적 장애일 수 있다.
        self.feed({"h1"}, rows=[_congress_row("h1")], senate=senate)
        with self.assertRaises(RuntimeError):
            f.run_congress()
        self.assertEqual(f.load_state()[4]["senate"], 1)
        self.assertNotIn("조용히", self.sent[-1])

        # 2회: 한 번 알리고 런은 통과시킨다.
        self.feed({"h2"}, rows=[_congress_row("h2")], senate=senate)
        f.run_congress()
        self.assertEqual(f.load_state()[4]["senate"], 2)
        self.assertIn("상원", self.sent[-1])
        self.assertIn("조용히", self.sent[-1])

        # 3회 이후: 공지도 없고 런도 통과한다.
        self.feed({"h3"}, rows=[_congress_row("h3")], senate=senate)
        f.run_congress()
        self.assertEqual(f.load_state()[4]["senate"], 3)
        self.assertNotIn("조용히", self.sent[-1])
        self.assertNotIn("수집 복구", self.sent[-1])

    def test_recovery_is_announced_once_and_resets(self):
        f.save_state(set(), set(), None, None, {"senate": 6})
        self.feed({"h1"}, rows=[_congress_row("h1")])
        f.run_congress()
        self.assertIn("상원 PTR 수집 복구 (6회 연속 실패 후)", self.sent[-1])
        self.assertEqual(f.load_state()[4]["senate"], 0)

        self.feed({"h2"}, rows=[_congress_row("h2")])
        f.run_congress()
        self.assertNotIn("복구", self.sent[-1])

    def test_quiet_failure_still_sends_when_there_is_nothing_else(self):
        # 공지만 있는 실행도 나가야 한다. 안 나가면 조용해진 사실조차 모른다.
        def senate(since, skip_docs=()):
            raise RuntimeError("403")
        f.save_state(set(), set(), None, None, {"senate": 1})
        self.feed(senate=senate)
        f.run_congress()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("조용히", self.sent[0])

    def test_the_other_chamber_failing_does_not_silence_this_one(self):
        def senate(since, skip_docs=()):
            raise RuntimeError("403")
        # 상원은 이미 조용해진 상태, 하원은 지금 처음 실패한다.
        f.save_state(set(), set(), None, None, {"senate": 5})

        def house(since, skip_docs=()):
            raise RuntimeError("Clerk ZIP 404")
        f.COLLECTORS = (("house", house), ("senate", senate))
        with self.assertRaises(RuntimeError) as cm:
            f.run_congress()
        self.assertIn("하원", str(cm.exception))
        self.assertNotIn("상원", str(cm.exception))
        state = f.load_state()[4]
        self.assertEqual((state["house"], state["senate"]), (1, 6))

    def test_overlap_failure_does_not_stop_the_digest(self):
        # 교차참조는 본문이 아니라 주석이다. 소스가 죽어도 공시는 가야 한다.
        f.annotate_overlap = self.orig_annotate
        orig = f.committee_overlap.load_members
        f.committee_overlap.load_members = lambda get: (_ for _ in ()).throw(
            RuntimeError("legislators 소스 다운"))
        try:
            self.feed({"h1"}, rows=[_congress_row("h1")])
            f.run_congress()
        finally:
            f.committee_overlap.load_members = orig
        self.assertEqual(len(self.sent), 1)
        self.assertIn("AAPL", self.sent[0])

    def test_healthy_run_stays_quiet_and_resets(self):
        f.save_state(set(), set(), {"house": f.PTR_DRY_DOCS_ALERT - 1})
        self.feed({"a"}, rows=[_congress_row("a")])
        f.run_congress()
        self.assertEqual(f.load_state()[2]["house"], 0)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("파서 점검 필요", self.sent[0])


class Format(unittest.TestCase):
    def test_money(self):
        self.assertEqual(f.money(2_500_000), "$2.5M")
        self.assertEqual(f.money(150_000), "$150K")
        self.assertEqual(f.money(500), "$500")

    def test_esc(self):
        self.assertEqual(f.esc("a<b>&c"), "a&lt;b&gt;&amp;c")

    def test_prev_business_day_skips_weekend(self):
        self.assertEqual(f._prev_business_day(dt.date(2025, 1, 6)), dt.date(2025, 1, 3))
        self.assertEqual(f._prev_business_day(dt.date(2025, 1, 7)), dt.date(2025, 1, 6))


if __name__ == "__main__":
    unittest.main()

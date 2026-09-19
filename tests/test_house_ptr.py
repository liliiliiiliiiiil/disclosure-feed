"""house_ptr 의 PTR 텍스트 파싱 회귀 테스트 (네트워크 불필요)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import house_ptr
from tests.fixtures import PTR_TEXT


class ParseTxns(unittest.TestCase):
    def setUp(self):
        self.rows = house_ptr.parse_txns(PTR_TEXT)
        self.by_ticker = {r["ticker"]: r for r in self.rows}

    def test_only_ticker_rows(self):
        # 지방채 행은 괄호 티커가 없어 잡히지 않아야 한다.
        self.assertEqual(sorted(self.by_ticker), ["AAPL", "BRK.B", "MSFT", "NVDA", "TSLA"])

    def test_transaction_types(self):
        self.assertEqual(self.by_ticker["AAPL"]["type"], "purchase")
        self.assertEqual(self.by_ticker["MSFT"]["type"], "sale")    # S (partial)
        self.assertEqual(self.by_ticker["TSLA"]["type"], "sale")    # S (full)
        self.assertEqual(self.by_ticker["NVDA"]["type"], "exchange")

    def test_range_bounds(self):
        aapl = self.by_ticker["AAPL"]
        self.assertEqual(aapl["value_min"], 50_001)
        self.assertEqual(aapl["value"], 100_000)

    def test_over_bracket_has_both_bounds(self):
        brk = self.by_ticker["BRK.B"]
        self.assertEqual(brk["value_min"], 50_000_000)
        self.assertEqual(brk["value"], 50_000_000)

    def test_dotted_ticker(self):
        self.assertIn("BRK.B", self.by_ticker)


class FetchAndParse(unittest.TestCase):
    """수집 실패(failed)와 스캔본(scanned)은 구분되어야 한다.

    둘을 섞으면 실패한 문서가 '처리 완료'로 기록되어 영구 누락이 된다.
    """

    def setUp(self):
        self.filing = {"doc_id": "20001", "url": "http://x/1.pdf", "who": "A",
                       "district": "CA01", "filed": None, "year": 2025}
        self._get = house_ptr._get

    def tearDown(self):
        house_ptr._get = self._get

    def test_http_failure_is_not_scanned(self):
        import requests

        def boom(url, timeout=60):
            raise requests.ConnectionError("down")

        house_ptr._get = boom
        out = house_ptr.fetch_and_parse(self.filing)
        self.assertTrue(out["failed"])
        self.assertFalse(out["scanned"])
        self.assertEqual(out["txns"], [])


if __name__ == "__main__":
    unittest.main()

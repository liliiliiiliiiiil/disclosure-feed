"""senate_efd 회귀 테스트. 픽스처는 실제 EFD 응답을 옮긴 것이다 (네트워크 불필요)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import senate_efd as se
from tests.fixtures import SENATE_PTR_HTML


class ParseTxns(unittest.TestCase):
    def setUp(self):
        self.rows = se.parse_txns(SENATE_PTR_HTML)
        self.by_ticker = {r["ticker"]: r for r in self.rows}

    def test_all_ticker_rows_captured(self):
        # 지방채 행은 티커를 특정할 수 없어 빠진다.
        self.assertEqual(sorted(r["ticker"] for r in self.rows),
                         ["AVB", "EA", "WMB"])

    def test_ticker_column_wins(self):
        self.assertEqual(self.by_ticker["WMB"]["asset_type"], "Stock Option")

    def test_falls_back_to_asset_name_when_column_empty(self):
        # Ticker 칸이 '--' 여도 자산명에 심볼이 하나면 살린다.
        self.assertEqual(self.by_ticker["EA"]["type"], "sale")

    def test_exchange_row_keeps_single_symbol(self):
        self.assertEqual(self.by_ticker["AVB"]["type"], "exchange")

    def test_ambiguous_symbols_are_dropped(self):
        html = SENATE_PTR_HTML.replace(
            "AvalonBay Communities, Inc. Common Stock (AVB) (Exchanged) VMRK",
            "AvalonBay (AVB) exchanged for Vivmark (VMRK)")
        self.assertNotIn("AVB", {r["ticker"] for r in se.parse_txns(html)})

    def test_amount_bounds(self):
        wmb = self.by_ticker["WMB"]
        self.assertEqual((wmb["value_min"], wmb["value"]), (15_001, 50_000))

    def test_rows_match_the_house_row_shape(self):
        # insider_feed 의 필터·출력이 두 원을 구분 없이 다루려면 키가 같아야 한다.
        # 한쪽 모듈만 필드를 바꾸면 여기서 걸린다.
        import house_ptr
        from tests.fixtures import PTR_TEXT
        house = house_ptr.parse_txns(PTR_TEXT)[0]
        shared = set(house) - {"asset_type"}      # 하원은 [ST] 코드, 상원은 문구
        self.assertTrue(shared <= set(self.rows[0]),
                        f"상원 행에 없는 키: {shared - set(self.rows[0])}")


class WafRetry(unittest.TestCase):
    """EFD 앞단 WAF 의 403 은 일시적이므로 재시도한다."""

    class _Resp:
        def __init__(self, status):
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError(f"{self.status_code} for url")

    class _Session:
        """정해둔 상태코드를 차례로 돌려준다."""

        def __init__(self, statuses):
            self.statuses = list(statuses)
            self.calls = 0

        def request(self, method, url, **kw):
            self.calls += 1
            return WafRetry._Resp(self.statuses.pop(0))

    def setUp(self):
        self.slept = []
        self.orig_sleep, self.orig_throttle = se.time.sleep, se._throttle
        se.time.sleep = self.slept.append
        se._throttle = lambda: None

    def tearDown(self):
        se.time.sleep, se._throttle = self.orig_sleep, self.orig_throttle

    def test_403_then_success(self):
        sess = self._Session([403, 403, 200])
        r = se._request(sess, "GET", "http://x/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sess.calls, 3)

    def test_403_uses_the_long_backoff(self):
        sess = self._Session([403, 200])
        se._request(sess, "GET", "http://x/")
        self.assertEqual(self.slept, [se.WAF_BACKOFF])

    def test_other_retryable_keeps_the_short_backoff(self):
        # 503 까지 분 단위로 기다리게 만들면 안 된다.
        sess = self._Session([503, 200])
        se._request(sess, "GET", "http://x/")
        self.assertEqual(self.slept, [1])

    def test_persistent_403_still_raises(self):
        import requests
        sess = self._Session([403] * (se.HTTP_RETRIES + 1))
        with self.assertRaises(requests.HTTPError):
            se._request(sess, "GET", "http://x/")
        self.assertEqual(sess.calls, se.HTTP_RETRIES + 1)

    def test_404_is_not_retried(self):
        import requests
        sess = self._Session([404])
        with self.assertRaises(requests.HTTPError):
            se._request(sess, "GET", "http://x/")
        self.assertEqual(sess.calls, 1)

    def test_backoff_schedule(self):
        self.assertEqual([se._backoff(403, i) for i in range(3)], [30, 60, 120])
        self.assertEqual([se._backoff(503, i) for i in range(3)], [1, 2, 4])
        self.assertEqual([se._backoff(None, i) for i in range(3)], [1, 2, 4])


class TxnType(unittest.TestCase):
    def test_known_types(self):
        self.assertEqual(se._txn_type("Purchase"), "purchase")
        self.assertEqual(se._txn_type("Sale (Full)"), "sale")
        self.assertEqual(se._txn_type("Sale (Partial)"), "sale")
        self.assertEqual(se._txn_type("Exchange"), "exchange")

    def test_unknown_type_is_dropped_not_guessed(self):
        self.assertEqual(se._txn_type("Something Else"), "")


class Ticker(unittest.TestCase):
    def test_column_value(self):
        self.assertEqual(se._ticker("wmb", "whatever"), "WMB")

    def test_placeholder_column(self):
        self.assertEqual(se._ticker("--", "No symbol here"), "")

    def test_single_parenthesised_symbol(self):
        self.assertEqual(se._ticker("--", "Electronic Arts Inc. (EA)"), "EA")

    def test_two_different_symbols_rejected(self):
        self.assertEqual(se._ticker("--", "Foo (AVB) for Bar (VMRK)"), "")

    def test_same_symbol_twice_is_fine(self):
        self.assertEqual(se._ticker("--", "Foo (EA) and more Foo (EA)"), "EA")

    def test_junk_column_rejected(self):
        self.assertEqual(se._ticker("not a ticker", "nothing here"), "")


if __name__ == "__main__":
    unittest.main()

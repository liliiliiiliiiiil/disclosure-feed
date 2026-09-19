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

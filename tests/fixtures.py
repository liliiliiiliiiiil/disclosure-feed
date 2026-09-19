"""테스트용 고정 입력.

실제 소스 포맷을 본떠 만든 최소 샘플이다. 이 프로젝트의 위험은 전부
정규식과 XPath 에 몰려 있는데, 네트워크 없이 검증할 수 있는 부분이 대부분이라
픽스처만 있으면 회귀는 잡힌다.
"""

# daily-index master.idx: 헤더 + 공동신고(같은 접수번호, 다른 CIK 디렉터리)
MASTER_IDX = """\
CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
1000|ACME INC|4|20250102|edgar/data/1000/0001000-25-000001.txt
2000|SMITH JOHN|4|20250102|edgar/data/2000/0001000-25-000001.txt
3000|BETA CORP|4|20250102|edgar/data/3000/0001000-25-000002.txt
4000|GAMMA LLC|8-K|20250102|edgar/data/4000/0001000-25-000003.txt
"""


def form4(ticker="ACME", owners=(("Smith John", "Chief Executive Officer", "0", "0"),),
          txns=(("P", "Common Stock", "1000", "50.00", "2025-01-02"),),
          aff10b5=None, footnote=None):
    """Form 4 전체 제출본(.txt) 흉내. SGML 래퍼 안에 XML 이 들어간다."""
    ro = "".join(
        f"""
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{director}</isDirector>
      <isTenPercentOwner>{tenpct}</isTenPercentOwner>
      {f"<officerTitle>{title}</officerTitle>" if title else ""}
    </reportingOwnerRelationship>
  </reportingOwner>"""
        for name, title, director, tenpct in owners
    )
    rows = "".join(
        f"""
    <nonDerivativeTransaction>
      <securityTitle><value>{sec}</value></securityTitle>
      <transactionDate><value>{date}</value></transactionDate>
      <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{px}</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>"""
        for code, sec, shares, px, date in txns
    )
    aff = f"<aff10b5One>{aff10b5}</aff10b5One>" if aff10b5 is not None else ""
    fn = f"<footnotes><footnote>{footnote}</footnote></footnotes>" if footnote else ""
    return f"""<SEC-DOCUMENT>
<TEXT>
<XML>
<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerName>{ticker} Inc</issuerName>
    <issuerTradingSymbol>{ticker}</issuerTradingSymbol>
  </issuer>{ro}
  {aff}
  <nonDerivativeTable>{rows}
  </nonDerivativeTable>
  {fn}
</ownershipDocument>
</XML>
</TEXT>
</SEC-DOCUMENT>
"""


# pdfplumber 가 PTR 에서 뽑아내는 텍스트 모양. 라벨 폰트가 깨져 헤딩은
# 사라지고 거래 행만 정상 추출된다.
PTR_TEXT = """\
ID Owner Asset Transaction Date Notification Amount
Type Date
SP Apple Inc. (AAPL) [ST] P 01/02/2025 01/20/2025 $50,001 - $100,000
JT Microsoft Corp (MSFT) [ST] S (partial) 01/03/2025 01/20/2025 $1,001 - $15,000
Tesla Inc (TSLA) [ST] S (full) 01/04/2025 01/20/2025 $15,001 - $50,000
Nvidia Corp (NVDA) [ST] E 01/05/2025 01/20/2025 $100,001 - $250,000
Berkshire (BRK.B) [ST] P 01/06/2025 01/20/2025 Over $50,000,000
City of Somewhere Municipal Bond 4.5% P 01/07/2025 01/20/2025 $250,001 - $500,000
"""


# ---- 상원 EFD ----
# 아래 두 개는 지어낸 것이 아니라 2026-09-19 탐색 실행이 실제 EFD 에서
# 받아온 응답을 그대로 옮긴 것이다. 헤더 9칸, 검색 행 5칸.

SENATE_SEARCH_JSON = {
    "result": "ok", "draw": 0, "recordsTotal": 2, "recordsFiltered": 2,
    "data": [
        ["Alan", "Armstrong", "Armstrong, Alan (Senator)",
         '<a href="/search/view/ptr/b999bc0e-3eb0-4ca9-ab07-8e8f2e04b41f/"'
         ' target="_blank">Periodic Transaction Report for 09/17/2026</a>',
         "09/17/2026"],
        ["Angus S", "King, Jr.", "King, Angus (Senator)",
         '<a href="/search/view/paper/e67c6e56-c81e-4e74-b960-849a460165e2/"'
         ' target="_blank">Periodic Transaction Report for 09/14/2026</a>',
         "09/14/2026"],
    ],
}

SENATE_PTR_HTML = """<table><thead><tr>
<th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th><th>Asset Name</th>
<th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th></tr></thead><tbody>
<tr><td>4</td><td>08/20/2026</td><td>Joint</td><td>WMB</td>
<td>Williams Companies, Inc. (The) Common Stock Option Type: Call Strike price: $75.00 Expires: 2026-08-21</td>
<td>Stock Option</td><td>Purchase</td><td>$15,001 - $50,000</td><td>--</td></tr>
<tr><td>3</td><td>08/04/2026</td><td>Joint</td><td>--</td><td>Electronic Arts Inc. (EA)</td>
<td>Stock</td><td>Sale (Full)</td><td>$1,001 - $15,000</td><td>Sale due to corporate transaction</td></tr>
<tr><td>2</td><td>08/14/2026</td><td>Joint</td><td>--</td>
<td>AvalonBay Communities, Inc. Common Stock (AVB) (Exchanged) VMRK - Vivmark Residential Common Shares of Beneficial Interest (Received)</td>
<td>Stock</td><td>Exchange</td><td>$1,001 - $15,000</td><td>--</td></tr>
<tr><td>1</td><td>08/19/2026</td><td>Joint</td><td>--</td>
<td>City of Somewhere Municipal Bond 4.5%</td>
<td>Municipal Security</td><td>Purchase</td><td>$50,001 - $100,000</td><td>--</td></tr>
</tbody></table>"""

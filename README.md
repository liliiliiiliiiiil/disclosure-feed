# disclosure-feed

A scheduled job that reads U.S. statutory financial disclosures and pushes a
filtered digest to Telegram. Two independent feeds, sent as separate messages
because the underlying filings have very different reporting lag and precision.

| Feed | Source | Reporting lag | Amount precision |
|---|---|---|---|
| Corporate insiders | SEC EDGAR, Form 4 | 2 business days | Exact (shares x price) |
| U.S. House members | House Clerk, PTR | Up to 45 days | Bracketed range |

Runs on GitHub Actions Tuesday through Saturday. No third-party data vendors —
both sources are official government endpoints.

## Why filtering matters

A raw Form 4 stream is mostly noise. Most filings are equity grants (code A),
option exercises (code M), and tax withholding (code F), none of which reflect
a discretionary decision. Sorting whatever is left by dollar value surfaces
scheduled sales and mechanical purchases by large holders.

This job keeps open-market purchases (code P) and drops anything flagged as a
Rule 10b5-1 planned trade, then requires one of:

- the filer is a senior officer (CEO / CFO / COO / President / Chairman)
- at least three distinct filers bought the same issuer in the same window
  (cluster buy, marked with a fire emoji in the digest)
- the single transaction is large in absolute terms

Sales are reported under a much higher threshold, since insider selling is a
weak signal on its own.

Thresholds live at the top of `insider_feed.py`.

## Files

```
.github/workflows/insider_feed.yml   Schedule and secrets
insider_feed.py                      Entry point, Form 4 collection, Telegram
house_ptr.py                         House PTR collection (imported module)
```

`.state/seen.json` is created at runtime to suppress duplicate House filings
across runs. It is restored and saved by `actions/cache` and is not committed.

## Setup

Add three repository secrets under **Settings -> Secrets and variables ->
Actions**:

| Secret | Value |
|---|---|
| `SEC_UA` | `Your Name your@email.com` — SEC requires a real contact string in the User-Agent header and returns 403 without one |
| `TELEGRAM_TOKEN` | Bot token from [@BotFather](https://t.me/botfather) |
| `TELEGRAM_CHAT_ID` | Target chat ID |

Then trigger the workflow manually once (**Actions -> insider-feed -> Run
workflow**) to confirm both messages arrive. The first House run sends a full
seven-day backlog; later runs only send filings not already in the state cache.

Run locally with:

```bash
pip install requests pdfplumber
export SEC_UA="Your Name your@email.com"
export TELEGRAM_TOKEN=...
export TELEGRAM_CHAT_ID=...
python insider_feed.py
```

## How House PTRs are parsed

The Clerk publishes one ZIP per year containing an XML index of every
disclosure filed. Entries with `FilingType=P` are Periodic Transaction Reports;
each maps to a PDF at `public_disc/ptr-pdfs/{year}/{DocID}.pdf`.

The PDFs use a broken ToUnicode mapping for their label font, so extracted text
loses headings such as "Periodic Transaction Report". The transaction rows
themselves use a normal font and extract cleanly, so a single regex over the
extracted text is sufficient — no table reconstruction is needed. Older filings
mangle letter case, which is normalized on extraction.

Only assets with a parenthesized ticker are captured. Municipal bonds, treasury
notes, and unlisted funds are deliberately dropped.

## Known limitations

- **Senate filings are not included.** The Senate EFD search requires accepting
  an interstitial agreement and carrying a CSRF token, which needs a separate
  fetcher. The House is roughly four times the volume.
- **Scanned PTRs are not parsed.** Some members still file on paper. These are
  detected by the absence of a text layer and reported as a link only. OCR is
  intentionally not used: these forms are largely handwritten, and a
  misrecognized ticker is worse than a missing one.
- **Committee assignments are not cross-referenced.** The strongest signal in
  congressional trading is the overlap between a member's committee jurisdiction
  and the sector they traded. That mapping is not implemented yet.
- The 10b5-1 flag only exists on filings made after the December 2022 rule
  change. Older filings fall through as unflagged, which means "undetermined"
  rather than "not a planned trade".

## Data sources and attribution

- SEC EDGAR full-text filing archive — <https://www.sec.gov/edgar>
- Office of the Clerk, U.S. House of Representatives, Financial Disclosure
  Reports — <https://disclosures-clerk.house.gov>

Both are public domain U.S. government records. This project stores no data
beyond a list of already-seen filing identifiers.

House financial disclosure records carry statutory restrictions on use under
the Ethics in Government Act, including prohibitions on use for credit rating,
solicitation, and other unlawful purposes. This project is intended for personal
research and transparency only.

## Disclaimer

For personal research. Not investment advice, not a solicitation, and no
representation is made that the data is accurate, complete, or current.
Verify against the primary filing before acting on anything here.

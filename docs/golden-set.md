# Stage 1: the golden set

The golden set is the true amount, currency and date for a sample of receipts, confirmed by
hand from the physical receipt. It is the only way to measure whether the extractor is right.
The acceptance rule from the design is **zero wrong values marked ready to write**.

## Build it

1. Run `probe --save-raw tests/fixtures/raw` so originals and Paperless text are on disk.
2. Generate the blank file: `uv run paperless-field-extractor golden-template`. It writes
   `scratch/golden.csv` with one row per FY document and never overwrites existing work.
3. Fill `currency` (`AUD`, `USD` or `JPY`), `amount` and `date` from the receipt itself, not
   from Paperless or from the extractor's output. Leave a row blank to skip it.

| Column | Format |
| --- | --- |
| `doc_id` | the Paperless document id |
| `currency` | `AUD`, `USD` or `JPY` |
| `amount` | plain decimal, no symbol or thousands separator: `12.34`, and `1100` for JPY |
| `date` | ISO, `2026-10-25` |
| `notes` | anything; not scored |

Amounts are gross (GST-inclusive for AUD), as for `Transaction Amount`. AUD and USD have two
decimal places, JPY none. The loader rejects anything else with the line number.

## Choose the sample

The 13 FY documents are too few to trust alone, and they will not cover every layout. Aim for
at least 30 and make sure the sample has:

- each currency (AUD, USD, JPY) and each input type (text PDF, scanned PDF, photo, email);
- a thermal-paper photo, a multi-page PDF, a credit note or refund, and a receipt with no GST;
- receipts where the subtotal, tax and total are all printed, and some where only the total is.

Add receipts as more documents are tagged `tax deductible`.

## Score it

```sh
uv run paperless-field-extractor evaluate --raw-dir tests/fixtures/raw
```

`--source content` scores Paperless's own text, `--source ocr` scores this tool's OCR, and the
default `both` scores the reconciled result, which is what `apply` will write. The report counts
per field:

| Count | Meaning |
| --- | --- |
| ready + correct | would be written, and is right |
| review + correct | right, but sent to review |
| **WRONG ready** | would be written, and is wrong. Must be 0 |
| wrong review | wrong, but caught by review |
| missing | no value found |

The command exits 4 if any wrong value is marked ready.

## Sanitised fixtures

Real receipts stay out of git. To keep a regression case, copy its text into `tests/fixtures/`
with the supplier, amounts, dates, account numbers and any identifiers replaced, and write the
expected result next to it. Keep the *shape* that made it hard: the layout, the label wording,
the currency markers. Never commit an original, a photo, or `golden.csv`.

## When the rule fails

A wrong value at ready is a bug in the rules or in a corroboration check, not a threshold to
tune away. Fix the cause, add a sanitised fixture that reproduces it, and rerun.

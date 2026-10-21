# Stage 0 runbook: live checks

Read-only checks against the live Paperless instance, plus one write test on a throwaway
document. Nothing here changes a real receipt.

## What is already known (from the vault, 2026-10-21)

| Fact | Value |
| --- | --- |
| Paperless | 3.3.2, API v10 |
| Tag | `tax deductible` (with a space; `tax-deductible` matches nothing) |
| Custom fields | `Transaction Amount` #1 monetary, `Claimable %` #2 float, `Reference` #3 string, `Transaction Date` #4 date |
| FY25-26 deduction documents | 13 to 14, `Transaction Amount` filled on 2 to 3 |
| Tika and Gotenberg | active on the stack; the Tika image tag is **not recorded** |
| Permissions | per object type; a missing permission returns an empty list, not an error |

`probe` re-reads the field facts from the live instance, so the numbers above are checked
rather than trusted.

## 1. Probe (read-only)

Use a token whose user can view documents, tags and custom fields. The `paperless-tax-export`
view-only token works.

```sh
uv run paperless-field-extractor probe \
  --base-url https://paperless.example.com \
  --token-file secrets/paperless-token.txt
```

This writes `output/probe.json`: counts, ids and field metadata only. It is safe to share. It
reports field ids and types, the tag match and near-misses, coverage of the amount and date
fields, and the receipt mix by type.

Add the downloads when you are ready to build the golden set and measure barcodes. Originals
and Paperless text are saved under a git-ignored directory; the command refuses any other path.

```sh
uv run paperless-field-extractor probe \
  --base-url https://paperless.example.com \
  --token-file secrets/paperless-token.txt \
  --save-raw tests/fixtures/raw \
  --barcodes
```

Downloads need Tesseract and poppler installed (or run the command inside the container image).
With downloads on, PDFs split into `pdf-text-layer` and `pdf-scanned`.

### Reading the barcode result

`probe.json` has a `barcodes` block. Build barcode reading only if `any_amount_like` or
`any_date_like` is true. If receipts carry only a store or receipt id, drop barcodes: the
design note says that is the usual case, and this measures it on your receipts.

## 2. Tika image tag

Paperless does not report it. On Ganymede:

```sh
docker ps --format '{{.Names}}  {{.Image}}'
```

Look for the Tika container. Only an image tag ending in `-full` includes Tesseract, and
Paperless sends Tika only Office files and `.eml`, never PDFs or images. Record the tag in
the vault note.

## 3. `custom_fields` PATCH test (writes to one throwaway document)

The extractor must not write until this is known. A PATCH of `custom_fields` may replace the
whole list, which would drop existing values.

1. Upload or pick a throwaway document. Give it `Transaction Amount` = `AUD1.00` by hand.
2. Use a token with change permission on documents (not the view-only token).
3. Read, patch only the date, read again:

```sh
curl -s -H "Authorization: Token $TOKEN" -H "Accept: application/json; version=10" \
  "$BASE/api/documents/$ID/" | python -c "import sys,json; print(json.load(sys.stdin)['custom_fields'])"

curl -s -X PATCH -H "Authorization: Token $TOKEN" -H "Accept: application/json; version=10" \
  -H "Content-Type: application/json" \
  -d '{"custom_fields":[{"field":4,"value":"2026-10-25"}]}' "$BASE/api/documents/$ID/"
```

PowerShell:

```powershell
$h = @{ Authorization = "Token $env:TOKEN"; Accept = "application/json; version=10" }
(Invoke-RestMethod -Headers $h "$env:BASE/api/documents/$env:ID/").custom_fields
Invoke-RestMethod -Method Patch -Headers $h -ContentType "application/json" `
  -Body '{"custom_fields":[{"field":4,"value":"2026-10-25"}]}' "$env:BASE/api/documents/$env:ID/"
```

Record the result:

- Did field #1 (`AUD1.00`) survive the PATCH of field #4? If not, the writer must read the
  whole list, merge, and write it back.
- What value shape did Paperless accept for the monetary field (`AUD12.34`, `12.34`, a
  JPY value `JPY1100`)?
- Does a date field accept `2026-10-25`?

## 4. Currency spot check

AUD, USD and JPY are all in scope. In the 13 FY documents, note how many are USD and JPY, and
whether the JPY receipts are printed in Japanese. Japanese receipts need the `jpn` traineddata,
which the image and CI install. The Paperless `content` of a Japanese receipt may have been
OCRed with English only; if so the two text sources disagree and the document goes to review.

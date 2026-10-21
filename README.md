# paperless-field-extractor

Deterministic extraction of receipt amounts and dates into
[Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) custom fields.

Companion to [paperless-tax-export](https://github.com/albert118/paperless-tax-export),
which reads those custom fields to build tax CSVs. Receipts that arrive without the
fields filled in are what this tool targets. Currencies: AUD, USD and JPY.

## Status

Read-only tooling is built: a Paperless client, the Stage 0 probe, receipt text extraction
(email, PDF, photo), the rules that find a total and a date, and a golden-set scorer.
**Nothing writes to Paperless yet.** The `apply` stage waits on the `custom_fields` PATCH test
and on a golden set that scores zero wrong values at ready. See [docs](docs/).

## Commands

| Command | What it does |
| --- | --- |
| `probe` | Writes `output/probe.json`: field ids and types, receipt mix, field coverage. `--save-raw DIR` saves originals and Paperless text for the golden set, `--barcodes` scans them. |
| `ocr` | Prints the text of one original, from a file or a document id. |
| `extract` | Extracts the total and date from a text file, or compares own OCR with Paperless `content` for one document and reconciles them. |
| `golden-template` | Writes a blank `golden.csv` for the FY documents. |
| `evaluate` | Scores extraction against `golden.csv`. Exits 4 if a wrong value is marked ready. |

All of them are read-only toward Paperless.

## How it reads a receipt

- **Routed OCR.** The text source is chosen by input type, never by confidence:

  | Input | Text source |
  | --- | --- |
  | `.eml` original | stdlib `email` (headers, body, attachments) |
  | PDF with a text layer | `pdftotext -layout` |
  | Scanned PDF | rasterise at a fixed DPI, then Tesseract |
  | Photo | EXIF rotate, grayscale, fixed scale, then Tesseract |

- **Reproducible.** Tesseract's version, the `traineddata` hashes, `--psm`, the preprocessing
  and a single OpenMP thread are recorded in an OCR fingerprint. The OCR cache is keyed by the
  original's SHA-256 and the fingerprint, so a rerun returns identical text.
- **Strict amounts.** AUD and USD amounts have two decimals, JPY none. A format that could be
  read two ways (`1.234`, `1234,56`) is reported as ambiguous and never guessed. Money is
  `Decimal`, never `float`.
- **Currency from evidence.** An explicit code or symbol decides it. A bare `$` is AUD only with
  Australian context (GST, ABN) and USD only with US context (sales tax); a labelled total alone
  is not evidence. Otherwise the document goes to review.
- **Dates.** Day-first for AUD and JPY, year-first for Japanese formats (including 年月日 and
  令和). A numeric date that reads two ways on a USD receipt is ambiguous and not guessed. The
  document's `created` date is never used as the receipt date.
- **Corroboration.** A total is high confidence only with an independent check: it appears at
  least twice, tax matches the total (GST is one eleventh for AUD, 10/110 or 8/108 for JPY), or
  subtotal plus tax equals the total.
- **Two text sources.** Own OCR is compared with the Paperless `content` field. A value is
  ready to write only when both agree.

## Setup

`uv` is required. Python 3.14 is pinned in `.python-version`. Reading originals needs
Tesseract with the `eng` and `jpn` languages, and poppler (`pdftotext`, `pdftoppm`):

```sh
sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn poppler-utils
```

On Windows, run the container image instead; the same tools are in it. Without the tools,
unit tests still pass and the real-tool tests skip. CI installs the tools and fails if they
are missing.

## The Paperless token

The token is never committed and never pasted into a file that is tracked by git.

1. Create a Paperless user for this tool with view permission on documents, tags and custom
   fields. Writing will need change permission on documents; that is not needed yet.
2. Make sure that user can see the target documents and the tags. Permissions are per object
   type, and a missing permission returns an empty list, not an error.
3. Create the API token for that user and store it in a password manager.
4. Locally, write it to `secrets/paperless-token.txt` (ignored by git). In the container it is
   a mounted secret file.

## Development

The loop:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run paperless-field-extractor --help
docker build -t paperless-field-extractor .
```

CI runs the same checks on every pull request and on pushes to `main`. The container image is
built on pull requests and pushed to `ghcr.io/albert118/paperless-field-extractor` only on a
release.

Fixtures recorded from the live instance are sanitised before they are committed. Real
receipts, photos and their OCR text stay out of git, under `tests/fixtures/raw/` or
`scratch/`. The repository is public, so treat every commit as readable by anyone.

Docs: [Stage 0 runbook](docs/stage-0-runbook.md), [golden set](docs/golden-set.md).

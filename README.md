# paperless-field-extractor

Deterministic extraction of receipt amounts and dates into
[Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) custom fields.

Companion to [paperless-tax-export](https://github.com/albert118/paperless-tax-export),
which reads those custom fields to build tax CSVs. Receipts that arrive without the
fields filled in are what this tool targets.

## Status

Bootstrap only. The repository builds, lints, type-checks and tests, and the CLI answers
`--help` and `--version`. No OCR, rules or Paperless client exist yet.

## Planned behaviour

The design is settled but unbuilt.

- **Deterministic.** No LLM in this iteration. The same input and OCR configuration give
  the same output.
- **Routed OCR.** The text source is chosen by input type, never by confidence:

  | Input | Text source |
  | --- | --- |
  | `.eml` original | stdlib `email` (headers, body, attachments) |
  | PDF with a text layer | `pdftotext -layout` |
  | Scanned PDF | rasterise at a fixed DPI, then Tesseract |
  | Photo | EXIF rotate, grayscale, fixed scale, then Tesseract |

- **Two text sources compared.** The tool's own OCR is compared with the Paperless
  `content` field. A field is written only when both give the same value. Anything else
  goes to review.
- **Corroboration.** A value needs at least one independent check: the total appears at
  least twice, the GST line is within one cent of total/11, or line items sum to the
  subtotal.
- **Review.** Documents that fail a check land in a local `review.csv`, lowest confidence
  first. Filled-in values are written back with `apply --from-review`.
- **Write journal.** Every machine-written value is recorded in a local, append-only
  journal (document ID, field, before, after, source, OCR fingerprint). Nothing marks the
  value in Paperless, so the journal is the only audit trail.
- **Safe writes.** Only empty fields are written. A value a human entered is never
  overwritten.
- **Dry run.** `apply --dry-run` reports what would be written and writes nothing.

## The Paperless token

The token is never committed and never pasted into a file that is tracked by git.

1. Create a Paperless user for this tool. Unlike `paperless-tax-export`, it needs
   permission to change documents, because it writes custom fields. Grant only that.
2. Make sure that user can see the target documents. A document the user cannot see is
   silently missing from a run, with no error.
3. Create the API token for that user and store it in a password manager.
4. Locally, write it to `secrets/paperless-token.txt` (ignored by git). In the container
   it is a mounted secret file.

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

`uv` is required. Python 3.14 is pinned in `.python-version`.

CI runs the same checks on every pull request and on pushes to `main`. The container
image is built on pull requests and pushed to `ghcr.io/albert118/paperless-field-extractor`
only on a release.

Fixtures recorded from the live instance are sanitised before they are committed. Real
receipts, photos and their OCR text stay out of git, under `tests/fixtures/raw/` or
`scratch/`. The repository is public, so treat every commit as readable by anyone.

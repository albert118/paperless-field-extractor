# AGENTS.md

Operating notes for coding agents working in `paperless-field-extractor`.

This is a personal tool. Conventions from other repositories do not apply here.

## What this is

A Python helper that extracts receipt amounts and dates into Paperless-ngx custom fields,
so that `paperless-tax-export` finds them populated. It is deterministic: no LLM in this
iteration.

## Current state

Bootstrap only. The repository builds, lints, type-checks and tests. The CLI answers
`--help` and `--version`. There is no Paperless client, OCR router, rules engine, review
stage or write journal yet.

In place: `pyproject.toml` and `uv.lock` (uv, Python 3.14), `src/paperless_field_extractor/`,
`tests/`, `Dockerfile` and `.dockerignore`, the dotfiles, `.vscode/`, `.github/` (CI, image
build, labeler, dependabot) and the docs.

CI runs ruff, ruff format check, mypy and pytest on every pull request and on pushes to
`main`. `docker.yml` builds the image on pull requests and pushes to GHCR only on a
release. Do not disable or weaken those checks to get a change through.

## Design (settled, not built)

The design lives in the owner's vault note "Receipt extractor - OCR stage design". The
decisions that constrain code:

- **Route OCR by input type, never by confidence.** `.eml` via stdlib `email`; PDF with a
  text layer via `pdftotext -layout`; scanned PDF and photos via Tesseract with fixed
  preprocessing.
- **Two text sources.** Own OCR and the Paperless `content` field are both compared. Write
  a field only when both give the same value; otherwise send it to review.
- **Corroboration gates a write.** At least one of: total appears at least twice; GST line
  within one cent of total/11 (AU GST-inclusive); line items sum to the subtotal.
- **Review is a stage.** A local `review.csv` with a CSV round-trip (`apply --from-review`),
  never a Paperless tag.
- **Write journal.** Local, append-only: document ID, field, before, after, source, OCR
  fingerprint. There are no `auto-extracted` or `needs-review` tags.
- **`Claimable %` is never written.** `propose` is `apply --dry-run`.

## Rules that must not be broken

- **Empty-only writes.** Never overwrite a value a human entered. A `custom_fields` PATCH
  may replace the whole list on this Paperless version, so read, merge and write the full
  list, and test against a throwaway document first.
- **No secrets in git.** The API token comes from `secrets/` locally and from a mounted
  secret file in the container. Never commit a token, a real `.env`, or a URL that carries
  a token.
- **No receipt data in git.** This repository is public. `output/`, the OCR cache, the
  write journal and `tests/fixtures/raw/` are ignored. Fixtures are sanitised before they
  are committed: no real supplier names, amounts, account numbers, images or OCR text.
- **Deterministic.** Same input and OCR fingerprint give the same result. Pin the Tesseract
  version, `traineddata` hash and `--psm`, and set `OMP_THREAD_LIMIT=1`. Key the OCR cache
  by `sha256(original)` plus a config hash. No LLM, no cloud OCR, no guessing parsers
  (`dateparser`, `price-parser`). `Decimal` from strings only, never `float`. Dates
  compared as ISO strings, not datetimes.
- **Tests never touch the network.** Recorded Paperless JSON plus an injectable transport
  (`httpx.MockTransport` or equivalent).
- **Python 3.14**, `uv` with a committed `uv.lock`, src layout, stdlib-first. Ask before
  adding a dependency and say why in the commit or PR.

## Working style

- Prefer small commits with a clear scope. Do not reformat unrelated files.
- Add or update tests with every behaviour change.
- If a task depends on a live-instance fact that is not written down (custom field names
  and IDs, tag IDs, the Tika image tag, ports), ask or inspect. Do not invent a value.

## Summary

<!-- What changes and why, in a few lines. -->

## Changes

-

## Checks

- [ ] Tests added or updated for each behaviour change, including failure paths
- [ ] Tests use no network (injected transport)
- [ ] Writes to Paperless only fill empty fields, and each write is recorded in the journal
- [ ] No tokens, secrets or real receipt data (images, OCR text, amounts) in code, fixtures, logs or commit history
- [ ] Output stays deterministic: same input and OCR fingerprint give the same result, `Decimal` from strings, dates compared as ISO strings
- [ ] No new dependency, or the reason is given below
- [ ] No Python version change, or `.python-version`, `requires-python`, ruff target, Dockerfile and docs move together

## Dependencies and risks

<!-- New dependencies and why. Anything that can silently write a wrong value to Paperless. "None" if none. -->

## Notes for the reviewer

<!-- Optional: where to start, what to ignore, follow-ups. -->

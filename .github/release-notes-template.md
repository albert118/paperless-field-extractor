<!--
Usage: copy this file, replace every placeholder, then publish:

  gh release create vX.Y.Z --notes-file <edited-copy>.md --generate-notes

--notes-file text is placed first and the changelog from .github/release.yml
(grouped by PR label) is appended under "What's Changed". Add --prerelease for
release candidates (vX.Y.Z-rc.N). Delete this comment before publishing.
-->

## Highlights

-

## Upgrade notes

<!-- Changes to flags, review.csv columns, journal format, settings, secret file location, OCR configuration or Python version. "None" if none. -->

## Extraction behaviour

<!-- Changes to OCR routing, preprocessing, rules, corroboration checks or the disagreement policy. Same input and OCR fingerprint still give the same result unless stated here. -->

## Image

```
docker pull ghcr.io/albert118/paperless-field-extractor:X.Y.Z
docker run --rm ghcr.io/albert118/paperless-field-extractor:X.Y.Z --version
```

Tags published: `X.Y.Z`, `X.Y`, and `latest` for stable releases only.

## Known issues

<!-- Open problems that affect a run. "None known" if none. -->

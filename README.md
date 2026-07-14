# ascension-oneness-schumann-archive

Tomsk Schumann archive pipeline for Ascension Oneness.

## Archive model

- Raw snapshots are the source of truth.
- Processed snapshots are cleaned 3-day chart views derived from raw snapshots.
- The stitched timeline is derived from processed snapshots and can be rebuilt.
- Black inside the chart can be real quiet Schumann data. Do not remove dark chart regions just because they look empty.

## Paths

- Raw: `schumann/tomsk/raw/YYYY/MM/YYYY-MM-DD.webp`
- Processed: `schumann/tomsk/processed/YYYY/MM/YYYY-MM-DD.webp`
- Daily compatibility: `schumann/tomsk/daily/YYYY/MM/YYYY-MM-DD.webp`
- Stitched timeline: `schumann/tomsk/stitched/timeline.webp`
- Manifest: `schumann/tomsk/manifest.json`

The app should prefer `processed_url` when available and keep `daily_url` for backward compatibility.

## Daily capture

`scripts/archive_tomsk.py`:

1. Downloads the current Tomsk 3-day image.
2. Saves the full raw snapshot.
3. Creates a processed chart crop.
4. Trims only nearly-empty edge columns.
5. Extends `stitched/timeline.webp` with the non-overlapping new section.
6. Updates `manifest.json` with diagnostics and warnings.

## Backfill

`scripts/backfill_stitched_tomsk.py` rebuilds the stitched timeline from existing raw snapshots.

R2 backfill:

```bash
python scripts/backfill_stitched_tomsk.py --start-date 2026-07-05 --end-date 2026-07-14 --write-processed
```

Local dry run:

```bash
python scripts/backfill_stitched_tomsk.py --local-input-dir ./raw-samples --out-dir ./out
```

Local input filenames must start with `YYYY-MM-DD`, for example:

```text
2026-07-05.webp
2026-07-06.webp
2026-07-07.webp
```

Debug output includes:

- crop box
- left/right trimmed pixels
- black ratio
- overlap pixels
- append width
- overlap percentage
- raw match score
- expected-overlap penalty
- final match score
- resulting timeline width
- match confidence

## Manual GitHub backfill

Use the `Backfill Tomsk stitched timeline` workflow from GitHub Actions.

By default it does not overwrite existing processed/daily images. Enable `write_processed` only when the processing logic has intentionally changed and derived images should be rebuilt.

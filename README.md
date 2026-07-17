# Collection of tools

Each standalone script lives in its own subfolder with a dedicated README.
Run commands from this repository root so paths and the shared Python
environment are consistent.

Tools that process image content use the shared dependencies:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## FinPrintv2 identification dataset preparation

The standalone tool in [`prepare_finprint_dataset/`](prepare_finprint_dataset/)
creates a balanced, crop-only FinPrintv2 identification dataset while keeping
camera bursts together in train, validation, or test. Its own
[`README.md`](prepare_finprint_dataset/README.md) documents all filtering,
copy/link, offspring-ID, reporting, and dry-run options.

## GPS Screen Reader

[`gps_screen_reader/`](gps_screen_reader/) finds photographed GPS screens with
Tesseract OCR and writes grouped coordinates to a tab-separated result. Its
[`README.md`](gps_screen_reader/README.md) covers setup, supported images,
filters, retained conversions, output columns, and the bundled sample.

## NKW filename flipper

[`flip_nkw_filenames/`](flip_nkw_filenames/) moves trailing NKW identifiers to
the front of JPEG and RAW filenames without rewriting image bytes. Its
[`README.md`](flip_nkw_filenames/README.md) documents dry runs, multi-ID
handling, collision checks, integrity verification, and the bundled sample.

## In-place JPEG normalizer

[`normalize_images_to_jpeg/`](normalize_images_to_jpeg/) fully decodes images,
repairs recoverable JPEGs, and converts other readable formats to validated JPEG
in place. Its [`README.md`](normalize_images_to_jpeg/README.md) explains the
destructive workflow, dry run, RAW support, conflict protection, parallelism,
metadata handling, and summary output.

## Recover files missed by JPEG preprocessing

[`copy_unprocessed_images/`](copy_unprocessed_images/) finds files missing from
an existing JPEG preprocessing output and recovers them as validated JPEGs
without overwriting prior results. Its
[`README.md`](copy_unprocessed_images/README.md) documents dry runs, directory
safety, actual-content decoding, RAW fallback, output naming, and validation.

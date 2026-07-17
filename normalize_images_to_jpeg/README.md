# In-place JPEG Normalizer

`normalize_images_to_jpeg.py` recursively inspects images by their actual
content, fully decodes JPEG data, repairs recoverable JPEGs, and converts other
readable image or camera RAW formats to standard JPEG.

This tool changes files in place. Run `--dry-run` first and use a tested backup
for important source data.

## Setup

From the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Pillow handles ordinary image formats. `rawpy` provides camera RAW support.

## Usage

Preview:

```sh
.venv/bin/python normalize_images_to_jpeg/normalize_images_to_jpeg.py \
  /path/to/photos \
  --dry-run
```

Normalize:

```sh
.venv/bin/python normalize_images_to_jpeg/normalize_images_to_jpeg.py \
  /path/to/photos
```

## Options

- `--quality 1..95`: JPEG output quality; default `75`.
- `--workers N`: parallel inspection and conversion workers; default `8`,
  maximum `64`.
- `--dry-run`: fully inspect inputs and report planned changes without writing
  or deleting files.
- `--no-progress`: suppress progress bars.
- `--verbose`: print every file selected for normalization.
- `-h`, `--help`: show the CLI reference.

## Normalization behavior

- Standard JPEGs that fully decode are left byte-for-byte unchanged.
- Recoverable damaged or truncated JPEG streams are rewritten atomically as
  clean JPEGs.
- MPO containers, including MPO data named `.jpg`, are reduced to frame zero
  and rewritten as standard single-frame JPEGs.
- Other readable image formats are converted based on their actual content,
  not their extension.
- Camera RAW formats are decoded with `rawpy`, with an embedded-preview
  fallback when full decoding is unsupported.
- Non-JPEG extensions are replaced with `.jpg`.
- JPEG-like `.jpg` and `.jpeg` filenames are retained even when their original
  content was another format.
- EXIF orientation is applied, usable EXIF data and source timestamps are
  retained where possible, and output dimensions and JPEG format are validated.
- Symlinks and non-image files are ignored.

The tool refuses a conversion if its destination already exists or if multiple
inputs would produce the same destination. A converted source is removed only
after its new JPEG has been written atomically and validated.

## Final summary

The command reports:

- files normalized or that would be normalized;
- damaged JPEG candidates or repaired JPEGs;
- worker count;
- fully decoded standard JPEGs left unchanged;
- ignored non-image files; and
- failures with paths and reasons.

A run with failures exits nonzero after processing the remaining eligible
files.

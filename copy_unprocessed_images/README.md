# Recover Images Missed by JPEG Preprocessing

`copy_unprocessed_images.py` compares an original source tree with an existing
JPEG preprocessing output and recovers source files that do not yet have a
corresponding output JPEG.

It decodes files by actual content rather than trusting extensions, writes a
validated JPEG under the same relative directory structure, and never
overwrites existing output.

## Setup

From the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Pillow handles ordinary images and `rawpy` handles camera RAW files.

## Usage

Preview missing files:

```sh
.venv/bin/python copy_unprocessed_images/copy_unprocessed_images.py \
  /path/to/originals \
  /path/to/jpeg-output \
  --dry-run
```

Recover them:

```sh
.venv/bin/python copy_unprocessed_images/copy_unprocessed_images.py \
  /path/to/originals \
  /path/to/jpeg-output
```

## Options

- `--quality 1..95`: JPEG output quality; default `75`.
- `--dry-run`: report missing files without creating output.
- `-h`, `--help`: show the CLI reference.

## Recovery behavior

- Source and output must be separate, non-overlapping directory trees.
- Existing output files are counted as already processed and never overwritten.
- Symlinks are ignored.
- `.jpg` and `.jpeg` source names retain their relative filename.
- Other source extensions become `.jpg`.
- TIFF, MPO, or other readable content disguised by a JPEG-like extension is
  decoded by content and written back as a real JPEG.
- Recoverable truncated images are decoded in relaxed mode.
- Camera RAW files use `rawpy`, with an embedded-preview fallback when needed.
- EXIF orientation is applied, transparency is flattened onto white, and source
  timestamps are copied where possible.
- Each output is validated for JPEG format and expected dimensions.
- Failed writes are removed so partial output is not left behind.

## Final summary

The command reports recovered files, files that were already preprocessed,
ignored symlinks, and failures. Failed paths and reasons are printed after all
candidates have been attempted.

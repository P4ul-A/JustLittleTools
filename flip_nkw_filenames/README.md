# NKW Filename Flipper

`flip_nkw_filenames.py` recursively moves trailing NKW identifiers to the front
of JPEG and camera RAW filenames without decoding or rewriting image data.

For example:

```text
Photo_15_NKW-460.jpg -> NKW-460_Photo_15.jpg
```

## Usage

Always preview first:

```sh
python3 flip_nkw_filenames/flip_nkw_filenames.py \
  /path/to/photos \
  --dry-run
```

Apply the reported renames:

```sh
python3 flip_nkw_filenames/flip_nkw_filenames.py /path/to/photos
```

This tool uses only the Python standard library. The bundled
[`sample/`](sample/) shows the expected final naming style.

## Options

- `--dry-run`: validate and print every proposed rename without changing any
  filename.
- `-h`, `--help`: show the CLI reference.

## Naming behavior

- Only JPEG and common camera RAW extensions are considered.
- Directory names, symlinks, and unsupported files are ignored.
- One or more consecutive trailing `NKW-<digits>` tokens are moved to the
  front.
- Existing leading NKW identifiers are kept at the front.
- Repeated occurrences of the same identifier are reduced to one.
- Identifier order is preserved.
- A file containing only identifiers and no remaining name is ignored.

Before changing anything, the tool rejects runs where two files would receive
the same destination or a destination already exists.

## Data integrity

Every source file is SHA-256 hashed before the rename and hashed again
afterward. If the bytes differ, the tool restores the original filename and
reports an integrity error.

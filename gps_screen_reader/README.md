# GPS Screen Reader

`gps_screen_reader.py` recursively scans photographs for GPS-coordinate screens,
uses Tesseract OCR to read latitude and longitude, and writes one tab-separated
result row per folder and position.

The source images are never modified. JPEG, PNG, TIFF, and Nikon NEF files are
supported. Non-JPEG inputs are converted temporarily for OCR unless
`--keep-jpegs` is used.

## Setup

From the repository root, create the shared Python 3.12 environment and install
Tesseract:

```sh
brew install python@3.12 tesseract
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Usage

Scan a directory and write `gps_positions.txt` inside it:

```sh
.venv/bin/python gps_screen_reader/gps_screen_reader.py /path/to/photos
```

Omitting the directory opens the system folder picker when Tkinter is
available:

```sh
.venv/bin/python gps_screen_reader/gps_screen_reader.py
```

The bundled [`sample/`](sample/) contains two NEF inputs and an example result.

## Options

- `-o PATH`, `--output PATH`: choose the output file instead of
  `DIRECTORY/gps_positions.txt`.
- `--keep-jpegs DIRECTORY`: retain converted JPEGs for review. Without this
  option, conversion and OCR images are temporary.
- `--tesseract PATH`: use a specific Tesseract executable.
- `--filename-pattern REGEX`, `--name-pattern REGEX`: process only image
  basenames matching a case-insensitive regular expression.
- `-h`, `--help`: show the CLI reference.

Examples:

```sh
# Write to a custom result file
.venv/bin/python gps_screen_reader/gps_screen_reader.py \
  /path/to/photos \
  --output /path/to/positions.txt

# Keep converted JPEGs
.venv/bin/python gps_screen_reader/gps_screen_reader.py \
  /path/to/photos \
  --keep-jpegs /path/to/converted_gps_jpegs

# Only inspect filenames containing GPS or SCREEN
.venv/bin/python gps_screen_reader/gps_screen_reader.py \
  /path/to/photos \
  --filename-pattern 'GPS|SCREEN'
```

## Output

The output is tab-separated with these columns:

1. absolute source folder;
2. coordinates in degrees and decimal minutes as shown on the GPS;
3. decimal latitude;
4. decimal longitude; and
5. matching source image paths relative to the scanned root.

Repeated photographs of the same coordinate in one folder are grouped into one
row. Unreadable images are reported as warnings, while readable images without
a coordinate are simply omitted.

## OCR behavior

Each image is orientation-corrected and converted to RGB. The tool gives
Tesseract normal and high-contrast variants and accepts common OCR substitutions
such as `O` for zero and `I` for one. Coordinates outside valid latitude,
longitude, or minute ranges are rejected.

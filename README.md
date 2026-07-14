# Collection of tools 
## GPS Screen Reader

This tool recursively crawls a directory, converts Nikon `.NEF` files to JPEG,
uses Tesseract OCR to identify photographed GPS screens, and writes the position
for each folder to `gps_positions.txt`. Common JPEG, PNG, and TIFF images are
also supported.

The source images are never modified. Converted JPEGs are temporary unless
`--keep-jpegs` is used; OCR-only intermediate images always remain temporary.

### macOS setup

Install Python 3.12 and Tesseract once:

```sh
brew install python@3.12 tesseract
```

Then double-click `LaunchGPSScreenReader.command` and choose the directory to
scan. The result is saved inside the selected directory as `gps_positions.txt`.

### Command-line use

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python gps_screen_reader.py /path/to/photos
```

Useful options:

```sh
# Choose a different result path
.venv/bin/python gps_screen_reader.py /path/to/photos -o positions.txt

# Keep the converted JPEGs for review
.venv/bin/python gps_screen_reader.py /path/to/photos --keep-jpegs converted_gps_jpegs
```

The tab-separated output contains the absolute folder, the position as shown on
the screen, decimal latitude/longitude, and the source image name(s). Repeated
photos of the same position in the same folder are grouped into one row.

## NKW filename flipper

`flip_nkw_filenames.py` recursively moves a trailing NKW identifier to the front
of JPEG and RAW filenames. For example, `Photo_15_NKW-460.jpg` becomes
`NKW-460_Photo_15.jpg`. Directories and unsupported files are not renamed. The
operation only renames each file and verifies its SHA-256 checksum before and
afterward; image data is never decoded or rewritten.

Preview the changes first, then apply them:

```sh
python3 flip_nkw_filenames.py Sample_Flipped --dry-run
python3 flip_nkw_filenames.py Sample_Flipped
```

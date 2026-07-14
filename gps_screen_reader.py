#!/usr/bin/env python3
"""Find photographed GPS screens and write their positions to a text file."""

from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, ImageOps
except ImportError as exc:  # pragma: no cover - exercised by a fresh installation
    raise SystemExit(
        "Pillow is not installed. Run: python3.12 -m pip install -r requirements.txt"
    ) from exc


RAW_EXTENSIONS = {".nef"}
IMAGE_EXTENSIONS = RAW_EXTENSIONS | {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
OCR_MAX_SIZE = 3000

# DMM coordinates as shown by the GPS in Sample_GPS. Degree and minute marks are
# optional because OCR sometimes drops or substitutes them.
LATITUDE_RE = re.compile(
    r"\b([NS])\s*([0-9OIl]{1,2})\s*[°ºo]?\s*"
    r"([0-9OIl]{1,2}[.,][0-9OIl]{2,5})",
    re.IGNORECASE,
)
LONGITUDE_RE = re.compile(
    r"\b([EW])\s*([0-9OIl]{1,3})\s*[°ºo]?\s*"
    r"([0-9OIl]{1,2}[.,][0-9OIl]{2,5})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Coordinate:
    latitude_hemisphere: str
    latitude_degrees: int
    latitude_minutes: float
    longitude_hemisphere: str
    longitude_degrees: int
    longitude_minutes: float

    @property
    def latitude_decimal(self) -> float:
        value = self.latitude_degrees + self.latitude_minutes / 60.0
        return -value if self.latitude_hemisphere == "S" else value

    @property
    def longitude_decimal(self) -> float:
        value = self.longitude_degrees + self.longitude_minutes / 60.0
        return -value if self.longitude_hemisphere == "W" else value

    @property
    def display(self) -> str:
        return (
            f"{self.latitude_hemisphere} {self.latitude_degrees}°"
            f"{self.latitude_minutes:06.3f}' / "
            f"{self.longitude_hemisphere} {self.longitude_degrees}°"
            f"{self.longitude_minutes:06.3f}'"
        )

    @property
    def grouping_key(self) -> tuple[str, int, float, str, int, float]:
        return (
            self.latitude_hemisphere,
            self.latitude_degrees,
            round(self.latitude_minutes, 5),
            self.longitude_hemisphere,
            self.longitude_degrees,
            round(self.longitude_minutes, 5),
        )


def _normalise_number(value: str) -> str:
    return value.translate(str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"})).replace(
        ",", "."
    )


def parse_coordinate(text: str) -> Coordinate | None:
    """Extract and validate one latitude/longitude pair from OCR text."""
    latitude = LATITUDE_RE.search(text)
    longitude = LONGITUDE_RE.search(text)
    if not latitude or not longitude:
        return None

    lat_degrees = int(_normalise_number(latitude.group(2)))
    lat_minutes = float(_normalise_number(latitude.group(3)))
    lon_degrees = int(_normalise_number(longitude.group(2)))
    lon_minutes = float(_normalise_number(longitude.group(3)))

    if not (0 <= lat_degrees <= 90 and 0 <= lon_degrees <= 180):
        return None
    if lat_degrees == 90 and lat_minutes != 0:
        return None
    if lon_degrees == 180 and lon_minutes != 0:
        return None
    if not (0 <= lat_minutes < 60 and 0 <= lon_minutes < 60):
        return None

    return Coordinate(
        latitude.group(1).upper(),
        lat_degrees,
        lat_minutes,
        longitude.group(1).upper(),
        lon_degrees,
        lon_minutes,
    )


def discover_images(
    root: Path,
    excluded_directory: Path | None = None,
    filename_pattern: re.Pattern[str] | None = None,
) -> list[Path]:
    root = root.resolve()
    excluded_directory = excluded_directory.resolve() if excluded_directory else None
    images: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if filename_pattern is not None and filename_pattern.search(path.name) is None:
            continue
        if excluded_directory and excluded_directory in path.resolve().parents:
            continue
        images.append(path)
    return sorted(images, key=lambda item: str(item).lower())


def _load_raw_image(source: Path) -> Image.Image:
    try:
        import rawpy
    except ImportError as exc:
        raise RuntimeError(
            "rawpy is required for NEF files. Install requirements.txt with Python 3.12."
        ) from exc

    decode_error: Exception | None = None
    try:
        with rawpy.imread(str(source)) as raw:
            pixels = raw.postprocess(use_camera_wb=True, output_bps=8)
        return Image.fromarray(pixels)
    except Exception as exc:  # Some Nikon bodies require the embedded-preview fallback.
        decode_error = exc

    try:
        with rawpy.imread(str(source)) as raw:
            thumbnail = raw.extract_thumb()
        if thumbnail.format == rawpy.ThumbFormat.JPEG:
            with Image.open(io.BytesIO(thumbnail.data)) as image:
                return image.convert("RGB")
        return Image.fromarray(thumbnail.data).convert("RGB")
    except Exception as preview_error:
        raise RuntimeError(
            f"RAW conversion failed ({decode_error}); embedded preview failed ({preview_error})"
        ) from preview_error


def convert_to_jpeg(source: Path, destination: Path) -> None:
    """Convert a supported source image to a full-frame RGB JPEG."""
    image: Image.Image | None = None
    try:
        if source.suffix.lower() in RAW_EXTENSIONS:
            image = _load_raw_image(source)
        else:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
        if image.mode != "RGB":
            converted = image.convert("RGB")
            image.close()
            image = converted
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, format="JPEG", quality=92)
    finally:
        if image is not None:
            image.close()


def make_ocr_variants(jpeg_path: Path, work_directory: Path) -> list[Path]:
    """Create smaller normal and high-contrast images for Tesseract."""
    normal_path = work_directory / f"{jpeg_path.stem}_ocr.jpg"
    threshold_path = work_directory / f"{jpeg_path.stem}_ocr_bw.png"

    with Image.open(jpeg_path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    try:
        image.thumbnail((OCR_MAX_SIZE, OCR_MAX_SIZE), Image.Resampling.LANCZOS)
        image.save(normal_path, format="JPEG", quality=95)
        grayscale = ImageOps.autocontrast(ImageOps.grayscale(image))
        threshold = grayscale.point(lambda pixel: 255 if pixel > 140 else 0)
        threshold.save(threshold_path, format="PNG")
        grayscale.close()
        threshold.close()
    finally:
        image.close()
    return [normal_path, threshold_path]


def run_tesseract(image_path: Path, page_segmentation_mode: int, executable: str) -> str:
    result = subprocess.run(
        [executable, str(image_path.resolve()), "stdout", "-l", "eng", "--psm", str(page_segmentation_mode)],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"Tesseract failed: {detail}")
    return result.stdout


def read_position(jpeg_path: Path, work_directory: Path, tesseract: str) -> tuple[Coordinate | None, str]:
    variants = make_ocr_variants(jpeg_path, work_directory)
    combined_text: list[str] = []
    attempts = ((variants[0], 11), (variants[0], 3), (variants[1], 11), (variants[1], 3))
    for image_path, mode in attempts:
        text = run_tesseract(image_path, mode, tesseract)
        combined_text.append(text)
        coordinate = parse_coordinate(text)
        if coordinate:
            return coordinate, text
    return None, "\n".join(combined_text)


def write_results(
    output_path: Path,
    root: Path,
    matches: Iterable[tuple[Path, Coordinate]],
) -> int:
    grouped: dict[tuple[Path, tuple[str, int, float, str, int, float]], list[Path]] = defaultdict(list)
    coordinates: dict[tuple[Path, tuple[str, int, float, str, int, float]], Coordinate] = {}
    for source, coordinate in matches:
        key = (source.parent.resolve(), coordinate.grouping_key)
        grouped[key].append(source)
        coordinates[key] = coordinate

    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["folder\tposition\tlatitude_decimal\tlongitude_decimal\tsource_images"]
    for key in sorted(grouped, key=lambda item: (str(item[0]).lower(), item[1])):
        folder, _ = key
        coordinate = coordinates[key]
        sources = "; ".join(str(path.resolve().relative_to(root)) for path in grouped[key])
        lines.append(
            f"{folder}\t{coordinate.display}\t{coordinate.latitude_decimal:.7f}\t"
            f"{coordinate.longitude_decimal:.7f}\t{sources}"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(grouped)


def select_directory() -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None
    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askdirectory(title="Choose a directory containing GPS screen photos")
    finally:
        root.destroy()
    return Path(selected) if selected else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recursively find photographed GPS screens and save their coordinates."
    )
    parser.add_argument("directory", nargs="?", type=Path, help="directory to crawl (picker opens if omitted)")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output text file (default: DIRECTORY/gps_positions.txt)",
    )
    parser.add_argument(
        "--keep-jpegs",
        type=Path,
        metavar="DIRECTORY",
        help="keep converted JPEGs here instead of using temporary files",
    )
    parser.add_argument(
        "--tesseract",
        default="tesseract",
        help="Tesseract executable or path (default: tesseract)",
    )
    parser.add_argument(
        "--filename-pattern",
        "--name-pattern",
        metavar="REGEX",
        help="only process image basenames matching this regular expression (case-insensitive)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        filename_pattern = (
            re.compile(args.filename_pattern, re.IGNORECASE) if args.filename_pattern else None
        )
    except re.error as exc:
        parser.error(f"invalid filename pattern: {exc}")
    root = args.directory or select_directory()
    if root is None:
        print("No directory selected.", file=sys.stderr)
        return 2
    root = root.expanduser().resolve()
    if not root.is_dir():
        print(f"Directory does not exist: {root}", file=sys.stderr)
        return 2

    tesseract = shutil.which(args.tesseract) if Path(args.tesseract).name == args.tesseract else args.tesseract
    if not tesseract or not Path(tesseract).is_file():
        print("Tesseract OCR was not found. On macOS, install it with: brew install tesseract", file=sys.stderr)
        return 2

    output_path = (args.output or root / "gps_positions.txt").expanduser().resolve()
    keep_directory = args.keep_jpegs.expanduser().resolve() if args.keep_jpegs else None
    images = discover_images(root, keep_directory, filename_pattern)
    print(f"Scanning {len(images)} supported image(s) under {root}")

    temporary = tempfile.TemporaryDirectory(prefix="gps_screen_reader_")
    work_directory = Path(temporary.name).resolve()
    if keep_directory:
        conversion_root = keep_directory
        conversion_root.mkdir(parents=True, exist_ok=True)
    else:
        conversion_root = work_directory

    matches: list[tuple[Path, Coordinate]] = []
    failures: list[tuple[Path, str]] = []
    try:
        for index, source in enumerate(images, start=1):
            relative = source.resolve().relative_to(root)
            print(f"[{index}/{len(images)}] {relative}", flush=True)
            safe_name = f"{index:06d}_{source.stem}.jpg"
            jpeg_path = conversion_root / safe_name
            try:
                convert_to_jpeg(source, jpeg_path)
                coordinate, _ = read_position(jpeg_path, work_directory, str(tesseract))
                if coordinate:
                    matches.append((source, coordinate))
                    print(f"  GPS screen: {coordinate.display}")
            except Exception as exc:
                failures.append((source, str(exc)))
                print(f"  Warning: {exc}", file=sys.stderr)
    finally:
        temporary.cleanup()

    row_count = write_results(output_path, root, matches)
    print(f"Wrote {row_count} folder/position row(s) to {output_path}")
    if failures:
        print(f"Completed with {len(failures)} unreadable image(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

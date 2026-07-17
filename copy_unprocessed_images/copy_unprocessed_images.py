#!/usr/bin/env python3
"""Recover files missed by preprocessing and write valid JPEG output."""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFile, ImageOps

try:
    import rawpy
except ImportError:  # rawpy is only needed when a camera RAW file is found.
    rawpy = None


JPEG_EXTENSIONS = {".jpg", ".jpeg"}
DEFAULT_JPEG_QUALITY = 75
RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".crw",
    ".dcr",
    ".dng",
    ".erf",
    ".fff",
    ".iiq",
    ".k25",
    ".kdc",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".raf",
    ".raw",
    ".rw2",
    ".rwl",
    ".sr2",
    ".srf",
    ".srw",
    ".x3f",
}


@dataclass(frozen=True)
class RecoveryCandidate:
    source: Path
    destination: Path


@dataclass(frozen=True)
class ScanResult:
    candidates: list[RecoveryCandidate]
    processed_count: int
    symlink_count: int


def resolved_directory(path: Path, label: str, *, create: bool = False) -> Path:
    path = path.expanduser().resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise NotADirectoryError(f"{label} directory does not exist: {path}")
    return path


def ensure_separate_trees(source: Path, output: Path) -> None:
    """Refuse recursive scans where either directory contains the other."""
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("source and output directories must not overlap")


def jpeg_destination(source_file: Path, source: Path, output: Path) -> Path:
    """Keep JPEG-like names and give every other source a .jpg suffix."""
    destination = output / source_file.relative_to(source)
    if source_file.suffix.lower() not in JPEG_EXTENSIONS:
        destination = destination.with_suffix(".jpg")
    return destination


def scan_missing_files(source: Path, output: Path) -> ScanResult:
    """Find source files without a corresponding JPEG output."""
    candidates: list[RecoveryCandidate] = []
    processed_count = 0
    symlink_count = 0

    files = sorted(source.rglob("*"), key=lambda path: str(path).lower())
    for source_file in files:
        if source_file.is_symlink():
            symlink_count += 1
            continue
        if not source_file.is_file():
            continue

        destination = jpeg_destination(source_file, source, output)
        if destination.is_file():
            processed_count += 1
            continue
        candidates.append(RecoveryCandidate(source_file, destination))

    return ScanResult(
        candidates=candidates,
        processed_count=processed_count,
        symlink_count=symlink_count,
    )


def open_raw_image(source: Path) -> Image.Image:
    if rawpy is None:
        raise RuntimeError(
            "RAW image support requires rawpy. Install requirements.txt with Python 3.12."
        )

    try:
        with rawpy.imread(str(source)) as raw:
            pixels = raw.postprocess()
    except rawpy.LibRawFileUnsupportedError:
        with rawpy.imread(str(source)) as raw:
            thumbnail = raw.extract_thumb()
        if thumbnail.format == rawpy.ThumbFormat.JPEG:
            with Image.open(BytesIO(thumbnail.data)) as preview:
                preview.load()
                return preview.copy()
        if thumbnail.format == rawpy.ThumbFormat.BITMAP:
            image = Image.fromarray(thumbnail.data)
            image.load()
            return image
        raise RuntimeError("RAW file has no supported embedded preview")

    image = Image.fromarray(pixels)
    image.load()
    return image


def open_image_relaxed(source: Path) -> Image.Image:
    """Decode by actual content, tolerating incomplete image streams."""
    if source.suffix.lower() in RAW_EXTENSIONS:
        return open_raw_image(source)

    previous_setting = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with Image.open(source) as opened:
            if opened.format == "MPO":
                opened.seek(0)
            opened.load()
            return opened.copy()
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous_setting


def prepare_for_jpeg(image: Image.Image) -> Image.Image:
    oriented = ImageOps.exif_transpose(image)
    try:
        if oriented.mode in ("RGBA", "LA") or (
            oriented.mode == "P" and oriented.info.get("transparency") is not None
        ):
            background = Image.new("RGB", oriented.size, "white")
            rgba = oriented.convert("RGBA")
            try:
                background.paste(rgba, mask=rgba.getchannel("A"))
            finally:
                rgba.close()
            return background
        return oriented.convert("RGB")
    finally:
        if oriented is not image:
            oriented.close()


def validate_jpeg(path: Path, expected_size: tuple[int, int]) -> None:
    with Image.open(path) as image:
        if image.format != "JPEG":
            raise ValueError(f"output is {image.format or 'unknown'}, not JPEG")
        if image.size != expected_size:
            raise ValueError(f"output dimensions {image.size} do not match {expected_size}")
        image.load()


def recover_as_jpeg(candidate: RecoveryCandidate, quality: int) -> None:
    """Decode one source and exclusively create a validated JPEG destination."""
    candidate.destination.parent.mkdir(parents=True, exist_ok=True)
    if candidate.destination.exists():
        raise FileExistsError(f"destination already exists: {candidate.destination}")

    image: Image.Image | None = None
    jpeg_image: Image.Image | None = None
    destination_created = False
    try:
        image = open_image_relaxed(candidate.source)
        jpeg_image = prepare_for_jpeg(image)
        with candidate.destination.open("xb") as destination_handle:
            destination_created = True
            jpeg_image.save(
                destination_handle,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
        validate_jpeg(candidate.destination, jpeg_image.size)
        try:
            shutil.copystat(candidate.source, candidate.destination)
        except OSError:
            pass
    except Exception:
        if destination_created:
            candidate.destination.unlink(missing_ok=True)
        raise
    finally:
        if jpeg_image is not None:
            jpeg_image.close()
        if image is not None:
            image.close()


def validate_quality(value: int) -> int:
    if not 1 <= value <= 95:
        raise ValueError("JPEG quality must be between 1 and 95")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recover files missed by JPEG preprocessing. Actual content is decoded "
            "without trusting the filename, and every successful output is JPEG."
        )
    )
    parser.add_argument("source", type=Path, help="original source directory")
    parser.add_argument("output", type=Path, help="JPEG preprocessing output directory")
    parser.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_JPEG_QUALITY,
        help=f"JPEG quality from 1 to 95 (default: {DEFAULT_JPEG_QUALITY})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be recovered without writing files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        quality = validate_quality(args.quality)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        source = resolved_directory(args.source, "Source")
        output = resolved_directory(args.output, "Output", create=not args.dry_run)
        ensure_separate_trees(source, output)
        scan = scan_missing_files(source, output)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"Source: {source}")
    print(f"Output: {output}")

    recovered_count = 0
    failures: list[tuple[Path, str]] = []
    total = len(scan.candidates)
    for index, candidate in enumerate(scan.candidates, start=1):
        relative_source = candidate.source.relative_to(source)
        relative_destination = candidate.destination.relative_to(output)
        description = str(relative_source)
        if relative_destination != relative_source:
            description += f" -> {relative_destination}"

        if args.dry_run:
            print(f"[{index}/{total}] Would recover: {description}")
            continue

        print(f"[{index}/{total}] Recovering: {description}")
        try:
            recover_as_jpeg(candidate, quality)
            recovered_count += 1
        except Exception as exc:
            failures.append((relative_source, str(exc)))

    if args.dry_run:
        print(f"Would attempt recovery: {total}")
    else:
        print(f"Recovered as JPEG: {recovered_count}")
    print(f"Already preprocessed: {scan.processed_count}")
    print(f"Ignored symlinks: {scan.symlink_count}")
    print(f"Failed: {len(failures)}")

    if failures:
        print("Failures:", file=sys.stderr)
        for relative, message in failures:
            print(f"- {relative}: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

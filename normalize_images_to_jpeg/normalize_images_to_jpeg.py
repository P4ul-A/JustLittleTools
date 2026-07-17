#!/usr/bin/env python3
"""Recursively normalize images and repair recoverable JPEGs in place."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

try:
    import rawpy
except ImportError:  # rawpy is only needed when a camera RAW file is found.
    rawpy = None


JPEG_EXTENSIONS = {".jpg", ".jpeg"}
JPEG_ORIENTATION_TAG = 274
DEFAULT_JPEG_QUALITY = 75
DEFAULT_WORKERS = 8
MAX_WORKERS = 64
WORK_QUEUE_MULTIPLIER = 2
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
class Conversion:
    source: Path
    destination: Path
    source_format: str


@dataclass(frozen=True)
class ScanResult:
    conversions: list[Conversion]
    jpeg_count: int
    damaged_jpeg_count: int
    ignored_count: int
    inspection_failures: list[tuple[Path, str]]


@dataclass(frozen=True)
class Inspection:
    source: Path
    outcome: str
    source_format: str = ""
    message: str = ""


class ProgressBar:
    """Compact terminal progress bar with elapsed time and ETA."""

    def __init__(self, label: str, total: int, *, enabled: bool = True):
        self.label = label
        self.total = total
        self.enabled = enabled
        self.current = 0
        self.started = time.monotonic()
        self.last_update = 0.0
        self.last_milestone = -1
        self.is_terminal = sys.stderr.isatty()
        if enabled:
            self._emit(final=total == 0)

    def advance(self) -> None:
        self.current += 1
        if not self.enabled:
            return
        now = time.monotonic()
        if self.is_terminal:
            if self.current < self.total and now - self.last_update < 0.1:
                return
        else:
            percentage = 100 if self.total == 0 else self.current * 100 // self.total
            milestone = percentage // 10
            if self.current < self.total and milestone <= self.last_milestone:
                return
            self.last_milestone = milestone
        self.last_update = now
        self._emit(final=self.current >= self.total)

    def _emit(self, *, final: bool) -> None:
        ratio = 1.0 if self.total == 0 else min(1.0, self.current / self.total)
        completed = int(32 * ratio)
        bar = "#" * completed + "-" * (32 - completed)
        elapsed = time.monotonic() - self.started
        eta = (
            elapsed / self.current * (self.total - self.current)
            if self.current
            else 0.0
        )
        message = (
            f"{self.label}: [{bar}] {self.current:,}/{self.total:,} "
            f"({ratio * 100:5.1f}%) elapsed {format_duration(elapsed)} "
            f"ETA {format_duration(eta)}"
        )
        if self.is_terminal:
            print(
                f"\r{message}",
                end="\n" if final else "",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(message, file=sys.stderr, flush=True)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def destination_for(source: Path) -> Path:
    """Keep JPEG-like names; otherwise replace the suffix with .jpg."""
    if source.suffix.lower() in JPEG_EXTENSIONS:
        return source
    return source.with_suffix(".jpg")


def bounded_process_map(function, items, workers):
    """Yield bounded process results without queuing the entire dataset."""
    if workers == 1:
        for item in items:
            try:
                yield item, function(item), None
            except Exception as exc:
                yield item, None, exc
        return

    item_iterator = iter(items)
    pending = {}
    with ProcessPoolExecutor(max_workers=workers) as executor:
        for _ in range(workers * WORK_QUEUE_MULTIPLIER):
            try:
                item = next(item_iterator)
            except StopIteration:
                break
            pending[executor.submit(function, item)] = item

        while pending:
            done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in done:
                item = pending.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    yield item, None, exc
                else:
                    yield item, result, None

                try:
                    next_item = next(item_iterator)
                except StopIteration:
                    continue
                pending[executor.submit(function, next_item)] = next_item


def inspect_source(source: Path) -> Inspection:
    """Inspect one source in a worker and fully decode JPEG image data."""
    if source.suffix.lower() in RAW_EXTENSIONS:
        return Inspection(source, "convert", "RAW")

    try:
        with Image.open(source) as image:
            source_format = image.format or "unknown"
            if source_format in {"JPEG", "MPO"}:
                image.seek(0)
                try:
                    image.load()
                except OSError as exc:
                    return Inspection(
                        source,
                        "damaged",
                        source_format,
                        str(exc),
                    )
    except UnidentifiedImageError as exc:
        if source.suffix.lower() in JPEG_EXTENSIONS:
            return Inspection(source, "failure", message=str(exc))
        return Inspection(source, "ignored")
    except OSError as exc:
        return Inspection(source, "failure", message=str(exc))

    if source_format == "JPEG":
        return Inspection(source, "jpeg")
    return Inspection(source, "convert", source_format)


def inspect_directory(
    root: Path,
    *,
    workers: int = DEFAULT_WORKERS,
    show_progress: bool = True,
) -> ScanResult:
    """Classify files and fully decode every JPEG encountered."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Directory does not exist: {root}")

    conversions: list[Conversion] = []
    jpeg_count = 0
    damaged_jpeg_count = 0
    ignored_count = 0
    inspection_failures: list[tuple[Path, str]] = []
    sources = sorted(
        (
            path
            for path in root.rglob("*")
            if not path.is_symlink() and path.is_file()
        ),
        key=lambda path: str(path).lower(),
    )
    progress = ProgressBar("Checking files", len(sources), enabled=show_progress)

    for source, inspection, worker_error in bounded_process_map(
        inspect_source,
        sources,
        workers,
    ):
        try:
            if worker_error is not None:
                inspection_failures.append((source, str(worker_error)))
                continue
            if inspection.outcome == "jpeg":
                jpeg_count += 1
            elif inspection.outcome == "ignored":
                ignored_count += 1
            elif inspection.outcome == "failure":
                inspection_failures.append((source, inspection.message))
            elif inspection.outcome == "damaged":
                damaged_jpeg_count += 1
                conversions.append(
                    Conversion(
                        source,
                        destination_for(source),
                        f"damaged {inspection.source_format}: {inspection.message}",
                    )
                )
            else:
                conversions.append(
                    Conversion(
                        source,
                        destination_for(source),
                        inspection.source_format,
                    )
                )
        finally:
            progress.advance()

    return ScanResult(
        conversions,
        jpeg_count,
        damaged_jpeg_count,
        ignored_count,
        inspection_failures,
    )


def find_conflicts(conversions: list[Conversion]) -> dict[Path, str]:
    """Return conversions that cannot safely claim their destination path."""
    by_destination: dict[Path, list[Conversion]] = {}
    for conversion in conversions:
        by_destination.setdefault(conversion.destination, []).append(conversion)

    conflicts: dict[Path, str] = {}
    for destination, matching in by_destination.items():
        if len(matching) > 1:
            sources = ", ".join(item.source.name for item in matching)
            message = f"multiple sources would become {destination.name}: {sources}"
            for conversion in matching:
                conflicts[conversion.source] = message
            continue

        conversion = matching[0]
        if destination != conversion.source and destination.exists():
            conflicts[conversion.source] = f"destination already exists: {destination}"

    return conflicts


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


def open_image_for_conversion(source: Path, *, relaxed: bool = False) -> Image.Image:
    """Load an image copy, optionally tolerating damaged compressed streams."""
    previous_setting = ImageFile.LOAD_TRUNCATED_IMAGES
    if relaxed:
        ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with Image.open(source) as opened:
            if opened.format == "MPO":
                opened.seek(0)
            opened.load()
            image = opened.copy()
            image.info.update(opened.info)
            exif = opened.getexif()
            if exif:
                image.info["exif"] = exif.tobytes()
            return image
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous_setting


def prepare_for_jpeg(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and image.info.get("transparency") is not None
    ):
        background = Image.new("RGB", image.size, (255, 255, 255))
        alpha_image = image.convert("RGBA")
        alpha_channel = alpha_image.getchannel("A")
        try:
            background.paste(alpha_image, mask=alpha_channel)
        finally:
            alpha_channel.close()
            alpha_image.close()
        return background
    return image.convert("RGB")


def validate_jpeg(path: Path, expected_size: tuple[int, int]) -> None:
    with Image.open(path) as image:
        if image.format != "JPEG":
            raise ValueError(f"converted output is {image.format or 'unknown'}, not JPEG")
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("converted JPEG contains more than one frame")
        orientation = image.getexif().get(JPEG_ORIENTATION_TAG)
        if orientation not in (None, 1):
            raise ValueError(f"converted JPEG still has EXIF orientation {orientation}")
        if image.size != expected_size:
            raise ValueError(
                f"converted dimensions {image.size} do not match {expected_size}"
            )
        image.load()


def atomic_write_jpeg(
    image: Image.Image,
    source: Path,
    destination: Path,
    quality: int,
) -> None:
    """Write and validate a JPEG before replacing or removing its source."""
    oriented_image = ImageOps.exif_transpose(image)
    temporary_path: Path | None = None
    jpeg_image: Image.Image | None = None
    try:
        expected_size = oriented_image.size
        exif = oriented_image.getexif()
        exif[JPEG_ORIENTATION_TAG] = 1
        jpeg_image = prepare_for_jpeg(oriented_image)

        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

        jpeg_image.save(
            temporary_path,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
            exif=exif,
        )
        validate_jpeg(temporary_path, expected_size)

        # Copy timestamps, permissions, flags, and extended attributes where the
        # filesystem permits it. This happens before replacement so same-name MPO
        # normalization retains the original source metadata.
        try:
            shutil.copystat(source, temporary_path, follow_symlinks=True)
        except OSError:
            pass

        if destination == source:
            os.replace(temporary_path, destination)
            temporary_path = None
        else:
            # A hard link publishes the completed temp file atomically but, unlike
            # os.replace(), refuses to overwrite a destination created after the
            # initial collision scan.
            os.link(temporary_path, destination)
            temporary_path.unlink()
            temporary_path = None

        if destination != source:
            try:
                source.unlink()
            except OSError as exc:
                try:
                    destination.unlink()
                except OSError as rollback_exc:
                    raise OSError(
                        f"could not remove {source}; JPEG also remains at {destination}: {exc}"
                    ) from rollback_exc
                raise OSError(
                    f"could not remove {source}; the new JPEG was removed and the source was kept: {exc}"
                ) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if jpeg_image is not None and jpeg_image is not oriented_image:
            jpeg_image.close()
        if oriented_image is not image:
            oriented_image.close()


def convert_image(conversion: Conversion, quality: int) -> None:
    image: Image.Image | None = None
    try:
        if conversion.source.suffix.lower() in RAW_EXTENSIONS:
            image = open_raw_image(conversion.source)
        else:
            try:
                image = open_image_for_conversion(conversion.source)
            except OSError as strict_error:
                try:
                    image = open_image_for_conversion(
                        conversion.source,
                        relaxed=True,
                    )
                except Exception as relaxed_error:
                    raise OSError(
                        f"strict decode failed ({strict_error}); "
                        f"recovery decode failed ({relaxed_error})"
                    ) from relaxed_error

        atomic_write_jpeg(
            image,
            conversion.source,
            conversion.destination,
            quality,
        )
    finally:
        if image is not None:
            image.close()


def convert_image_job(job) -> None:
    """Process-pool entry point for one in-place normalization."""
    conversion, quality = job
    convert_image(conversion, quality)


def validate_quality(value: int) -> int:
    if not 1 <= value <= 95:
        raise ValueError("JPEG quality must be between 1 and 95")
    return value


def validate_workers(value: int) -> int:
    if not 1 <= value <= MAX_WORKERS:
        raise ValueError(f"Workers must be between 1 and {MAX_WORKERS}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively fully decode JPEGs, repair recoverable damaged streams, "
            "and replace non-JPEG images with standard JPEGs in place."
        )
    )
    parser.add_argument(
        "directory",
        type=Path,
        help="directory to scan recursively",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_JPEG_QUALITY,
        help=f"JPEG quality from 1 to 95 (default: {DEFAULT_JPEG_QUALITY})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"parallel worker processes (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change without writing or deleting files",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="disable terminal progress bars",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every file selected for normalization",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        quality = validate_quality(args.quality)
        workers = validate_workers(args.workers)
    except ValueError as exc:
        parser.error(str(exc))

    root = args.directory.expanduser().resolve()
    print(f"Directory: {root}", flush=True)
    print("Finding files and fully decoding JPEG data...", flush=True)
    try:
        scan = inspect_directory(
            root,
            workers=workers,
            show_progress=not args.no_progress,
        )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    conflicts = find_conflicts(scan.conversions)
    failures = list(scan.inspection_failures)
    converted = 0
    damaged_jpegs_fixed = 0
    progress = ProgressBar(
        "Normalizing files",
        len(scan.conversions),
        enabled=not args.no_progress,
    )

    eligible_conversions = []
    for conversion in scan.conversions:
        relative_source = conversion.source.relative_to(root)
        relative_destination = conversion.destination.relative_to(root)
        description = str(relative_source)
        if relative_destination != relative_source:
            description += f" -> {relative_destination}"

        conflict = conflicts.get(conversion.source)
        if conflict:
            failures.append((conversion.source, conflict))
            if args.verbose:
                print(f"Cannot normalize: {description}")
            progress.advance()
            continue
        eligible_conversions.append(conversion)

    if args.dry_run:
        for conversion in eligible_conversions:
            relative_source = conversion.source.relative_to(root)
            relative_destination = conversion.destination.relative_to(root)
            description = str(relative_source)
            if relative_destination != relative_source:
                description += f" -> {relative_destination}"
            print(f"Would normalize {conversion.source_format}: {description}")
            progress.advance()
    else:
        jobs = ((conversion, quality) for conversion in eligible_conversions)
        for job, _result, worker_error in bounded_process_map(
            convert_image_job,
            jobs,
            workers,
        ):
            conversion, _quality = job
            relative_source = conversion.source.relative_to(root)
            try:
                if worker_error is not None:
                    failures.append((conversion.source, str(worker_error)))
                    continue
                converted += 1
                if conversion.source_format.startswith("damaged "):
                    damaged_jpegs_fixed += 1
                if args.verbose:
                    print(
                        f"Normalized {conversion.source_format}: {relative_source}"
                    )
            finally:
                progress.advance()

    if args.dry_run:
        print(f"Would normalize: {len(eligible_conversions)}")
        print(f"Damaged JPEG candidates found: {scan.damaged_jpeg_count}")
    else:
        print(f"Normalized: {converted}")
        print(f"Damaged JPEGs repaired: {damaged_jpegs_fixed}")
    print(f"Worker processes: {workers}")
    print(f"Fully decoded standard JPEGs left unchanged: {scan.jpeg_count}")
    print(f"Ignored non-image files: {scan.ignored_count}")
    print(f"Failed: {len(failures)}")
    if failures:
        print("Failures:", file=sys.stderr)
        for path, message in failures:
            try:
                display_path = path.relative_to(root)
            except ValueError:
                display_path = path
            print(f"- {display_path}: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

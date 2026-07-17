#!/usr/bin/env python3
"""Move trailing NKW identifiers to the front of JPEG and RAW filenames."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path


JPEG_EXTENSIONS = {".jpg", ".jpeg"}
RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".dcr",
    ".dng",
    ".erf",
    ".iiq",
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
SUPPORTED_EXTENSIONS = JPEG_EXTENSIONS | RAW_EXTENSIONS
NKW_ID_RE = re.compile(r"NKW-\d+", re.IGNORECASE)
HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Rename:
    source: Path
    destination: Path


def flipped_name(path: Path) -> str | None:
    """Return the flipped filename, or None when the file should be skipped."""
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None

    parts = path.stem.split("_")

    trailing_identifiers: list[str] = []
    while parts and NKW_ID_RE.fullmatch(parts[-1]):
        trailing_identifiers.append(parts.pop().upper())
    if not trailing_identifiers:
        return None
    trailing_identifiers.reverse()

    leading_identifiers: list[str] = []
    while parts and NKW_ID_RE.fullmatch(parts[0]):
        leading_identifiers.append(parts.pop(0).upper())

    identifiers = "_".join(
        dict.fromkeys(leading_identifiers + trailing_identifiers)
    )
    original_name = "_".join(parts).lstrip("_")
    if not original_name:
        return None
    return f"{identifiers}_{original_name}{path.suffix}"


def find_renames(root: Path) -> list[Rename]:
    """Find eligible regular files recursively without changing the filesystem."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    renames: list[Rename] = []
    for source in root.rglob("*"):
        if source.is_symlink() or not source.is_file():
            continue
        new_name = flipped_name(source)
        if new_name is not None:
            renames.append(Rename(source, source.with_name(new_name)))

    return sorted(renames, key=lambda rename: str(rename.source).lower())


def validate_renames(renames: list[Rename]) -> None:
    """Reject destination conflicts before any files are renamed."""
    destinations: set[Path] = set()
    for rename in renames:
        if rename.destination in destinations:
            raise FileExistsError(f"More than one file would become: {rename.destination}")
        destinations.add(rename.destination)
        if rename.destination.exists():
            raise FileExistsError(f"Destination already exists: {rename.destination}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_renames(renames: list[Rename]) -> None:
    """Rename files and verify that every file's bytes remain identical."""
    validate_renames(renames)
    for rename in renames:
        checksum_before = sha256(rename.source)
        rename.source.rename(rename.destination)
        checksum_after = sha256(rename.destination)
        if checksum_after != checksum_before:
            # A rename does not normally touch file contents. Restore the original
            # name if an external process somehow changed the file concurrently.
            rename.destination.rename(rename.source)
            raise OSError(f"Integrity check failed; restored: {rename.source}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively rename JPEG and RAW files from NAME_NKW-XXX.ext to "
            "NKW-XXX_NAME.ext without rewriting file contents."
        )
    )
    parser.add_argument("directory", type=Path, help="Directory containing files to rename")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show matching renames without changing any filenames",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        renames = find_renames(args.directory)
        validate_renames(renames)
        if not args.dry_run:
            apply_renames(renames)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    action = "Would rename" if args.dry_run else "Renamed"
    for rename in renames:
        print(f"{action}: {rename.source.name} -> {rename.destination.name}")
    print(f"{action} {len(renames)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

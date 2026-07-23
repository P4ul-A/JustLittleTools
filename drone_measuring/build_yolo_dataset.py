#!/usr/bin/env python3
"""Build a leakage-resistant YOLO dataset from reviewed SAM export folders."""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import shutil
import sys
from pathlib import Path


ACCEPTED_STATUSES = {"APPROVED", "CORRECTED"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy approved SAM annotations into a YOLO segmentation dataset. "
            "Whole export folders/videos are assigned to train or validation."
        )
    )
    parser.add_argument(
        "exports",
        type=Path,
        nargs="+",
        help="Two or more reviewed SAM export directories",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New output dataset directory",
    )
    parser.add_argument(
        "--val-fraction",
        type=float,
        default=0.2,
        help="Fraction of source videos assigned to validation (default: %(default)s)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=2026,
        help="Reproducible video-group shuffle seed (default: %(default)s)",
    )
    parser.add_argument(
        "--class-name",
        default="whale",
        help="Name for class 0 in the generated YAML (default: %(default)s)",
    )
    args = parser.parse_args()
    if len(args.exports) < 2:
        parser.error(
            "provide at least two export folders so validation uses a different video"
        )
    if not 0.0 < args.val_fraction < 1.0:
        parser.error("--val-fraction must be greater than 0 and less than 1")
    return args


def safe_group_name(path: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.name).strip("._")
    return name or "video"


def validate_single_class_label(path: Path) -> None:
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        fields = line.split()
        if len(fields) < 7 or (len(fields) - 1) % 2:
            raise SystemExit(f"Invalid YOLO polygon at {path}:{line_number}")
        try:
            class_id = int(fields[0])
            coordinates = [float(value) for value in fields[1:]]
        except ValueError as exc:
            raise SystemExit(
                f"Non-numeric YOLO label at {path}:{line_number}"
            ) from exc
        if class_id != 0:
            raise SystemExit(
                f"Expected single-class ID 0 at {path}:{line_number}, got {class_id}"
            )
        if any(value < 0.0 or value > 1.0 for value in coordinates):
            raise SystemExit(
                f"Coordinate outside 0..1 at {path}:{line_number}"
            )


def read_approved_rows(export_dir: Path) -> list[dict[str, str]]:
    manifest = export_dir / "manifest.csv"
    if not manifest.is_file():
        raise SystemExit(f"Missing manifest: {manifest}")
    with manifest.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    approved = [
        row for row in rows if row.get("review_status", "") in ACCEPTED_STATUSES
    ]
    if not approved:
        raise SystemExit(
            f"No APPROVED or CORRECTED rows in: {manifest}\n"
            "Run review_sam_export.py first."
        )
    for row in approved:
        image = export_dir / row["image"]
        label = export_dir / row["label"]
        if not image.is_file() or not label.is_file():
            raise SystemExit(f"Missing approved image or label: {image} / {label}")
        validate_single_class_label(label)
    return approved


def copy_group(
    export_dir: Path,
    rows: list[dict[str, str]],
    split: str,
    output: Path,
    split_writer,
) -> int:
    prefix = safe_group_name(export_dir)
    copied = 0
    for row in rows:
        source_image = export_dir / row["image"]
        source_label = export_dir / row["label"]
        destination_stem = f"{prefix}__{source_image.stem}"
        destination_image = (
            output / "images" / split / f"{destination_stem}{source_image.suffix.lower()}"
        )
        destination_label = output / "labels" / split / f"{destination_stem}.txt"
        if destination_image.exists() or destination_label.exists():
            raise SystemExit(
                f"Filename collision while building dataset: {destination_stem}"
            )
        shutil.copy2(source_image, destination_image)
        shutil.copy2(source_label, destination_label)
        split_writer.writerow(
            {
                "split": split,
                "source_export": str(export_dir),
                "source_frame": row.get("source_frame", ""),
                "image": str(destination_image.relative_to(output)),
                "label": str(destination_label.relative_to(output)),
            }
        )
        copied += 1
    return copied


def main() -> int:
    args = parse_args()
    exports = [path.expanduser().resolve() for path in args.exports]
    if len(set(exports)) != len(exports):
        raise SystemExit("Each export directory must be provided only once.")
    for export_dir in exports:
        if not export_dir.is_dir():
            raise SystemExit(f"Export directory does not exist: {export_dir}")

    output = args.output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(
            f"Output directory is not empty: {output}\n"
            "Use a new directory to avoid mixing dataset versions."
        )

    groups = [(export_dir, read_approved_rows(export_dir)) for export_dir in exports]
    random.Random(args.seed).shuffle(groups)
    validation_count = max(1, round(len(groups) * args.val_fraction))
    validation_count = min(validation_count, len(groups) - 1)
    validation_groups = {path for path, _ in groups[:validation_count]}

    for split in ("train", "val"):
        (output / "images" / split).mkdir(parents=True, exist_ok=True)
        (output / "labels" / split).mkdir(parents=True, exist_ok=True)

    manifest_path = output / "split_manifest.csv"
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "split",
                "source_export",
                "source_frame",
                "image",
                "label",
            ],
        )
        writer.writeheader()
        counts = {"train": 0, "val": 0}
        for export_dir, rows in groups:
            split = "val" if export_dir in validation_groups else "train"
            counts[split] += copy_group(
                export_dir, rows, split, output, writer
            )

    yaml_path = output / "whales.yaml"
    yaml_path.write_text(
        "\n".join(
            [
                f"path: {json.dumps(str(output))}",
                "train: images/train",
                "val: images/val",
                "names:",
                f"  0: {json.dumps(args.class_name)}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(
        f"Built dataset with {counts['train']} training and "
        f"{counts['val']} validation images.\n"
        f"Dataset YAML:\n  {yaml_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

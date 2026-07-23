#!/usr/bin/env python3
"""Quickly approve or reject SAM-generated YOLO segmentation candidates."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path


REVIEWED_STATUSES = {"APPROVED", "CORRECTED", "REJECTED"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Display SAM candidate masks and update manifest.csv. "
            "Keys: A approve, C corrected, R reject, U reset, Q save and quit."
        )
    )
    parser.add_argument(
        "export_dir",
        type=Path,
        help="SAM export directory containing images, labels, and manifest.csv",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Review all rows, including previously reviewed rows",
    )
    return parser.parse_args()


def load_runtime():
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise SystemExit(
            "Missing OpenCV. Install dependencies with:\n"
            "  .venv/bin/python -m pip install -U ultralytics\n"
            f"\nOriginal import error: {exc}"
        ) from exc
    return cv2, np


def read_manifest(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise SystemExit(f"Manifest does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "review_status" not in reader.fieldnames:
            raise SystemExit(f"Manifest has no review_status column: {path}")
        return list(reader.fieldnames), list(reader)


def write_manifest_atomic(
    path: Path, fieldnames: list[str], rows: list[dict[str, str]]
) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(file_descriptor, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def draw_yolo_labels(image, label_path: Path, cv2, np):
    overlay = image.copy()
    height, width = image.shape[:2]
    if not label_path.is_file():
        raise RuntimeError(f"Missing label: {label_path}")

    text = label_path.read_text(encoding="utf-8").strip()
    if not text:
        return overlay, 0

    count = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        fields = line.split()
        if len(fields) < 7 or (len(fields) - 1) % 2:
            raise RuntimeError(
                f"Invalid polygon at {label_path}:{line_number}: {line}"
            )
        coordinates = np.asarray([float(value) for value in fields[1:]], dtype=np.float32)
        polygon = coordinates.reshape(-1, 2)
        polygon[:, 0] *= width
        polygon[:, 1] *= height
        polygon = np.rint(polygon).astype(np.int32)
        cv2.fillPoly(overlay, [polygon], (0, 200, 255))
        cv2.polylines(image, [polygon], True, (0, 255, 255), 3, cv2.LINE_AA)
        count += 1
    cv2.addWeighted(overlay, 0.35, image, 0.65, 0, image)
    return image, count


def fit_for_display(image, cv2):
    height, width = image.shape[:2]
    scale = min(1.0, 1600.0 / width, 900.0 / height)
    if scale == 1.0:
        return image
    return cv2.resize(
        image,
        (round(width * scale), round(height * scale)),
        interpolation=cv2.INTER_AREA,
    )


def main() -> int:
    args = parse_args()
    cv2, np = load_runtime()
    export_dir = args.export_dir.expanduser().resolve()
    manifest_path = export_dir / "manifest.csv"
    fieldnames, rows = read_manifest(manifest_path)

    indices = [
        index
        for index, row in enumerate(rows)
        if args.all or row.get("review_status", "UNREVIEWED") not in REVIEWED_STATUSES
    ]
    if not indices:
        print("Nothing to review.")
        return 0

    window = "SAM mask review"
    changed = False
    try:
        for position, row_index in enumerate(indices, start=1):
            row = rows[row_index]
            image_path = export_dir / row["image"]
            label_path = export_dir / row["label"]
            image = cv2.imread(str(image_path))
            if image is None:
                raise RuntimeError(f"Could not read image: {image_path}")
            image, instance_count = draw_yolo_labels(
                image, label_path, cv2, np
            )
            status = row.get("review_status", "UNREVIEWED")
            heading = (
                f"{position}/{len(indices)} | {instance_count} mask(s) | {status} | "
                "A approve  C corrected  R reject  U reset  Q quit"
            )
            cv2.rectangle(image, (0, 0), (image.shape[1], 55), (0, 0, 0), -1)
            cv2.putText(
                image,
                heading,
                (18, 37),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.85,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            cv2.imshow(window, fit_for_display(image, cv2))

            while True:
                key = cv2.waitKey(0) & 0xFF
                if key in {ord("a"), ord("A")}:
                    row["review_status"] = "APPROVED"
                    changed = True
                    break
                if key in {ord("c"), ord("C")}:
                    row["review_status"] = "CORRECTED"
                    changed = True
                    break
                if key in {ord("r"), ord("R")}:
                    row["review_status"] = "REJECTED"
                    changed = True
                    break
                if key in {ord("u"), ord("U")}:
                    row["review_status"] = "UNREVIEWED"
                    changed = True
                    break
                if key in {ord("q"), ord("Q"), 27}:
                    if changed:
                        write_manifest_atomic(manifest_path, fieldnames, rows)
                    print("Review progress saved.")
                    return 0
    finally:
        cv2.destroyAllWindows()

    if changed:
        write_manifest_atomic(manifest_path, fieldnames, rows)
    approved = sum(row.get("review_status") == "APPROVED" for row in rows)
    corrected = sum(row.get("review_status") == "CORRECTED" for row in rows)
    rejected = sum(row.get("review_status") == "REJECTED" for row in rows)
    print(
        f"Review saved: {approved} approved, {corrected} corrected, "
        f"{rejected} rejected."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

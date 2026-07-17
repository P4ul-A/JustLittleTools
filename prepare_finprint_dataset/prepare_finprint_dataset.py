#!/usr/bin/env python3
"""Prepare encounter-aware FinPrintv2 identification data from crop manifests.

The source tree is read-only. Output is first assembled in a temporary sibling
directory and then renamed into place so a failed run does not leave a partial
dataset at the requested destination.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import Any


DEFAULT_SOURCE = Path("/Volumes/SSD_ID/cluster_all_2sec")
DEFAULT_MINIMUM_TOTAL_IMAGES: int | None = 10
DEFAULT_MAXIMUM_MANUAL_IMAGES: int | None = 30
DEFAULT_MODE = "copy"
DEFAULT_EXCLUDE_LETTER_SUFFIX_IDS = True

ID_RE = re.compile(r"^(?:NKW-[0-9]+[A-Za-z]?|KI[0-9]+)$")
LETTER_SUFFIX_ID_RE = re.compile(r"^NKW-[0-9]+[A-Za-z]$")
ID_PARTS_RE = re.compile(r"^(NKW-|KI)([0-9]+)([A-Za-z]?)$")
CSV_COLUMNS = ("Label", "Filepath", "Root")
REPORT_CSV_COLUMNS = (
    "RecordType",
    "ID",
    "Status",
    "Count",
    "SHA256",
    "Source",
    "Output",
    "Encounter",
    "Split",
    "Details",
)


@dataclass(frozen=True)
class Crop:
    observed_id: str
    canonical_id: str
    source: Path
    source_display: str
    sha256: str
    encounter: str
    source_kind: str
    manifest_line: int
    output_name: str = ""
    split: str = ""


@dataclass
class Preparation:
    crops: list[Crop]
    report: dict[str, Any]


class Progress:
    """Compact terminal progress with sparse milestones for redirected logs."""

    def __init__(self, label: str, total: int, *, enabled: bool = True):
        self.label = label
        self.total = total
        self.enabled = enabled
        self.count = 0
        self.is_terminal = sys.stderr.isatty()
        self.last_update = 0.0
        self.last_milestone = -1
        if enabled:
            self._emit(force=True)
            if not self.is_terminal:
                self.last_milestone = 0

    def advance(self) -> None:
        self.count += 1
        if not self.enabled:
            return
        now = time.monotonic()
        percentage = 100 if self.total == 0 else min(100, self.count * 100 // self.total)
        if self.is_terminal:
            if self.count < self.total and now - self.last_update < 0.1:
                return
        else:
            milestone = percentage // 10
            if self.count < self.total and milestone <= self.last_milestone:
                return
            self.last_milestone = milestone
        self.last_update = now
        self._emit(force=self.count >= self.total)

    def finish(self) -> None:
        if not self.enabled or self.count >= self.total:
            return
        self.count = self.total
        self._emit(force=True)

    def _emit(self, *, force: bool) -> None:
        percentage = 100 if self.total == 0 else min(100, self.count * 100 // self.total)
        message = f"{self.label}: {percentage:3d}% ({self.count:,}/{self.total:,})"
        if self.is_terminal:
            print(f"\r{message}", end="\n" if force and self.count >= self.total else "", file=sys.stderr, flush=True)
        else:
            print(message, file=sys.stderr, flush=True)


def sha256_file(path: Path) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    prefix = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            if not prefix:
                prefix = chunk[:3]
            digest.update(chunk)
    return digest.hexdigest(), prefix


def alias_key(identity: str) -> tuple[str, int, str]:
    match = ID_PARTS_RE.fullmatch(identity)
    if match is None:
        raise ValueError(f"invalid identity: {identity!r}")
    prefix, number, suffix = match.groups()
    return prefix, int(number), suffix.casefold()


def read_alias_map(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in data.items()
    ):
        raise ValueError("alias map must be a JSON object of observed ID to canonical ID")
    for observed, canonical in data.items():
        if not ID_RE.fullmatch(observed) or not ID_RE.fullmatch(canonical):
            raise ValueError(f"invalid alias mapping {observed!r} -> {canonical!r}")
        if alias_key(observed) != alias_key(canonical):
            raise ValueError(
                f"alias mapping changes the numeric identity: {observed!r} -> {canonical!r}"
            )
    return data


def canonical_alias_name(key: tuple[str, int, str]) -> str:
    prefix, number, suffix = key
    minimum_width = 3 if prefix == "NKW-" else 2
    number_text = str(number).zfill(minimum_width)
    return f"{prefix}{number_text}{suffix}"


def find_aliases(
    valid_ids: list[str], mappings: dict[str, str]
) -> tuple[list[dict], set[str], dict[str, str]]:
    groups: dict[tuple[str, int, str], list[str]] = defaultdict(list)
    for identity in valid_ids:
        groups[alias_key(identity)].append(identity)

    aliases: list[dict] = []
    unresolved: set[str] = set()
    resolved_mappings = dict(mappings)
    valid_set = set(valid_ids)
    unknown = sorted(set(mappings) - valid_set)
    if unknown:
        raise ValueError(f"alias map contains IDs not present in the source: {', '.join(unknown)}")

    for key, identities in sorted(groups.items()):
        if len(identities) < 2:
            continue
        explicit_mappings = {
            identity: mappings[identity]
            for identity in identities
            if identity in mappings
        }
        if explicit_mappings:
            canonical_values = {
                mappings.get(identity, identity) for identity in identities
            }
            resolved = len(canonical_values) == 1
            canonical = next(iter(canonical_values)) if resolved else None
            resolution = "explicit" if resolved else "conflicting_explicit_mappings"
        else:
            resolved = True
            canonical = canonical_alias_name(key)
            resolution = "automatic_zero_padding"
        if not resolved:
            unresolved.update(identities)
        else:
            resolved_mappings.update(
                {identity: canonical for identity in identities}
            )
        aliases.append(
            {
                "ids": identities,
                "resolved": resolved,
                "canonical_id": canonical,
                "resolution": resolution,
                "explicit_mappings": explicit_mappings,
            }
        )
    return aliases, unresolved, resolved_mappings


def encounter_from_manifest(record: dict[str, Any]) -> str:
    """Return the camera-burst key, with a conservative folder fallback."""
    encounter_id = record.get("encounter_id")
    if isinstance(encounter_id, str) and encounter_id:
        return encounter_id

    path_value: str | None = None
    if record.get("source_kind") == "manual":
        path_value = record.get("matched_original_path")
        if not path_value:
            for match in record.get("manual_anchor_matches") or ():
                if isinstance(match, dict) and match.get("matched_original_path"):
                    path_value = match["matched_original_path"]
                    break
    path_value = path_value or record.get("source_path") or record.get("source_relative_path")
    if not isinstance(path_value, str) or not path_value:
        return "unknown-encounter"
    return f"fallback-folder:{PurePath(path_value).parent.as_posix()}"


def read_manifest(crop_dir: Path) -> tuple[dict[str, tuple[dict[str, Any], int]], list[dict]]:
    manifest_path = crop_dir / "crop_manifest.jsonl"
    records: dict[str, tuple[dict[str, Any], int]] = {}
    errors: list[dict] = []
    if not manifest_path.is_file():
        return records, [{"manifest": str(manifest_path), "error": "missing manifest"}]

    with manifest_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
                name = record.get("crop_file")
                if not isinstance(name, str) or Path(name).name != name:
                    raise ValueError("crop_file must be a plain filename")
                if Path(name).suffix.lower() != ".jpg":
                    raise ValueError("crop_file is not a .jpg")
                if name in records:
                    raise ValueError("duplicate crop_file entry")
                records[name] = (record, line_number)
            except (json.JSONDecodeError, ValueError, AttributeError) as exc:
                errors.append(
                    {
                        "manifest": str(manifest_path),
                        "line": line_number,
                        "error": str(exc),
                    }
                )
    return records, errors


def scan_crops(
    source: Path,
    valid_ids: list[str],
    mappings: dict[str, str],
    *,
    show_progress: bool,
) -> tuple[list[Crop], list[dict], list[dict], list[str]]:
    crops: list[Crop] = []
    rejected: list[dict] = []
    manifest_errors: list[dict] = []
    ids_without_manifest: list[str] = []

    jpegs_by_id: dict[str, list[Path]] = {}
    for observed_id in valid_ids:
        crop_dir = source / observed_id / "cropped"
        if not crop_dir.is_dir():
            jpegs_by_id[observed_id] = []
            continue
        jpegs_by_id[observed_id] = sorted(
            (
                path
                for path in crop_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".jpg"
            ),
            key=lambda path: path.name.casefold(),
        )
    progress = Progress(
        "Hashing crop JPEGs",
        sum(len(paths) for paths in jpegs_by_id.values()),
        enabled=show_progress,
    )

    for observed_id in valid_ids:
        crop_dir = source / observed_id / "cropped"
        manifest, errors = read_manifest(crop_dir)
        manifest_errors.extend(errors)
        if not manifest:
            ids_without_manifest.append(observed_id)

        disk_jpegs = jpegs_by_id[observed_id]
        disk_names = {path.name for path in disk_jpegs}
        for name in sorted(set(manifest) - disk_names):
            rejected.append(
                {
                    "id": observed_id,
                    "source": str(crop_dir / name),
                    "reason": "manifest crop is missing",
                }
            )

        canonical_id = mappings.get(observed_id, observed_id)
        for crop_path in disk_jpegs:
            digest, prefix = sha256_file(crop_path)
            progress.advance()
            if crop_path.name not in manifest:
                rejected.append(
                    {
                        "id": observed_id,
                        "source": str(crop_path),
                        "sha256": digest,
                        "reason": "JPEG is not listed in crop_manifest.jsonl",
                    }
                )
                continue
            if prefix != b"\xff\xd8\xff":
                rejected.append(
                    {
                        "id": observed_id,
                        "source": str(crop_path),
                        "sha256": digest,
                        "reason": "file does not have a JPEG signature",
                    }
                )
                continue
            record, line_number = manifest[crop_path.name]
            source_kind = record.get("source_kind")
            if source_kind not in {"manual", "additional"}:
                rejected.append(
                    {
                        "id": observed_id,
                        "source": str(crop_path),
                        "sha256": digest,
                        "reason": "manifest source_kind is not manual or additional",
                    }
                )
                continue
            crops.append(
                Crop(
                    observed_id=observed_id,
                    canonical_id=canonical_id,
                    source=crop_path,
                    source_display=f"{observed_id}/cropped/{crop_path.name}",
                    sha256=digest,
                    encounter=encounter_from_manifest(record),
                    source_kind=source_kind,
                    manifest_line=line_number,
                )
            )
    progress.finish()
    return crops, rejected, manifest_errors, ids_without_manifest


def remove_hash_duplicates(crops: list[Crop]) -> tuple[list[Crop], list[dict], list[dict]]:
    by_hash: dict[str, list[Crop]] = defaultdict(list)
    for crop in crops:
        by_hash[crop.sha256].append(crop)

    clean: list[Crop] = []
    same_id_duplicates: list[dict] = []
    cross_id_conflicts: list[dict] = []
    for digest, matches in sorted(by_hash.items()):
        ordered = sorted(
            matches,
            key=lambda crop: (
                crop.source_kind != "manual",
                crop.source_display.casefold(),
            ),
        )
        identities = sorted({crop.canonical_id for crop in ordered})
        if len(identities) > 1:
            cross_id_conflicts.append(
                {
                    "sha256": digest,
                    "ids": identities,
                    "copies": [crop.source_display for crop in ordered],
                }
            )
            continue
        clean.append(ordered[0])
        if len(ordered) > 1:
            same_id_duplicates.append(
                {
                    "id": identities[0],
                    "sha256": digest,
                    "kept": ordered[0].source_display,
                    "excluded": [crop.source_display for crop in ordered[1:]],
                }
            )
    return clean, same_id_duplicates, cross_id_conflicts


def choose_output_names(crops: list[Crop]) -> list[Crop]:
    by_id_and_name: dict[tuple[str, str], list[Crop]] = defaultdict(list)
    for crop in crops:
        by_id_and_name[(crop.canonical_id, crop.source.name.casefold())].append(crop)

    named: list[Crop] = []
    for (_identity, _name), matches in sorted(by_id_and_name.items()):
        ordered = sorted(matches, key=lambda crop: crop.source_display.casefold())
        if len(ordered) == 1:
            named.append(replace(ordered[0], output_name=ordered[0].source.name))
            continue
        for crop in ordered:
            output_name = f"{crop.source.stem}__sha_{crop.sha256[:12]}.jpg"
            named.append(replace(crop, output_name=output_name))
    return named


def encounter_order(identity: str, encounter: str, seed: str) -> str:
    return hashlib.sha256(f"{seed}\0{identity}\0{encounter}".encode()).hexdigest()


def source_kind_counts(crops: list[Crop]) -> dict[str, int]:
    return {
        "manual": sum(crop.source_kind == "manual" for crop in crops),
        "additional": sum(crop.source_kind == "additional" for crop in crops),
        "total": len(crops),
    }


def select_burst_diverse_manual_crops(
    identity: str, manual_crops: list[Crop], maximum: int, seed: str
) -> list[Crop]:
    """Select deterministically, taking one crop per burst before taking seconds."""
    by_encounter: dict[str, list[Crop]] = defaultdict(list)
    for crop in manual_crops:
        by_encounter[crop.encounter].append(crop)
    for members in by_encounter.values():
        members.sort(key=lambda crop: crop.source_display.casefold())

    encounters = sorted(
        by_encounter,
        key=lambda encounter: encounter_order(identity, encounter, seed),
    )
    selected: list[Crop] = []
    member_index = 0
    while len(selected) < maximum:
        found_member = False
        for encounter in encounters:
            members = by_encounter[encounter]
            if member_index >= len(members):
                continue
            selected.append(members[member_index])
            found_member = True
            if len(selected) == maximum:
                break
        if not found_member:
            break
        member_index += 1
    return selected


def balance_crops(
    crops: list[Crop],
    candidate_ids: set[str],
    minimum_total_images: int | None,
    maximum_manual_images: int | None,
    seed: str,
) -> tuple[list[Crop], list[dict], list[dict], list[dict]]:
    by_id: dict[str, list[Crop]] = defaultdict(list)
    for crop in crops:
        by_id[crop.canonical_id].append(crop)

    retained: list[Crop] = []
    insufficient: list[dict] = []
    balancing_details: list[dict] = []
    omitted_images: list[dict] = []
    for identity in sorted(candidate_ids):
        ordered = sorted(
            by_id.get(identity, ()), key=lambda crop: crop.source_display.casefold()
        )
        before = source_kind_counts(ordered)
        status = "unchanged"
        kept = ordered

        if minimum_total_images is not None and before["total"] < minimum_total_images:
            status = "excluded_insufficient"
            kept = []
            insufficient.append(
                {
                    "id": identity,
                    "clean_image_count": before["total"],
                    "manual_image_count": before["manual"],
                    "additional_image_count": before["additional"],
                    "minimum": minimum_total_images,
                }
            )
            omitted_reason = "ID is below the minimum total image cutoff"
        elif before["total"] == 0:
            status = "excluded_no_clean_images"
            kept = []
            omitted_reason = "ID has no clean manifest-backed crops"
        elif (
            maximum_manual_images is not None
            and before["manual"] > maximum_manual_images
        ):
            status = "capped_manual_only"
            manual = [crop for crop in ordered if crop.source_kind == "manual"]
            kept = select_burst_diverse_manual_crops(
                identity, manual, maximum_manual_images, seed
            )
            omitted_reason = "omitted by manual-only cap"
        else:
            omitted_reason = ""

        kept_set = set(kept)
        omitted = [crop for crop in ordered if crop not in kept_set]
        for crop in omitted:
            if status == "capped_manual_only":
                reason = (
                    "additional crop omitted because the manual-image cap was exceeded"
                    if crop.source_kind == "additional"
                    else "manual crop omitted above the manual-image cap"
                )
            else:
                reason = omitted_reason
            omitted_images.append(
                {
                    "id": crop.canonical_id,
                    "observed_id": crop.observed_id,
                    "source": crop.source_display,
                    "sha256": crop.sha256,
                    "source_kind": crop.source_kind,
                    "encounter": crop.encounter,
                    "reason": reason,
                }
            )

        after = source_kind_counts(kept)
        balancing_details.append(
            {
                "id": identity,
                "status": status,
                "before": before,
                "after": after,
                "omitted": {
                    "manual": before["manual"] - after["manual"],
                    "additional": before["additional"] - after["additional"],
                    "total": before["total"] - after["total"],
                },
            }
        )
        retained.extend(kept)

    return retained, insufficient, balancing_details, omitted_images


def eval_group_counts(group_count: int, val_fraction: float, test_fraction: float) -> tuple[int, int]:
    if group_count < 3 or val_fraction == 0 or test_fraction == 0:
        return 0, 0
    val_count = max(1, round(group_count * val_fraction))
    test_count = max(1, round(group_count * test_fraction))
    while val_count + test_count >= group_count:
        if val_count >= test_count and val_count > 1:
            val_count -= 1
        elif test_count > 1:
            test_count -= 1
        else:
            break
    return val_count, test_count


def assign_splits(
    crops: list[Crop], val_fraction: float, test_fraction: float, seed: str
) -> tuple[list[Crop], list[dict]]:
    by_id: dict[str, list[Crop]] = defaultdict(list)
    for crop in crops:
        by_id[crop.canonical_id].append(crop)

    assigned: list[Crop] = []
    split_details: list[dict] = []
    for identity, identity_crops in sorted(by_id.items()):
        groups: dict[str, list[Crop]] = defaultdict(list)
        for crop in identity_crops:
            groups[crop.encounter].append(crop)
        ordered_encounters = sorted(
            groups, key=lambda encounter: encounter_order(identity, encounter, seed)
        )
        val_count, test_count = eval_group_counts(
            len(ordered_encounters), val_fraction, test_fraction
        )
        val_encounters = set(ordered_encounters[:val_count])
        test_encounters = set(ordered_encounters[val_count : val_count + test_count])
        counts = {"train": 0, "val": 0, "test": 0}
        encounter_counts = {"train": 0, "val": val_count, "test": test_count}
        encounter_counts["train"] = len(ordered_encounters) - val_count - test_count
        for encounter, members in groups.items():
            split = "val" if encounter in val_encounters else "test" if encounter in test_encounters else "train"
            counts[split] += len(members)
            assigned.extend(replace(crop, split=split) for crop in members)
        split_details.append(
            {
                "id": identity,
                "independent_encounters": len(ordered_encounters),
                "training_only": val_count == 0 and test_count == 0,
                "image_counts": counts,
                "encounter_counts": encounter_counts,
            }
        )
    return assigned, split_details


def validate_fractions(val_fraction: float, test_fraction: float) -> None:
    if not 0 <= val_fraction < 1 or not 0 <= test_fraction < 1:
        raise ValueError("validation and test fractions must be in [0, 1)")
    if val_fraction + test_fraction >= 1:
        raise ValueError("validation and test fractions must sum to less than 1")


def validate_cutoffs(
    minimum_total_images: int | None, maximum_manual_images: int | None
) -> None:
    if minimum_total_images is not None and minimum_total_images <= 0:
        raise ValueError("minimum-total-images must be positive or disabled")
    if maximum_manual_images is not None and maximum_manual_images <= 0:
        raise ValueError("maximum-manual-images must be positive or disabled")
    if (
        minimum_total_images is not None
        and maximum_manual_images is not None
        and maximum_manual_images < minimum_total_images
    ):
        raise ValueError(
            "maximum-manual-images must be at least minimum-total-images "
            "when both cutoffs are enabled"
        )


def prepare(
    source: Path,
    alias_mappings: dict[str, str],
    minimum_total_images: int | None,
    maximum_manual_images: int | None,
    val_fraction: float,
    test_fraction: float,
    seed: str,
    mode: str,
    exclude_letter_suffix_ids: bool,
    show_progress: bool,
) -> Preparation:
    source_entries = sorted(
        (path.name for path in source.iterdir() if path.is_dir()), key=str.casefold
    )
    recognized_ids = [
        identity for identity in source_entries if ID_RE.fullmatch(identity)
    ]
    letter_suffix_ids = [
        identity
        for identity in recognized_ids
        if LETTER_SUFFIX_ID_RE.fullmatch(identity)
    ]
    valid_ids = [
        identity
        for identity in recognized_ids
        if not exclude_letter_suffix_ids
        or not LETTER_SUFFIX_ID_RE.fullmatch(identity)
    ]
    unknown_alias_ids = sorted(set(alias_mappings) - set(recognized_ids))
    if unknown_alias_ids:
        raise ValueError(
            "alias map contains IDs not present in the source: "
            + ", ".join(unknown_alias_ids)
        )
    active_alias_mappings = {
        observed: canonical
        for observed, canonical in alias_mappings.items()
        if observed in valid_ids
    }
    aliases, unresolved_alias_ids, resolved_alias_mappings = find_aliases(
        valid_ids, active_alias_mappings
    )
    scanned, rejected, manifest_errors, ids_without_manifest = scan_crops(
        source, valid_ids, resolved_alias_mappings, show_progress=show_progress
    )
    for crop in scanned:
        if crop.observed_id in unresolved_alias_ids:
            rejected.append(
                {
                    "id": crop.observed_id,
                    "source": crop.source_display,
                    "sha256": crop.sha256,
                    "reason": "ID has an unresolved naming alias",
                }
            )
    eligible_for_hash_checks = [
        crop for crop in scanned if crop.observed_id not in unresolved_alias_ids
    ]
    clean, same_id_duplicates, cross_id_conflicts = remove_hash_duplicates(
        eligible_for_hash_checks
    )

    candidate_ids = {
        resolved_alias_mappings.get(identity, identity)
        for identity in valid_ids
        if identity not in unresolved_alias_ids and identity not in ids_without_manifest
    }
    eligible, insufficient, balancing_details, balancing_omitted_images = (
        balance_crops(
            clean,
            candidate_ids,
            minimum_total_images,
            maximum_manual_images,
            seed,
        )
    )
    eligible = choose_output_names(eligible)
    assigned, split_details = assign_splits(
        eligible, val_fraction, test_fraction, seed
    )
    assigned.sort(
        key=lambda crop: (crop.split, crop.canonical_id, crop.output_name.casefold())
    )

    included_ids = sorted({crop.canonical_id for crop in assigned})
    image_counts = {
        detail["id"]: detail["image_counts"] for detail in split_details
    }
    excluded_ids: list[dict] = []
    if exclude_letter_suffix_ids:
        excluded_ids.extend(
            {
                "id": identity,
                "reason": "letter-suffixed offspring ID",
            }
            for identity in letter_suffix_ids
        )
    excluded_ids.extend(
        {"id": identity, "reason": "unresolved naming alias"}
        for identity in sorted(unresolved_alias_ids)
    )
    excluded_ids.extend(
        {"id": identity, "reason": "missing or empty crop manifest"}
        for identity in sorted(ids_without_manifest)
    )
    excluded_ids.extend(
        {"id": entry["id"], "reason": "insufficient clean images"}
        for entry in insufficient
    )
    excluded_ids.extend(
        {
            "id": detail["id"],
            "reason": "no clean manifest-backed crops",
        }
        for detail in balancing_details
        if detail["status"] == "excluded_no_clean_images"
    )
    excluded_ids.sort(key=lambda entry: (entry["id"], entry["reason"]))

    capped_details = [
        detail
        for detail in balancing_details
        if detail["status"] == "capped_manual_only"
    ]
    report: dict[str, Any] = {
        "format_version": 2,
        "configuration": {
            "source": str(source),
            "id_regex": ID_RE.pattern,
            "minimum_total_images": minimum_total_images,
            "maximum_manual_images": maximum_manual_images,
            "exclude_letter_suffix_ids": exclude_letter_suffix_ids,
            "validation_fraction": val_fraction,
            "test_fraction": test_fraction,
            "split_seed": seed,
            "materialization_mode": mode,
            "finprint": {
                "data_directory": "<output>",
                "dataset": "cropped_images",
                "replace_csvs": False,
            },
        },
        "summary": {
            "included_id_count": len(included_ids),
            "excluded_id_count": len({entry["id"] for entry in excluded_ids}),
            "included_image_count": len(assigned),
            "train_image_count": sum(crop.split == "train" for crop in assigned),
            "val_image_count": sum(crop.split == "val" for crop in assigned),
            "test_image_count": sum(crop.split == "test" for crop in assigned),
            "excluded_letter_suffix_id_count": (
                len(letter_suffix_ids) if exclude_letter_suffix_ids else 0
            ),
            "capped_id_count": len(capped_details),
            "manual_images_omitted_by_cap": sum(
                detail["omitted"]["manual"] for detail in capped_details
            ),
            "additional_images_omitted_by_cap": sum(
                detail["omitted"]["additional"] for detail in capped_details
            ),
            "unresolved_alias_group_count": sum(
                not alias["resolved"] for alias in aliases
            ),
            "resolved_alias_group_count": sum(
                alias["resolved"] for alias in aliases
            ),
            "automatically_resolved_alias_group_count": sum(
                alias["resolution"] == "automatic_zero_padding"
                for alias in aliases
            ),
            "same_id_duplicate_copy_count": sum(
                len(entry["excluded"]) for entry in same_id_duplicates
            ),
            "cross_id_conflict_hash_count": len(cross_id_conflicts),
            "cross_id_conflict_copy_count": sum(
                len(entry["copies"]) for entry in cross_id_conflicts
            ),
            "insufficient_sample_id_count": len(insufficient),
            "training_only_id_count": sum(
                detail["training_only"] for detail in split_details
            ),
        },
        "included_ids": included_ids,
        "excluded_ids": excluded_ids,
        "image_counts_per_id": image_counts,
        "excluded_letter_suffix_ids": (
            letter_suffix_ids if exclude_letter_suffix_ids else []
        ),
        "solved_aliases": [
            alias for alias in aliases if alias["resolved"]
        ],
        "unresolved_aliases": [
            alias for alias in aliases if not alias["resolved"]
        ],
        "naming_aliases": aliases,
        "same_id_duplicates": same_id_duplicates,
        "cross_id_conflicts": cross_id_conflicts,
        "ids_excluded_for_insufficient_samples": insufficient,
        "balancing_details": balancing_details,
        "balancing_omitted_images": balancing_omitted_images,
        "split_details": split_details,
        "rejected_crops": rejected,
        "manifest_errors": manifest_errors,
        "included_images": [
            {
                "id": crop.canonical_id,
                "observed_id": crop.observed_id,
                "source": crop.source_display,
                "output": f"cropped_images/{crop.canonical_id}/{crop.output_name}",
                "sha256": crop.sha256,
                "encounter": crop.encounter,
                "source_kind": crop.source_kind,
                "split": crop.split,
            }
            for crop in assigned
        ],
    }
    return Preparation(assigned, report)


def csv_rows(preparation: Preparation) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    report = preparation.report
    balancing_by_id = {
        detail["id"]: detail for detail in report["balancing_details"]
    }
    for identity in report["included_ids"]:
        counts = report["image_counts_per_id"][identity]
        rows.append(
            {
                "RecordType": "id",
                "ID": identity,
                "Status": "included",
                "Count": sum(counts.values()),
                "Details": json.dumps(
                    {
                        "splits": counts,
                        "balancing": balancing_by_id[identity],
                    },
                    sort_keys=True,
                ),
            }
        )
    for entry in report["excluded_ids"]:
        rows.append(
            {
                "RecordType": "id",
                "ID": entry["id"],
                "Status": "excluded",
                "Details": entry["reason"],
            }
        )
    for crop in preparation.crops:
        rows.append(
            {
                "RecordType": "image",
                "ID": crop.canonical_id,
                "Status": "included",
                "Count": 1,
                "SHA256": crop.sha256,
                "Source": crop.source_display,
                "Output": f"cropped_images/{crop.canonical_id}/{crop.output_name}",
                "Encounter": crop.encounter,
                "Split": crop.split,
                "Details": f"source_kind={crop.source_kind}",
            }
        )
    for omitted in report["balancing_omitted_images"]:
        rows.append(
            {
                "RecordType": "balancing_omission",
                "ID": omitted["id"],
                "Status": "excluded",
                "Count": 1,
                "SHA256": omitted["sha256"],
                "Source": omitted["source"],
                "Encounter": omitted["encounter"],
                "Details": (
                    f"source_kind={omitted['source_kind']}; "
                    f"reason={omitted['reason']}"
                ),
            }
        )
    for duplicate in report["same_id_duplicates"]:
        rows.append(
            {
                "RecordType": "same_id_duplicate",
                "ID": duplicate["id"],
                "Status": "excluded",
                "Count": len(duplicate["excluded"]),
                "SHA256": duplicate["sha256"],
                "Source": ";".join(duplicate["excluded"]),
                "Details": f"kept={duplicate['kept']}",
            }
        )
    for conflict in report["cross_id_conflicts"]:
        rows.append(
            {
                "RecordType": "cross_id_conflict",
                "ID": ";".join(conflict["ids"]),
                "Status": "excluded",
                "Count": len(conflict["copies"]),
                "SHA256": conflict["sha256"],
                "Source": ";".join(conflict["copies"]),
            }
        )
    for alias in report["naming_aliases"]:
        rows.append(
            {
                "RecordType": "naming_alias",
                "ID": ";".join(alias["ids"]),
                "Status": "resolved" if alias["resolved"] else "excluded",
                "Details": json.dumps(alias, sort_keys=True),
            }
        )
    return rows


def link_or_copy(source: Path, destination: Path, mode: str) -> None:
    if mode == "symlink":
        destination.symlink_to(source.resolve())
    elif mode == "hardlink":
        os.link(source, destination)
    else:
        shutil.copy2(source, destination)


def write_dataset(
    preparation: Preparation, output: Path, mode: str, *, show_progress: bool
) -> None:
    output_parent = output.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.preparing-", dir=output_parent)
    )
    try:
        dataset_dir = staging / "cropped_images"
        dataset_dir.mkdir()
        split_rows: dict[str, list[dict[str, str]]] = {
            "train": [],
            "val": [],
            "test": [],
        }
        action = {
            "symlink": "Creating symlinks",
            "hardlink": "Creating hard links",
            "copy": "Copying crops",
        }[mode]
        progress = Progress(action, len(preparation.crops), enabled=show_progress)
        for crop in preparation.crops:
            destination = dataset_dir / crop.canonical_id / crop.output_name
            destination.parent.mkdir(exist_ok=True)
            link_or_copy(crop.source, destination, mode)
            progress.advance()
            split_rows[crop.split].append(
                {
                    "Label": crop.canonical_id,
                    "Filepath": f"/{crop.canonical_id}/{crop.output_name}",
                    "Root": f"{crop.canonical_id}__enc_{hashlib.sha256(crop.encounter.encode()).hexdigest()[:16]}",
                }
            )
        progress.finish()
        for split, rows in split_rows.items():
            with (dataset_dir / f"{split}.csv").open(
                "w", encoding="utf-8", newline=""
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)

        (staging / "preparation_report.json").write_text(
            json.dumps(preparation.report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with (staging / "preparation_report.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=REPORT_CSV_COLUMNS)
            writer.writeheader()
            for row in csv_rows(preparation):
                writer.writerow(row)
        staging.rename(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def ensure_paths(source: Path, output: Path) -> tuple[Path, Path]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"source directory does not exist: {source}")
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("source and output directory trees must not overlap")
    if output.exists():
        raise FileExistsError(f"output already exists; choose a new path: {output}")
    return source, output


def print_alias_summary(aliases: list[dict]) -> None:
    solved = [alias for alias in aliases if alias["resolved"]]
    if solved:
        print("Solved aliases:", file=sys.stderr)
        for alias in solved:
            resolution = alias["resolution"].replace("_", " ")
            print(
                f"  {', '.join(alias['ids'])} -> {alias['canonical_id']} "
                f"({resolution})",
                file=sys.stderr,
            )

    unresolved = [alias for alias in aliases if not alias["resolved"]]
    if unresolved:
        print("Unresolved aliases (excluded):", file=sys.stderr)
        for alias in unresolved:
            print(f"  {', '.join(alias['ids'])}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare manifest-backed, encounter-aware FinPrintv2 identification data."
    )
    parser.add_argument("output", type=Path, help="new output dataset directory")
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"cluster root (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--alias-map",
        type=Path,
        help="JSON override for automatically resolved zero-padding aliases",
    )
    parser.add_argument(
        "--mode",
        choices=("symlink", "hardlink", "copy"),
        default=DEFAULT_MODE,
        help=f"how to materialize crops (default: {DEFAULT_MODE})",
    )
    minimum_group = parser.add_mutually_exclusive_group()
    minimum_group.add_argument(
        "--minimum-total-images",
        "--minimum-images",
        dest="minimum_total_images",
        type=int,
        default=DEFAULT_MINIMUM_TOTAL_IMAGES,
        help=(
            "exclude IDs with fewer clean manual+additional crops "
            f"(default: {DEFAULT_MINIMUM_TOTAL_IMAGES})"
        ),
    )
    minimum_group.add_argument(
        "--no-minimum-total-images",
        dest="minimum_total_images",
        action="store_const",
        const=None,
        help="disable the minimum total-image cutoff",
    )
    maximum_group = parser.add_mutually_exclusive_group()
    maximum_group.add_argument(
        "--maximum-manual-images",
        dest="maximum_manual_images",
        type=int,
        default=DEFAULT_MAXIMUM_MANUAL_IMAGES,
        help=(
            "when exceeded, keep only this many manual crops "
            f"(default: {DEFAULT_MAXIMUM_MANUAL_IMAGES})"
        ),
    )
    maximum_group.add_argument(
        "--no-maximum-manual-images",
        dest="maximum_manual_images",
        action="store_const",
        const=None,
        help="disable the maximum manual-image cutoff",
    )
    suffix_group = parser.add_mutually_exclusive_group()
    suffix_group.add_argument(
        "--exclude-letter-suffix-ids",
        dest="exclude_letter_suffix_ids",
        action="store_true",
        help="exclude NKW IDs ending in a letter (default)",
    )
    suffix_group.add_argument(
        "--include-letter-suffix-ids",
        dest="exclude_letter_suffix_ids",
        action="store_false",
        help="include letter-suffixed NKW IDs as distinct individuals",
    )
    parser.set_defaults(
        exclude_letter_suffix_ids=DEFAULT_EXCLUDE_LETTER_SUFFIX_IDS
    )
    parser.add_argument("--val-fraction", type=float, default=0.15)
    parser.add_argument("--test-fraction", type=float, default=0.15)
    parser.add_argument(
        "--split-seed",
        default="finprint-encounter-v1",
        help="deterministic encounter ordering seed",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan, hash, and report the plan without writing output",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="suppress hashing and output progress indicators",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_cutoffs(
            args.minimum_total_images,
            args.maximum_manual_images,
        )
        validate_fractions(args.val_fraction, args.test_fraction)
        source, output = ensure_paths(args.source, args.output)
        mappings = read_alias_map(args.alias_map)
        preparation = prepare(
            source,
            mappings,
            args.minimum_total_images,
            args.maximum_manual_images,
            args.val_fraction,
            args.test_fraction,
            args.split_seed,
            args.mode,
            args.exclude_letter_suffix_ids,
            not args.no_progress,
        )
        print_alias_summary(preparation.report["naming_aliases"])
        if args.dry_run:
            configuration = preparation.report["configuration"]
            print(
                json.dumps(
                    {
                        "configuration": {
                            "source": configuration["source"],
                            "minimum_total_images": configuration[
                                "minimum_total_images"
                            ],
                            "maximum_manual_images": configuration[
                                "maximum_manual_images"
                            ],
                            "exclude_letter_suffix_ids": configuration[
                                "exclude_letter_suffix_ids"
                            ],
                            "materialization_mode": configuration[
                                "materialization_mode"
                            ],
                        },
                        "summary": preparation.report["summary"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            print(f"Dry run: no output written to {output}")
            return 0
        write_dataset(
            preparation, output, args.mode, show_progress=not args.no_progress
        )
        summary = preparation.report["summary"]
        print(
            f"Prepared {summary['included_image_count']} crops for "
            f"{summary['included_id_count']} IDs in {output}"
        )
        print(f"Reports: {output / 'preparation_report.json'} and {output / 'preparation_report.csv'}")
        print("FinPrintv2: set data_directory to this output, dataset to cropped_images, and replace_csvs to false.")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

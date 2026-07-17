from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "prepare_finprint_dataset.py"
SPEC = importlib.util.spec_from_file_location("prepare_finprint_dataset_tool", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)


def crop(
    identity: str,
    number: int,
    *,
    source_kind: str,
    encounter: str,
    digest: str | None = None,
) -> tool.Crop:
    name = f"{source_kind}__{identity}_{number:03d}_fin.jpg"
    return tool.Crop(
        observed_id=identity,
        canonical_id=identity,
        source=Path("/tmp") / name,
        source_display=f"{identity}/cropped/{name}",
        sha256=digest or f"{number:064x}",
        encounter=encounter,
        source_kind=source_kind,
        manifest_line=number + 1,
    )


def write_cluster_id(
    root: Path,
    identity: str,
    records: list[tuple[str, str]],
) -> None:
    crop_dir = root / identity / "cropped"
    crop_dir.mkdir(parents=True)
    manifest_lines: list[str] = []
    for index, (source_kind, encounter) in enumerate(records):
        name = f"{source_kind}__{identity}_{index:03d}_fin.jpg"
        (crop_dir / name).write_bytes(
            b"\xff\xd8\xff" + f"{identity}-{source_kind}-{index}".encode()
        )
        manifest_lines.append(
            json.dumps(
                {
                    "crop_file": name,
                    "source_kind": source_kind,
                    "encounter_id": encounter,
                    "source_path": f"/source/{identity}/{name}",
                }
            )
        )
    (crop_dir / "crop_manifest.jsonl").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8"
    )


class BalancingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = "NKW-100"
        self.crops = [
            crop(
                self.identity,
                index,
                source_kind="manual",
                encounter=f"burst-{index % 3}",
            )
            for index in range(4)
        ] + [
            crop(
                self.identity,
                index + 10,
                source_kind="additional",
                encounter=f"burst-{index}",
            )
            for index in range(2)
        ]

    def balance(
        self, minimum: int | None, maximum: int | None
    ) -> tuple[list[tool.Crop], list[dict], list[dict], list[dict]]:
        return tool.balance_crops(
            self.crops,
            {self.identity},
            minimum,
            maximum,
            "test-seed",
        )

    def test_both_cutoffs(self) -> None:
        kept, insufficient, details, omitted = self.balance(3, 3)
        self.assertEqual(3, len(kept))
        self.assertTrue(all(item.source_kind == "manual" for item in kept))
        self.assertFalse(insufficient)
        self.assertEqual("capped_manual_only", details[0]["status"])
        self.assertEqual(3, len(omitted))

    def test_minimum_only(self) -> None:
        kept, _, details, omitted = self.balance(3, None)
        self.assertEqual(6, len(kept))
        self.assertEqual("unchanged", details[0]["status"])
        self.assertFalse(omitted)

    def test_maximum_only(self) -> None:
        kept, _, _, _ = self.balance(None, 3)
        self.assertEqual(3, len(kept))
        self.assertTrue(all(item.source_kind == "manual" for item in kept))

    def test_neither_cutoff(self) -> None:
        kept, _, _, omitted = self.balance(None, None)
        self.assertEqual(6, len(kept))
        self.assertFalse(omitted)

    def test_minimum_excludes_only_below_boundary(self) -> None:
        kept, insufficient, details, _ = self.balance(7, None)
        self.assertFalse(kept)
        self.assertEqual(self.identity, insufficient[0]["id"])
        self.assertEqual("excluded_insufficient", details[0]["status"])

        kept, insufficient, _, _ = self.balance(6, None)
        self.assertEqual(6, len(kept))
        self.assertFalse(insufficient)

    def test_exact_manual_maximum_does_not_cap(self) -> None:
        exact = self.crops[:3] + self.crops[4:]
        kept, _, details, omitted = tool.balance_crops(
            exact,
            {self.identity},
            None,
            3,
            "test-seed",
        )
        self.assertEqual(5, len(kept))
        self.assertEqual("unchanged", details[0]["status"])
        self.assertFalse(omitted)

    def test_manual_selection_maximizes_burst_diversity(self) -> None:
        manual = [
            crop(self.identity, 1, source_kind="manual", encounter="burst-a"),
            crop(self.identity, 2, source_kind="manual", encounter="burst-a"),
            crop(self.identity, 3, source_kind="manual", encounter="burst-a"),
            crop(self.identity, 4, source_kind="manual", encounter="burst-b"),
            crop(self.identity, 5, source_kind="manual", encounter="burst-b"),
            crop(self.identity, 6, source_kind="manual", encounter="burst-c"),
        ]
        first = tool.select_burst_diverse_manual_crops(
            self.identity, manual, 3, "test-seed"
        )
        second = tool.select_burst_diverse_manual_crops(
            self.identity, manual, 3, "test-seed"
        )
        self.assertEqual(3, len({item.encounter for item in first}))
        self.assertEqual(first, second)


class ManifestAndIdentityTests(unittest.TestCase):
    def test_zero_padding_aliases_resolve_to_padded_nkw_id(self) -> None:
        aliases, unresolved, mappings = tool.find_aliases(
            ["NKW-074", "NKW-74"], {}
        )
        self.assertFalse(unresolved)
        self.assertEqual("NKW-074", aliases[0]["canonical_id"])
        self.assertEqual(
            "automatic_zero_padding", aliases[0]["resolution"]
        )
        self.assertEqual(
            {"NKW-074": "NKW-074", "NKW-74": "NKW-074"},
            mappings,
        )

        explicit, unresolved, mappings = tool.find_aliases(
            ["NKW-074", "NKW-74"],
            {"NKW-074": "NKW-74"},
        )
        self.assertFalse(unresolved)
        self.assertEqual("NKW-74", explicit[0]["canonical_id"])
        self.assertEqual("explicit", explicit[0]["resolution"])
        self.assertEqual("NKW-74", mappings["NKW-074"])
        self.assertEqual("NKW-74", mappings["NKW-74"])

    def test_solved_aliases_are_printed(self) -> None:
        aliases, _, _ = tool.find_aliases(["NKW-074", "NKW-74"], {})
        output = io.StringIO()
        with redirect_stderr(output):
            tool.print_alias_summary(aliases)
        self.assertIn("Solved aliases:", output.getvalue())
        self.assertIn(
            "NKW-074, NKW-74 -> NKW-074 (automatic zero padding)",
            output.getvalue(),
        )

    def test_prepare_merges_zero_padding_alias_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            write_cluster_id(
                source,
                "NKW-074",
                [
                    ("manual", "burst-a"),
                    ("manual", "burst-b"),
                ],
            )
            write_cluster_id(
                source,
                "NKW-74",
                [
                    ("manual", "burst-c"),
                    ("additional", "burst-c"),
                    ("additional", "burst-d"),
                ],
            )
            preparation = tool.prepare(
                source,
                {},
                None,
                None,
                0,
                0,
                "seed",
                "copy",
                True,
                False,
            )
            self.assertEqual(["NKW-074"], preparation.report["included_ids"])
            self.assertEqual(5, len(preparation.crops))
            self.assertEqual(
                1,
                preparation.report["summary"][
                    "automatically_resolved_alias_group_count"
                ],
            )
            self.assertEqual(
                1,
                preparation.report["summary"]["resolved_alias_group_count"],
            )
            self.assertEqual(
                [
                    {
                        "ids": ["NKW-074", "NKW-74"],
                        "resolved": True,
                        "canonical_id": "NKW-074",
                        "resolution": "automatic_zero_padding",
                        "explicit_mappings": {},
                    }
                ],
                preparation.report["solved_aliases"],
            )
            self.assertEqual([], preparation.report["unresolved_aliases"])
            self.assertTrue(
                all(
                    item.canonical_id == "NKW-074"
                    for item in preparation.crops
                )
            )

    def test_manifest_burst_has_priority_and_folder_is_fallback(self) -> None:
        record = {
            "encounter_id": "burst-123",
            "source_kind": "manual",
            "source_path": "/source/folder/image.jpg",
        }
        self.assertEqual("burst-123", tool.encounter_from_manifest(record))
        record["encounter_id"] = None
        self.assertEqual(
            "fallback-folder:/source/folder",
            tool.encounter_from_manifest(record),
        )

    def test_manual_wins_same_id_hash_deduplication(self) -> None:
        additional = crop(
            "NKW-100",
            1,
            source_kind="additional",
            encounter="burst-a",
            digest="a" * 64,
        )
        manual = crop(
            "NKW-100",
            2,
            source_kind="manual",
            encounter="burst-a",
            digest="a" * 64,
        )
        clean, duplicates, conflicts = tool.remove_hash_duplicates(
            [additional, manual]
        )
        self.assertEqual("manual", clean[0].source_kind)
        self.assertEqual(manual.source_display, duplicates[0]["kept"])
        self.assertFalse(conflicts)

    def test_suffix_ids_are_excluded_or_included(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            write_cluster_id(source, "NKW-100", [("manual", "burst-a")])
            write_cluster_id(source, "NKW-100a", [("manual", "burst-b")])

            excluded = tool.prepare(
                source,
                {},
                None,
                None,
                0,
                0,
                "seed",
                "copy",
                True,
                False,
            )
            self.assertEqual(["NKW-100"], excluded.report["included_ids"])
            self.assertEqual(
                ["NKW-100a"],
                excluded.report["excluded_letter_suffix_ids"],
            )

            included = tool.prepare(
                source,
                {},
                None,
                None,
                0,
                0,
                "seed",
                "copy",
                False,
                False,
            )
            self.assertEqual(
                ["NKW-100", "NKW-100a"],
                included.report["included_ids"],
            )

    def test_each_burst_is_assigned_to_only_one_split(self) -> None:
        items = [
            crop(
                "NKW-100",
                index,
                source_kind="manual",
                encounter=f"burst-{index // 2}",
            )
            for index in range(8)
        ]
        assigned, _ = tool.assign_splits(items, 0.25, 0.25, "seed")
        by_encounter: dict[str, set[str]] = {}
        for item in assigned:
            by_encounter.setdefault(item.encounter, set()).add(item.split)
        self.assertTrue(
            all(len(splits) == 1 for splits in by_encounter.values())
        )


class InterfaceAndOutputTests(unittest.TestCase):
    def test_parser_defaults_and_disable_flags(self) -> None:
        parser = tool.build_parser()
        defaults = parser.parse_args(["output"])
        self.assertEqual(tool.DEFAULT_SOURCE, defaults.source)
        self.assertEqual(10, defaults.minimum_total_images)
        self.assertEqual(30, defaults.maximum_manual_images)
        self.assertEqual("copy", defaults.mode)
        self.assertTrue(defaults.exclude_letter_suffix_ids)

        disabled = parser.parse_args(
            [
                "output",
                "--no-minimum-total-images",
                "--no-maximum-manual-images",
                "--include-letter-suffix-ids",
            ]
        )
        self.assertIsNone(disabled.minimum_total_images)
        self.assertIsNone(disabled.maximum_manual_images)
        self.assertFalse(disabled.exclude_letter_suffix_ids)

        alias = parser.parse_args(["output", "--minimum-images", "12"])
        self.assertEqual(12, alias.minimum_total_images)

    def test_invalid_cutoffs(self) -> None:
        for minimum, maximum in [(0, None), (None, 0), (10, 9)]:
            with self.subTest(minimum=minimum, maximum=maximum):
                with self.assertRaises(ValueError):
                    tool.validate_cutoffs(minimum, maximum)

    def test_report_tracks_cap_omissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            write_cluster_id(
                source,
                "NKW-100",
                [
                    ("manual", "burst-a"),
                    ("manual", "burst-b"),
                    ("manual", "burst-c"),
                    ("manual", "burst-d"),
                    ("additional", "burst-a"),
                    ("additional", "burst-b"),
                ],
            )
            preparation = tool.prepare(
                source,
                {},
                None,
                3,
                0,
                0,
                "seed",
                "copy",
                True,
                False,
            )
            summary = preparation.report["summary"]
            self.assertEqual(1, summary["capped_id_count"])
            self.assertEqual(1, summary["manual_images_omitted_by_cap"])
            self.assertEqual(2, summary["additional_images_omitted_by_cap"])
            self.assertEqual(3, len(preparation.crops))
            self.assertEqual(
                3, len(preparation.report["balancing_omitted_images"])
            )

    def test_default_materialization_creates_regular_copies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "output"
            write_cluster_id(source, "NKW-100", [("manual", "burst-a")])
            preparation = tool.prepare(
                source,
                {},
                None,
                None,
                0,
                0,
                "seed",
                "copy",
                True,
                False,
            )
            tool.write_dataset(
                preparation,
                output,
                tool.DEFAULT_MODE,
                show_progress=False,
            )
            copied = next((output / "cropped_images" / "NKW-100").glob("*.jpg"))
            self.assertTrue(copied.is_file())
            self.assertFalse(copied.is_symlink())


if __name__ == "__main__":
    unittest.main()

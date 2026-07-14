import tempfile
import unittest
import re
from pathlib import Path

from gps_screen_reader import Coordinate, discover_images, parse_coordinate, write_results


class CoordinateParsingTests(unittest.TestCase):
    def test_parses_sample_screen_text(self):
        coordinate = parse_coordinate("POS\nN 69°19.894'\nE 15°40.842'\nDTD NM")

        self.assertIsNotNone(coordinate)
        self.assertEqual(coordinate.display, "N 69°19.894' / E 15°40.842'")
        self.assertAlmostEqual(coordinate.latitude_decimal, 69.3315667, places=7)
        self.assertAlmostEqual(coordinate.longitude_decimal, 15.6807, places=7)

    def test_tolerates_common_ocr_substitutions(self):
        coordinate = parse_coordinate("N 69 19,894\nE I5 4O.842")

        self.assertIsNotNone(coordinate)
        self.assertEqual(coordinate.longitude_degrees, 15)
        self.assertEqual(coordinate.longitude_minutes, 40.842)

    def test_rejects_invalid_minutes(self):
        self.assertIsNone(parse_coordinate("N 69°61.000'\nE 15°40.842'"))


class FileHandlingTests(unittest.TestCase):
    def test_discovery_is_recursive_and_case_insensitive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            excluded = root / "converted"
            nested.mkdir()
            excluded.mkdir()
            (nested / "screen.NEF").touch()
            (nested / "photo.JPG").touch()
            (nested / "notes.txt").touch()
            (excluded / "generated.jpg").touch()

            found = discover_images(root, excluded)

            self.assertEqual([path.name for path in found], ["photo.JPG", "screen.NEF"])

    def test_discovery_filters_by_filename_pattern(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "240101_screen.NEF").touch()
            (root / "other.NEF").touch()

            found = discover_images(root, filename_pattern=re.compile(r"^\d{6}_", re.IGNORECASE))

            self.assertEqual([path.name for path in found], ["240101_screen.NEF"])

    def test_results_group_duplicate_position_in_one_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            folder = root / "trip"
            folder.mkdir()
            first = folder / "one.NEF"
            second = folder / "two.NEF"
            first.touch()
            second.touch()
            coordinate = Coordinate("N", 69, 19.894, "E", 15, 40.842)
            output = root / "positions.txt"

            rows = write_results(output, root, [(first, coordinate), (second, coordinate)])

            self.assertEqual(rows, 1)
            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("one.NEF; trip/two.NEF", lines[1])


if __name__ == "__main__":
    unittest.main()

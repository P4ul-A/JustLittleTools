import hashlib
import tempfile
import unittest
from pathlib import Path

from flip_nkw_filenames import apply_renames, find_renames, flipped_name


class FilenameTests(unittest.TestCase):
    def test_flips_sample_jpeg_name(self):
        self.assertEqual(
            flipped_name(Path("Photo_15_NKW-460.jpg")),
            "NKW-460_Photo_15.jpg",
        )

    def test_avoids_double_underscore_and_preserves_raw_extension(self):
        self.assertEqual(
            flipped_name(Path("_MAB0157_NKW-321.NEF")),
            "NKW-321_MAB0157.NEF",
        )

    def test_skips_unsupported_or_already_flipped_names(self):
        self.assertIsNone(flipped_name(Path("notes_NKW-123.txt")))
        self.assertIsNone(flipped_name(Path("NKW-123_Photo_1.jpg")))


class RenameTests(unittest.TestCase):
    def test_recurses_over_files_without_renaming_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "folder_NKW-999"
            directory.mkdir()
            source = directory / "Photo_1_NKW-608.CR2"
            contents = b"pretend raw bytes\x00\x01"
            source.write_bytes(contents)
            checksum_before = hashlib.sha256(contents).hexdigest()

            renames = find_renames(root)
            apply_renames(renames)

            destination = directory / "NKW-608_Photo_1.CR2"
            self.assertTrue(directory.is_dir())
            self.assertTrue(destination.is_file())
            self.assertFalse(source.exists())
            self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), checksum_before)

    def test_detects_conflict_before_renaming(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Photo_1_NKW-608.jpg"
            destination = root / "NKW-608_Photo_1.jpg"
            source.write_bytes(b"source")
            destination.write_bytes(b"existing")

            with self.assertRaises(FileExistsError):
                apply_renames(find_renames(root))

            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(destination.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()

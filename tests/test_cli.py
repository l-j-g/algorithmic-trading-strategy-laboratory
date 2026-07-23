import tempfile
import unittest
from pathlib import Path

from ats_lab.cli import discover_lab_repo


class DiscoverLabRepoTests(unittest.TestCase):
    def test_finds_lab_root_from_nested_package_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ats-lab").mkdir()
            (root / ".ats-lab" / "config.toml").write_text("")
            nested = root / "src" / "ats_lab"
            nested.mkdir(parents=True)

            self.assertEqual(discover_lab_repo(nested), root.resolve())

    def test_keeps_start_directory_when_no_config_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            start = Path(tmp)

            self.assertEqual(discover_lab_repo(start), start.resolve())


if __name__ == "__main__":
    unittest.main()

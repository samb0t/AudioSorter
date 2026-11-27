"""requirements-release.lock must stay in step with the pins it was generated from."""
import re
import unittest

from . import ROOT


def locked():
    """{name: version} from requirements-release.lock, names normalised as pip does."""
    text = (ROOT / "requirements-release.lock").read_text()
    return {re.sub(r"[-_.]+", "-", name).lower(): version
            for name, version in re.findall(r"^([A-Za-z0-9_.-]+)==(\S+) \\$", text, re.M)}


class LockTests(unittest.TestCase):
    def test_every_pin_is_locked_at_its_version(self):
        lock = locked()
        for file in ("requirements.txt", "requirements-detect.txt"):
            for name, version in re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)$", (ROOT / file).read_text(), re.M):
                with self.subTest(package=name):
                    self.assertEqual(lock.get(re.sub(r"[-_.]+", "-", name).lower()), version,
                                     f"{file} pins {name}=={version}; run zsh tools/lock_deps.sh")

    def test_every_package_has_hashes(self):
        # Each "name==version \" line must be followed by at least one hash.
        text = (ROOT / "requirements-release.lock").read_text()
        entries = re.findall(r"^([A-Za-z0-9_.-]+)==\S+ \\\n((?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", text, re.M)
        self.assertEqual({re.sub(r"[-_.]+", "-", name).lower() for name, _ in entries}, set(locked()))


if __name__ == "__main__":
    unittest.main()

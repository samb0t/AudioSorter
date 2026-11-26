"""Check a release build (zsh build.sh --release) the way a tester's Mac runs it: a copy
outside this folder, an empty environment, and nothing but what the bundle carries.

Opt-in, since it needs a built app:
AUDIO_SORTER_RELEASE_APP=".build/release/Audio Sorter.app" .venv/bin/python -m unittest -v test_release
"""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest

from test_detect import assert_house_loop_grid, house_loop

APP = os.environ.get("AUDIO_SORTER_RELEASE_APP")


@unittest.skipUnless(APP, "set AUDIO_SORTER_RELEASE_APP to a release build")
class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        # realpath: /var is a symlink to /private/var, and Python reports resolved paths.
        cls.root = Path(os.path.realpath(cls.work.name))
        cls.app = cls.root / "Audio Sorter.app"
        subprocess.run(["ditto", APP, str(cls.app)], check=True)
        cls.resources = cls.app / "Contents" / "Resources"

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def python(self, *arguments, input=None):
        # Only what Sorter.helper adds, on top of an otherwise empty environment.
        environment = {"HOME": str(self.root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        return subprocess.run([str(self.resources / "python" / "bin" / "python3"), *arguments],
                              input=input, env=environment, capture_output=True, text=True)

    def test_signed_and_self_contained(self):
        result = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(self.app)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("python-path", "config-path", "detect-model"):
            self.assertFalse((self.resources / name).exists(), f"{name} points outside the bundle")
        for name in ("comments.py", "edit.py", "rekordbox.py", "detect.py", "config.example.json", "beat_this/final0.ckpt"):
            self.assertTrue((self.resources / name).is_file(), name)
        info = plistlib.loads((self.app / "Contents" / "Info.plist").read_bytes())
        self.assertEqual(info["CFBundleIdentifier"], "com.bottonimusic.audiosorter")
        self.assertEqual(info["LSMinimumSystemVersion"], "14.0")

    def test_python_uses_only_the_bundle(self):
        script = "import json, sys; import mutagen, numpy, torch, beat_this.inference; print(json.dumps([sys.prefix, *sys.path]))"
        result = self.python("-c", script)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in filter(None, json.loads(result.stdout)):
            self.assertTrue(os.path.realpath(path).startswith(str(self.resources)), path)

    def test_detection_through_the_helper(self):
        path = self.root / "loop.wav"
        house_loop(path)
        request = json.dumps({"model": str(self.resources / "beat_this" / "final0.ckpt")})
        result = self.python(str(self.resources / "comments.py"), "detect", str(path), input=request)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        assert_house_loop_grid(self, json.loads(result.stdout))

    def test_tags_through_the_helper(self):
        path = self.root / "tagged.wav"
        house_loop(path)
        result = self.python(str(self.resources / "comments.py"), "write", str(path), input="Bundled comment")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.python(str(self.resources / "comments.py"), "read", str(path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["comment"], "Bundled comment")


if __name__ == "__main__":
    unittest.main()

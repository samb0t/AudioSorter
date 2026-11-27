"""Tests for Audio Sorter. Run from the repo root: .venv/bin/python -m unittest"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
# The Swift app and the Python helpers it bundles; the helpers import each other by name.
APP = ROOT / "app"
sys.path.insert(0, str(APP))

# -*- coding: utf-8 -*-
"""pytest bootstrap for dsh-pet-pure.

Only what the pure pet needs, nothing else:
- offscreen Qt platform (no window is ever needed by these tests);
- imports of `pet.*` and `main` resolve from the project root.

The suite deliberately spawns no ffmpeg for most tests: catalog/library naming
checks are pure; the GUI smoke test plays a real clip only via short-run clips.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Shared webm assets from the retired project's checkout (real machine).
SHARED_ASSETS = ROOT.parent / "dsh-pet-indesktop" / "assets" / "characters"
if SHARED_ASSETS.is_dir():
    os.environ.setdefault("DSH_PET_ASSETS", str(SHARED_ASSETS))


@pytest.fixture(scope="session")
def app():
    """A single QApplication for the whole session (offscreen)."""
    from PySide6.QtWidgets import QApplication
    existing = QApplication.instance()
    return existing if existing is not None else QApplication(sys.argv)


@pytest.fixture(scope="session")
def assets():
    return SHARED_ASSETS / "shenshen" / "videos"

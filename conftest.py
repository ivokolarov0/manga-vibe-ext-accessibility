"""Run the tests against a MangaVibe checkout (default: ../manga-vibe next to this folder, or set
MANGAVIBE_DIR). Uses MangaVibe's own test setup: a temporary data folder and a small generated library."""
import os
import sys
from pathlib import Path

APP_DIR = Path(os.environ.get("MANGAVIBE_DIR") or Path(__file__).parent.parent / "manga-vibe").resolve()
if not (APP_DIR / "app" / "plugins" / "__init__.py").exists():
    raise SystemExit(f"MangaVibe not found at {APP_DIR}. Set MANGAVIBE_DIR to your checkout.")
sys.path.insert(0, str(APP_DIR))

from tests.conftest import TMP, client  # noqa: E402,F401

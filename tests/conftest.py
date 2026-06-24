"""Shared pytest config: put the repo root on sys.path so `import agent...`
works without a pip install -e ."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

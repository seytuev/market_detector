"""Run shared chart regressions in every normal test/CI run."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_shared_zone_display():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests/zone_display_check.cjs")],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr

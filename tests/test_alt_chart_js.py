"""Проекция графика альткоинов считается в одном JS-модуле.

Отдельная Python-копия разошлась бы с экраном, поэтому pytest вызывает node.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_alt_chart_projection_matches_spec():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не установлен")
    script = ROOT / "tests" / "alt_chart_check.js"
    proc = subprocess.run(
        [node, str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr

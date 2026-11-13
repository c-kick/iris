"""The pinned request area retains its buttons across polling updates."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_pinned_confirmation_lifecycle():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('sticky-confirm.test.mjs'))],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr

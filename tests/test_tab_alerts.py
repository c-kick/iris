"""Exercise the tab alert lifecycle with Node's deterministic DOM and timer doubles."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_tab_alert_lifecycle():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("tab-alerts.test.mjs"))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

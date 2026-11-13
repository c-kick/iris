"""Exercise chat opening and refresh races with the production JavaScript."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_chat_navigation_races():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("chat-navigation.test.mjs"))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_quote_navigation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("chat-quote.test.mjs"))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

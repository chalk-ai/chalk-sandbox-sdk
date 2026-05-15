"""Helper function for test_function_secrets.py — reads an env var and returns it."""

from __future__ import annotations

import os


def read_secret(var_name: str) -> str:
    return os.environ.get(var_name, "<NOT SET>")

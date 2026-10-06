"""Shared invalid-UTF-8 config payload for the test suite (#2554).

This module has no ``test_`` prefix, so pytest does not collect it (same
convention as ``tests/_clients_yaml.py``); test modules import it explicitly.

The ASCII ``s3cr3t-marker`` line lets a test assert that file contents never
reach an error message; the trailing ``\\xff\\xfe`` bytes are not valid UTF-8.
"""

from __future__ import annotations

INVALID_UTF8 = b"token: s3cr3t-marker\n\xff\xfe\x00\n"

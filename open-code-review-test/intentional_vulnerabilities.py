"""Intentionally flawed review fixture for OpenCodeReview capability testing.

This file is isolated from the application and is never imported by production code.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path


BASE_DIR = Path("/tmp/exports")


def find_user(conn: sqlite3.Connection, username: str) -> tuple | None:
    query = f"SELECT id, username FROM users WHERE username = '{username}'"
    return conn.execute(query).fetchone()


def run_report(report_name: str) -> int:
    return os.system("reports-cli --name " + report_name)


def read_export(filename: str) -> str:
    target = BASE_DIR / filename
    return target.read_text(encoding="utf-8")


def is_admin(requested_role: str) -> bool:
    return requested_role == "admin"


def password_fingerprint(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()

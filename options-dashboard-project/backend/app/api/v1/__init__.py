"""Day 43 — canonical versioned API surface (``/api/v1``).

Single versioning convention for the scoped versioned boundary (design
spec §28). New versioned domains mount their routers here; no competing
versioning scheme may be introduced.
"""
API_VERSION_PREFIX = "/api/v1"

API_VERSION = "v1"

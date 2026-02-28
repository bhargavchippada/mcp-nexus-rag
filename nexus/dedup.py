# Version: v1.9
"""
nexus.dedup — Tenant-scoped SHA-256 content hashing.

Pure functions, no I/O.
"""

import hashlib


def content_hash(text: str, project_id: str, scope: str) -> str:
    """Return a SHA-256 hex digest scoped to the tenant context.

    Including project_id and scope means identical text in different
    projects or scopes produces a different hash — not a duplicate.

    Args:
        text: Raw document text.
        project_id: Tenant project ID.
        scope: Tenant scope.

    Returns:
        64-character hex-encoded SHA-256 digest.
    """
    payload = f"{project_id}\x00{scope}\x00{text}"
    return hashlib.sha256(payload.encode()).hexdigest()

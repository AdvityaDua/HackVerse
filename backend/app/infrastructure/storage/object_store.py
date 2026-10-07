"""Asset storage behind a port.

We author the campus map ourselves for the pilots, so nothing is uploaded and
no object storage is provisioned. This file is deliberately just the boundary:
one interface plus a local adapter, so that moving to R2/S3 later is a new
class and a config value rather than a rewrite.

``validate_asset`` is the part that earns its keep today. It validates the
authored map at registration time, and it is the fix for the original upload
hole: any file with any extension and any content was previously written under
a client-supplied name and served from the app's own origin, which made
``payload.html`` a same-origin script-execution vector.
"""

from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.infrastructure.config import Settings

#: Extension -> permitted content types. Anything else is rejected outright.
ALLOWED_ASSET_TYPES: dict[str, frozenset[str]] = {
    ".json": frozenset({"application/json", "text/json", "text/plain"}),
    ".png": frozenset({"image/png"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".webp": frozenset({"image/webp"}),
}


class UploadRejected(ValueError):
    """Raised when an asset fails validation. Surfaces as a 400."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    url: str
    size_bytes: int


def validate_asset(
    *,
    filename: str | None,
    content_type: str | None,
    content: bytes,
    settings: Settings,
    allowed_extensions: frozenset[str] | None = None,
) -> str:
    """Validate an asset and return the safe extension to store it under."""
    if not content:
        raise UploadRejected("File is empty.")
    if len(content) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes // (1024 * 1024)
        raise UploadRejected(f"File is larger than the {limit_mb} MB limit.")

    extension = os.path.splitext(filename or "")[1].lower()
    permitted = allowed_extensions or frozenset(ALLOWED_ASSET_TYPES)
    if extension not in permitted:
        raise UploadRejected(
            f"Unsupported file type '{extension or 'unknown'}'. "
            f"Allowed: {', '.join(sorted(permitted))}."
        )

    expected_types = ALLOWED_ASSET_TYPES[extension]
    if content_type and content_type.split(";")[0].strip().lower() not in expected_types:
        raise UploadRejected(f"'{content_type}' does not match a {extension} file.")

    if extension == ".json":
        try:
            json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UploadRejected("File is not valid JSON.") from exc

    return extension


class ObjectStore(ABC):
    """The seam. Add an S3/R2 adapter here if organizers ever upload maps."""

    @abstractmethod
    async def put(self, *, prefix: str, extension: str, content: bytes) -> StoredObject: ...


class LocalObjectStore(ObjectStore):
    """Writes under ``backend/uploads``.

    Fine for development. Not suitable for production: container disks are
    ephemeral and are not shared between replicas, so anything written here is
    lost on redeploy. That is why the pilot map is an authored asset rather
    than an upload.
    """

    def __init__(self, settings: Settings, base_dir: str | None = None) -> None:
        self.settings = settings
        self.base_dir = base_dir or os.path.join(
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            ),
            "uploads",
        )

    async def put(self, *, prefix: str, extension: str, content: bytes) -> StoredObject:
        # The stored name is generated, never derived from user input.
        name = f"{uuid.uuid4().hex}{extension}"
        safe_prefix = prefix.strip("/").replace("..", "")
        directory = os.path.join(self.base_dir, safe_prefix)
        os.makedirs(directory, exist_ok=True)

        path = os.path.realpath(os.path.join(directory, name))
        if not path.startswith(os.path.realpath(self.base_dir) + os.sep):
            raise UploadRejected("Invalid upload path.")

        with open(path, "wb") as handle:
            handle.write(content)

        key = f"{safe_prefix}/{name}"
        base = self.settings.storage_public_base_url.rstrip("/") or "/uploads"
        return StoredObject(key=key, url=f"{base}/{key}", size_bytes=len(content))


def get_object_store(settings: Settings) -> ObjectStore:
    return LocalObjectStore(settings)

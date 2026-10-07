"""The catalogue of maps we author and ship with the backend.

Organizers do not upload maps for the pilots — we author one campus in Tiled,
commit the export under ``backend/assets/maps/``, and organizers pick from this
catalogue. That removes the upload endpoint, the object-storage dependency, and
the whole class of "any file, any extension, served from our own origin"
problems that came with it.

To add a map: drop the Tiled JSON export in that directory. It is discovered by
filename, validated on registration, and served from ``/assets/maps/``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.application.maps.parser import MapParseError, MapParser

ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "assets",
    "maps",
)

#: URL prefix the backend serves ``assets/`` from. Point this at a CDN later by
#: setting STORAGE_PUBLIC_BASE_URL.
PUBLIC_PREFIX = "/assets/maps"

KEY_PATTERN_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


class MapNotFound(LookupError):
    """Requested map key is not in the catalogue."""


@dataclass(frozen=True)
class CatalogueMap:
    key: str
    name: str
    url: str
    spawn_points: list[dict]
    width: int
    height: int
    tile_size: int
    tileset_image: str | None

    @property
    def spawn_names(self) -> list[str]:
        return [str(point.get("name")) for point in self.spawn_points]


def _safe_key(key: str) -> str:
    """Reject anything that could escape the assets directory."""
    candidate = key.strip().lower()
    if not candidate or not set(candidate) <= KEY_PATTERN_CHARS:
        raise MapNotFound(f"'{key}' is not a valid map key.")
    return candidate


def available_keys() -> list[str]:
    if not os.path.isdir(ASSETS_DIR):
        return []
    return sorted(
        os.path.splitext(name)[0]
        for name in os.listdir(ASSETS_DIR)
        if name.endswith(".json") and not name.startswith(".")
    )


def load(key: str, public_base_url: str = "") -> CatalogueMap:
    """Read, validate and describe one bundled map."""
    safe = _safe_key(key)
    path = os.path.join(ASSETS_DIR, f"{safe}.json")
    if not os.path.isfile(path):
        raise MapNotFound(
            f"No bundled map '{safe}'. Available: {', '.join(available_keys()) or 'none'}."
        )

    with open(path, "rb") as handle:
        content = handle.read()

    parser = MapParser.from_bytes(content)
    spawn_points = parser.extract_spawn_points()
    if not spawn_points:
        raise MapParseError(
            f"Bundled map '{safe}' declares no spawn points, so nobody could enter it."
        )

    base = public_base_url.rstrip("/")
    tilesets = parser.data.get("tilesets") or [{}]

    return CatalogueMap(
        key=safe,
        name=_display_name(parser, safe),
        url=f"{base}{PUBLIC_PREFIX}/{safe}.json",
        spawn_points=spawn_points,
        width=int(parser.data.get("width", 0)),
        height=int(parser.data.get("height", 0)),
        tile_size=int(parser.data.get("tilewidth", 0)),
        tileset_image=tilesets[0].get("image"),
    )


def load_all(public_base_url: str = "") -> list[CatalogueMap]:
    maps: list[CatalogueMap] = []
    for key in available_keys():
        try:
            maps.append(load(key, public_base_url))
        except (MapNotFound, MapParseError):
            # A malformed file in the directory should not hide the good ones.
            continue
    return maps


def _display_name(parser: MapParser, fallback: str) -> str:
    for prop in parser.data.get("properties", []) or []:
        if isinstance(prop, dict) and prop.get("name") == "mapName":
            return str(prop.get("value") or fallback)
    return fallback.replace("-", " ").replace("_", " ").title()

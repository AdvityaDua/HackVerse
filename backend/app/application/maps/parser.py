"""Tiled map parsing.

``json.loads`` was previously called unguarded on arbitrary uploaded bytes, so
a non-JSON file produced a 500. This validates the shape well enough to reject
something that is not a Tiled export before it can become the campus map.
"""

from __future__ import annotations

import json
from typing import Any


class MapParseError(ValueError):
    """Raised when an upload is not a usable Tiled map."""


class MapParser:
    #: Object-layer names searched for spawn points.
    SPAWN_LAYER_NAMES = ("spawns", "spawn_points", "spawnpoints")

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    @classmethod
    def from_bytes(cls, content: bytes) -> MapParser:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MapParseError("Map file must be UTF-8 encoded JSON.") from exc
        return cls.from_string(text)

    @classmethod
    def from_string(cls, content: str) -> MapParser:
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise MapParseError(f"Map file is not valid JSON: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise MapParseError("Map file must contain a JSON object.")

        layers = data.get("layers")
        if not isinstance(layers, list) or not layers:
            raise MapParseError("Map file has no layers; it is not a Tiled export.")
        for key in ("width", "height", "tilewidth", "tileheight"):
            if not isinstance(data.get(key), int):
                raise MapParseError(f"Map file is missing the numeric '{key}' property.")

        return cls(data)

    def extract_spawn_points(self) -> list[dict[str, Any]]:
        spawns: list[dict[str, Any]] = []
        for layer in self.data.get("layers", []):
            if not isinstance(layer, dict) or layer.get("type") != "objectgroup":
                continue
            if str(layer.get("name", "")).lower() not in self.SPAWN_LAYER_NAMES:
                continue
            for obj in layer.get("objects", []) or []:
                if not isinstance(obj, dict):
                    continue
                spawns.append(
                    {
                        "id": obj.get("id"),
                        "name": obj.get("name") or "default",
                        "x": obj.get("x", 0),
                        "y": obj.get("y", 0),
                    }
                )
        return spawns

    def collision_layer_names(self) -> list[str]:
        return [
            str(layer.get("name"))
            for layer in self.data.get("layers", [])
            if isinstance(layer, dict) and "collision" in str(layer.get("name", "")).lower()
        ]

"""Interest management: who needs to hear about whose movement.

Broadcasting every position to every player in the event is O(n^2) and falls
over well before the PRD's 100-user target. Instead the world is divided into
square cells; a player publishes movement to their own cell and listens to the
3x3 block of cells around themselves. Cost per move becomes proportional to
local density rather than event size.

This module is deliberately framework-free so the arithmetic can be unit tested
without Redis or FastAPI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Cell edge length in tiles. Must be >= the interest radius so that the 3x3
#: neighbourhood always covers everything within that radius.
DEFAULT_CELL_SIZE_TILES = 20


Cell = tuple[int, int]


def cell_for(x: float, y: float, cell_size: int = DEFAULT_CELL_SIZE_TILES) -> Cell:
    """Return the cell coordinate containing tile position ``(x, y)``."""
    if cell_size <= 0:
        raise ValueError("cell_size must be positive")
    return (math.floor(x / cell_size), math.floor(y / cell_size))


def neighbourhood(cell: Cell) -> list[Cell]:
    """Return ``cell`` plus its eight neighbours, in deterministic order."""
    cx, cy = cell
    return [(cx + dx, cy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]


def distance(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def within(ax: float, ay: float, bx: float, by: float, radius: float) -> bool:
    """True when ``b`` is inside ``radius`` tiles of ``a`` (inclusive)."""
    return distance(ax, ay, bx, by) <= radius


@dataclass(frozen=True)
class MovementBounds:
    """Server-side sanity limits for a position update."""

    max_coordinate: float
    #: Maximum tiles a player may legitimately travel between two updates.
    max_step_tiles: float = 12.0

    def is_valid_position(self, x: float, y: float) -> bool:
        for value in (x, y):
            if not math.isfinite(value):
                return False
            if abs(value) > self.max_coordinate:
                return False
        return True

    def is_plausible_step(
        self,
        previous: tuple[float, float] | None,
        x: float,
        y: float,
    ) -> bool:
        """Reject teleports. Absent a previous position, any valid point passes."""
        if previous is None:
            return True
        return distance(previous[0], previous[1], x, y) <= self.max_step_tiles

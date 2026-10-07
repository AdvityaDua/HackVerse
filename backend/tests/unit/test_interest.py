"""Interest management and movement validation."""

import math

import pytest

from app.domain.realtime.interest import (
    DEFAULT_CELL_SIZE_TILES,
    MovementBounds,
    cell_for,
    distance,
    neighbourhood,
    within,
)


class TestCells:
    def test_origin_is_in_cell_zero(self):
        assert cell_for(0, 0) == (0, 0)

    def test_positions_in_the_same_cell_agree(self):
        size = DEFAULT_CELL_SIZE_TILES
        assert cell_for(1, 1) == cell_for(size - 1, size - 1)

    def test_crossing_the_boundary_changes_cell(self):
        size = DEFAULT_CELL_SIZE_TILES
        assert cell_for(size - 0.1, 0) != cell_for(size + 0.1, 0)

    def test_negative_coordinates_floor_downwards(self):
        assert cell_for(-1, -1) == (-1, -1)

    def test_rejects_non_positive_cell_size(self):
        with pytest.raises(ValueError):
            cell_for(1, 1, 0)

    def test_neighbourhood_is_nine_cells_including_self(self):
        cells = neighbourhood((5, 5))
        assert len(cells) == 9
        assert (5, 5) in cells
        assert (4, 4) in cells and (6, 6) in cells

    def test_neighbourhood_covers_the_interest_radius(self):
        """A 3x3 block must cover anything within one cell edge.

        This is the invariant that makes cell-based broadcast correct: if it
        failed, players would vanish from each other near cell borders.
        """
        size = DEFAULT_CELL_SIZE_TILES
        me = (size / 2, size / 2)
        my_cell = cell_for(*me)
        covered = set(neighbourhood(my_cell))
        for dx in range(-size, size + 1, 5):
            for dy in range(-size, size + 1, 5):
                other = (me[0] + dx, me[1] + dy)
                if distance(*me, *other) <= size:
                    assert cell_for(*other) in covered


class TestProximity:
    def test_within_is_inclusive_at_the_boundary(self):
        assert within(0, 0, 3, 0, radius=3)

    def test_outside_radius_is_excluded(self):
        assert not within(0, 0, 3.1, 0, radius=3)

    def test_diagonal_distance_uses_euclidean_metric(self):
        assert distance(0, 0, 3, 4) == pytest.approx(5.0)


class TestMovementBounds:
    @pytest.fixture
    def bounds(self) -> MovementBounds:
        return MovementBounds(max_coordinate=1000, max_step_tiles=10)

    def test_accepts_an_ordinary_position(self, bounds):
        assert bounds.is_valid_position(10, 20)

    @pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
    def test_rejects_non_finite_coordinates(self, bounds, value):
        """A NaN position used to serialise into JSON that broke every client."""
        assert not bounds.is_valid_position(value, 0)
        assert not bounds.is_valid_position(0, value)

    def test_rejects_out_of_world_coordinates(self, bounds):
        assert not bounds.is_valid_position(1001, 0)
        assert not bounds.is_valid_position(0, -1001)

    def test_first_position_is_always_plausible(self, bounds):
        assert bounds.is_plausible_step(None, 500, 500)

    def test_small_steps_are_plausible(self, bounds):
        assert bounds.is_plausible_step((0, 0), 3, 4)

    def test_teleports_are_rejected(self, bounds):
        assert not bounds.is_plausible_step((0, 0), 500, 500)

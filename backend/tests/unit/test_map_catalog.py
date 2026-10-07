"""The authored map catalogue.

Organizers pick from maps we ship rather than uploading their own, so the
bundled files are part of the product and need the same scrutiny as code.
"""

import pytest

from app.application.maps import catalog
from app.application.maps.parser import MapParseError, MapParser


class TestCatalogue:
    def test_the_pilot_campus_map_is_shipped(self):
        assert "campus-v1" in catalog.available_keys()

    def test_loading_the_pilot_map_yields_a_usable_campus(self):
        entry = catalog.load("campus-v1")

        assert entry.width > 0 and entry.height > 0
        assert entry.tile_size == 32
        assert entry.url == "/assets/maps/campus-v1.json"

    def test_the_pilot_map_has_a_spawn_point_for_every_role(self):
        """A missing role spawn means that role enters at the origin."""
        names = set(catalog.load("campus-v1").spawn_names)
        assert {"default", "mentor", "sponsor", "admin"} <= names

    def test_the_pilot_map_declares_the_prd_areas(self):
        """The PRD names these areas; the map has to actually contain them."""
        parser = MapParser.from_bytes(open(f"{catalog.ASSETS_DIR}/campus-v1.json", "rb").read())
        zones = {
            obj["name"]
            for layer in parser.data["layers"]
            if layer.get("name") == "zones"
            for obj in layer["objects"]
        }
        assert {
            "main_stage",
            "sponsor_hall",
            "mentor_lounge",
            "team_area",
            "chill_zone",
        } <= zones

    def test_the_pilot_map_has_a_collision_layer(self):
        parser = MapParser.from_bytes(open(f"{catalog.ASSETS_DIR}/campus-v1.json", "rb").read())
        assert parser.collision_layer_names() == ["collision"]

    def test_public_base_url_fronts_the_asset_path(self):
        entry = catalog.load("campus-v1", "https://cdn.example.com/")
        assert entry.url == "https://cdn.example.com/assets/maps/campus-v1.json"

    def test_unknown_key_is_reported_with_the_available_ones(self):
        with pytest.raises(catalog.MapNotFound, match="campus-v1"):
            catalog.load("no-such-map")

    @pytest.mark.parametrize(
        "evil", ["../../../etc/passwd", "..", "a/b", "campus v1", "", "CAMPUS/../x"]
    )
    def test_keys_cannot_escape_the_assets_directory(self, evil):
        """The key reaches the filesystem, so it must be constrained."""
        with pytest.raises(catalog.MapNotFound):
            catalog.load(evil)

    def test_load_all_skips_nothing_valid(self):
        assert {entry.key for entry in catalog.load_all()} == set(catalog.available_keys())


class TestParserRejectsBadMaps:
    def test_non_json_is_rejected(self):
        with pytest.raises(MapParseError, match="not valid JSON"):
            MapParser.from_bytes(b"<html>nope</html>")

    def test_json_that_is_not_an_object_is_rejected(self):
        with pytest.raises(MapParseError, match="JSON object"):
            MapParser.from_string("[1,2,3]")

    def test_map_without_layers_is_rejected(self):
        with pytest.raises(MapParseError, match="no layers"):
            MapParser.from_string('{"width":1,"height":1,"tilewidth":1,"tileheight":1}')

    def test_map_missing_dimensions_is_rejected(self):
        with pytest.raises(MapParseError, match="tileheight"):
            MapParser.from_string('{"layers":[{}],"width":1,"height":1,"tilewidth":1}')

    def test_non_utf8_is_rejected(self):
        with pytest.raises(MapParseError, match="UTF-8"):
            MapParser.from_bytes(b"\xff\xfe\x00bad")

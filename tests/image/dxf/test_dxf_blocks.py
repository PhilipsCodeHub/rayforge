from pathlib import Path

import pytest

from rayforge.core.vectorization_spec import LayerImportMode, PassthroughSpec
from rayforge.image.dxf.importer import DxfImporter


def _line(layer, x1, y1, x2, y2):
    return [
        (0, "LINE"), (8, layer),
        (10, x1), (20, y1), (30, 0),
        (11, x2), (21, y2), (31, 0),
    ]


def _insert(layer, name, x, y):
    return [
        (0, "INSERT"), (8, layer), (2, name),
        (10, x), (20, y), (30, 0),
    ]


def _blocks_file() -> bytes:
    pairs = [
        (0, "SECTION"), (2, "BLOCKS"),
        (0, "BLOCK"), (8, "0"), (2, "SQ"), (10, 0), (20, 0), (30, 0),
    ]
    pairs += _line("0", 0, 0, 10, 0) + _line("CUT2", 0, 5, 10, 5)
    pairs += [(0, "ENDBLK"), (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES")]
    pairs += _insert("HOST", "SQ", 100, 0) + _insert("HOST", "SQ", 200, 0)
    pairs += _line("HOST", 0, 50, 5, 50)
    pairs += [(0, "ENDSEC"), (0, "EOF")]
    return "".join(f"{c}\n{v}\n" for c, v in pairs).encode()


def _run(blocks=True, mode=LayerImportMode.NEW_LAYERS, **kwargs):
    importer = DxfImporter(_blocks_file(), Path("blocks.dxf"))
    parsed = importer.parse()
    assert parsed is not None
    spec = PassthroughSpec(
        layer_import_mode=mode, blocks_as_groups=blocks, **kwargs
    )
    return importer, importer.vectorize(parsed, spec)


def test_option_off_keeps_flat_layers():
    importer, result = _run(blocks=False)
    assert set(result.geometries_by_layer) == {"HOST", "CUT2"}
    assert importer._block_parts == []


def test_option_on_splits_loose_layers_and_block_parts():
    importer, result = _run()
    geos = result.geometries_by_layer
    assert set(geos) == {
        "HOST",
        "block:0:HOST",
        "block:0:CUT2",
        "block:1:HOST",
        "block:1:CUT2",
    }
    assert geos["HOST"].rect() == pytest.approx((0.0, 50.0, 5.0, 50.0))
    assert geos["block:1:HOST"].rect() == pytest.approx(
        (200.0, 0.0, 210.0, 0.0)
    )
    assert geos["block:0:CUT2"].rect() == pytest.approx(
        (100.0, 5.0, 110.0, 5.0)
    )
    assert [p.host_layer for p in importer._block_parts] == ["HOST"] * 4
    assert {p.block_name for p in importer._block_parts} == {"SQ"}


def test_layer_entries_carry_source_layer():
    _, result = _run()
    sources = {
        layer.layer_id: layer.source_layer_id
        for layer in result.source_parse_result.layers
    }
    assert sources["HOST"] is None
    assert sources["block:0:CUT2"] == "CUT2"
    assert sources["block:1:HOST"] == "HOST"


def test_active_layers_filter_block_parts():
    _, result = _run(active_layer_ids=["CUT2"])
    assert set(result.geometries_by_layer) == {
        "block:0:CUT2",
        "block:1:CUT2",
    }


def test_flatten_mode_ignores_the_option():
    importer, result = _run(mode=LayerImportMode.FLATTEN)
    assert set(result.geometries_by_layer) == {None}
    assert importer._block_parts == []

from pathlib import Path

import pytest

from rayforge.core.group import ORIGIN_LAYER_UID, Group
from rayforge.core.item import DocItem
from rayforge.core.layer import Layer
from rayforge.core.vectorization_spec import LayerImportMode, PassthroughSpec
from rayforge.core.workpiece import WorkPiece
from rayforge.image.assembler import ItemAssembler
from rayforge.image.dxf.importer import DxfImporter
from rayforge.image.engine import NormalizationEngine
from rayforge.image.structures import ImportPayload


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


def _assemble(blocks: bool) -> tuple[DxfImporter, ImportPayload]:
    importer = DxfImporter(_blocks_file(), Path("blocks.dxf"))
    parsed = importer.parse()
    assert parsed is not None
    spec = PassthroughSpec(
        layer_import_mode=LayerImportMode.NEW_LAYERS,
        blocks_as_groups=blocks,
    )
    vec = importer.vectorize(parsed, spec)
    plan = NormalizationEngine().calculate_layout(vec, spec)
    source = importer.create_source_asset(parsed)
    items = ItemAssembler().create_items(
        source_asset=source,
        layout_plan=plan,
        spec=spec,
        source_name="blocks",
        geometries=vec.geometries_by_layer,
        document_bounds=vec.source_parse_result.document_bounds,
    )
    return importer, ImportPayload(source=source, items=items)


def _workpieces(items: list[DocItem]) -> list[WorkPiece]:
    found: list[WorkPiece] = []
    for item in items:
        found.extend(item.get_descendants(of_type=WorkPiece))
    return found


def _key(wp: WorkPiece) -> str | None:
    assert wp.source_segment is not None
    return wp.source_segment.layer_id


def _points(wp: WorkPiece) -> list[float]:
    world = wp.get_world_transform()
    values: list[float] = []
    for corner in [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]:
        values.extend(world.transform_point(corner))
    return values


def test_option_off_leaves_the_payload_alone():
    importer, payload = _assemble(False)
    assert importer._post_process_payload(payload) is payload


def test_blocks_become_groups_in_the_host_layer():
    importer, payload = _assemble(True)
    before = {_key(wp): _points(wp) for wp in _workpieces(payload.items)}
    assert len(before) == 5

    result = importer._post_process_payload(payload)

    layers = {i.name: i for i in result.items if isinstance(i, Layer)}
    assert set(layers) == {"HOST", "CUT2"}
    host, cut2 = layers["HOST"], layers["CUT2"]
    assert cut2.get_content_items() == []

    content = host.get_content_items()
    groups = [c for c in content if isinstance(c, Group)]
    loose = [c for c in content if isinstance(c, WorkPiece)]
    assert [g.name for g in groups] == ["SQ", "SQ (2)"]
    assert [_key(wp) for wp in loose] == ["HOST"]

    for number, group in enumerate(groups):
        kids = {_key(wp): wp for wp in group.all_workpieces}
        assert set(kids) == {
            f"block:{number}:HOST",
            f"block:{number}:CUT2",
        }
        tagged = kids[f"block:{number}:CUT2"]
        assert tagged.extra[ORIGIN_LAYER_UID] == cut2.uid
        assert ORIGIN_LAYER_UID not in kids[f"block:{number}:HOST"].extra

    for wp in _workpieces(result.items):
        assert _points(wp) == pytest.approx(before[_key(wp)])

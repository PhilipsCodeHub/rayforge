from raygeo.geo import Matrix

from rayforge.core.vectorization_spec import LayerImportMode, PassthroughSpec
from rayforge.image.engine import NormalizationEngine
from rayforge.image.structures import (
    LayerGeometry,
    ParsingResult,
    VectorizationResult,
)


def _vec_result(layers: list[LayerGeometry]) -> VectorizationResult:
    parse = ParsingResult(
        document_bounds=(0.0, 0.0, 100.0, 100.0),
        native_unit_to_mm=1.0,
        is_y_down=False,
        layers=layers,
        world_frame_of_reference=(0.0, 0.0, 100.0, 100.0),
        background_world_transform=Matrix.identity(),
    )
    return VectorizationResult(
        geometries_by_layer={}, source_parse_result=parse
    )


def _plan_ids(layers: list[LayerGeometry], active: list[str]):
    spec = PassthroughSpec(
        active_layer_ids=active,
        layer_import_mode=LayerImportMode.NEW_LAYERS,
    )
    plan = NormalizationEngine().calculate_layout(_vec_result(layers), spec)
    return [item.layer_id for item in plan]


def test_derived_layer_kept_when_source_layer_active():
    layers = [
        LayerGeometry("A", "A", (0.0, 0.0, 10.0, 10.0)),
        LayerGeometry(
            "block:1:A",
            "Block",
            (20.0, 20.0, 10.0, 10.0),
            source_layer_id="A",
        ),
        LayerGeometry("B", "B", (40.0, 40.0, 10.0, 10.0)),
    ]
    assert _plan_ids(layers, ["A"]) == ["A", "block:1:A"]


def test_layers_without_source_layer_id_filter_as_before():
    layers = [
        LayerGeometry("A", "A", (0.0, 0.0, 10.0, 10.0)),
        LayerGeometry("B", "B", (40.0, 40.0, 10.0, 10.0)),
    ]
    assert _plan_ids(layers, ["B"]) == ["B"]

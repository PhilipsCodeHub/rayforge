import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from gettext import gettext as _
from pathlib import Path
from typing import Any, ClassVar

from raygeo.formats.dxf.reader import (
    ReadOutput,
    read,
    read_with_inserts,
)
from raygeo.geo import Geometry
from raygeo.geo.types import Rect

from ...core.color import pick_unused_color
from ...core.group import ORIGIN_LAYER_UID, Group
from ...core.item import DocItem
from ...core.layer import Layer
from ...core.source_asset import SourceAsset
from ...core.vectorization_spec import (
    LayerImportMode,
    PassthroughSpec,
    VectorizationSpec,
)
from ...core.workpiece import WorkPiece
from ...image.geo_renderer import render_geometry_to_png
from ..base_importer import (
    Importer,
    ImporterFeature,
)
from ..engine import NormalizationEngine
from ..structures import (
    ImportManifest,
    ImportPayload,
    LayerGeometry,
    LayerInfo,
    ParsingResult,
    VectorizationResult,
)
from .renderer import DXF_RENDERER

logger = logging.getLogger(__name__)

# Z extent below which a drawing is considered flat. DXF coordinates
# carry rounding noise, so an exact zero test is too strict.
Z_VARIATION_TOLERANCE = 1e-4

# Affine projection onto the Z=0 plane: identity in X and Y, zero row
# for Z. Applied with raygeo's Rust-side transform, which preserves
# command types and curve control points exactly.
PROJECT_ONTO_Z0 = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 0.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]

# The layer DXF uses for content that never named one.
DEFAULT_LAYER = "0"

# The layer AutoCAD reserves for construction geometry. It is never cut,
# so it is hidden from the layer list.
HIDDEN_LAYER = "defpoints"


@dataclass
class BlockPart:
    """One DXF layer's share of one INSERT, imported as a part."""

    key: str
    block_name: str
    insert_index: int
    host_layer: str
    source_layer: str


class DxfImporter(Importer):
    label = "DXF files (2D)"
    mime_types = ("image/vnd.dxf",)
    extensions = (".dxf",)
    features: ClassVar[set[ImporterFeature]] = {
        ImporterFeature.DIRECT_VECTOR,
        ImporterFeature.LAYER_SELECTION,
        ImporterFeature.Z_FLATTENING,
        ImporterFeature.GEOMETRY_REPAIR,
        ImporterFeature.BLOCK_GROUPS,
    }

    def __init__(self, data: bytes, source_file: Path | None = None):
        super().__init__(data, source_file)
        self._read_output: ReadOutput | None = None
        self._geometries_by_layer: dict[str | None, Geometry] = {}
        self._structure: Any = None
        self._block_parts: list[BlockPart] = []

    def _read_document(self) -> ReadOutput | None:
        """Reads the file once, caching the result between the phases."""
        if self._read_output is not None:
            return self._read_output
        data_str = self.raw_data.decode("utf-8", errors="replace")
        try:
            self._read_output = read(data_str)
        except ValueError as e:
            logger.warning(f"DXF read failed: {e}")
            self.add_error(_("DXF file is corrupt or invalid: {}").format(e))
            return None
        except Exception as e:
            logger.exception("Unexpected error while reading DXF")
            self.add_error(
                _("Unexpected error while reading DXF: {}").format(e)
            )
            return None
        output = self._read_output
        assert output is not None
        for issue in output.issues:
            self.add_warning(_("DXF import: {}").format(issue))
        return output

    def scan(self) -> ImportManifest:
        output = self._read_document()
        if output is None:
            return ImportManifest(
                title=self.source_file.name, errors=self._errors
            )

        scale = output.units_to_mm
        min_x, min_y, max_x, max_y = output.bounds
        size_mm = None
        if not output.is_empty():
            size_mm = ((max_x - min_x) * scale, (max_y - min_y) * scale)

        return ImportManifest(
            title=self.source_file.name,
            layers=self._layer_infos(output),
            natural_size_mm=size_mm,
            warnings=self._warnings,
            errors=self._errors,
            has_z_variation=output.has_z_variation(Z_VARIATION_TOLERANCE),
        )

    @staticmethod
    def _layer_infos(output: ReadOutput) -> list[LayerInfo]:
        """One entry per layer that draws something, in document order."""
        infos = []
        for name, geometry in output.layers:
            if name.lower() == HIDDEN_LAYER:
                continue
            infos.append(
                LayerInfo(
                    id=name,
                    name=name,
                    feature_count=len(geometry.split_drawn_contours()),
                )
            )
        return infos

    def create_source_asset(self, parse_result: ParsingResult) -> SourceAsset:
        _, _, w, h = parse_result.document_bounds
        width_mm = w * parse_result.native_unit_to_mm
        height_mm = h * parse_result.native_unit_to_mm

        thumbnail_data = self._render_thumbnail()

        source = SourceAsset(
            source_file=self.source_file,
            original_data=self.raw_data,
            renderer=DXF_RENDERER,
            metadata={"is_vector": True},
            thumbnail_data=thumbnail_data,
            width_mm=width_mm,
            height_mm=height_mm,
        )
        return source

    def _render_thumbnail(self, size: int = 256) -> bytes | None:
        merged = self._merged_geometry()
        if merged is None:
            return None
        return render_geometry_to_png(
            merged,
            size,
            line_width=2.0,
            color=(0.2, 0.2, 0.2, 1.0),
        )

    def _merged_geometry(self) -> Geometry | None:
        if not self._geometries_by_layer:
            return None
        merged = Geometry()
        for geo in self._geometries_by_layer.values():
            if geo:
                merged.extend(geo)
        if merged.is_empty():
            return None
        return merged

    def vectorize(
        self,
        parse_result: ParsingResult,
        spec: VectorizationSpec,
    ) -> VectorizationResult:
        """
        Prepares the final vector geometry based on the user's specification.
        This method is "spec-aware" and handles the merging of geometries
        if requested.
        """
        split_layers = False
        active_layers_set = None
        if isinstance(spec, PassthroughSpec):
            split_layers = spec.layer_import_mode != LayerImportMode.FLATTEN
            if spec.active_layer_ids:
                active_layers_set = set(spec.active_layer_ids)

        # Filter geometries based on the active layers in the spec
        geometries_to_process: dict[str | None, Geometry]
        if active_layers_set:
            geometries_to_process = {
                layer_id: geo
                for layer_id, geo in self._geometries_by_layer.items()
                if layer_id in active_layers_set
            }
        else:
            geometries_to_process = self._geometries_by_layer

        self._block_parts = []
        block_geometries: dict[str | None, Geometry] | None = None
        if (
            isinstance(spec, PassthroughSpec)
            and spec.blocks_as_groups
            and split_layers
        ):
            block_geometries = self._block_geometries(
                parse_result, active_layers_set
            )
        if block_geometries is not None:
            geometries_to_process = block_geometries

        final_geometries: dict[str | None, Geometry]
        if split_layers:
            # For a "split" strategy, return the dictionary of individual
            # layer geometries.
            final_geometries = geometries_to_process
        else:
            # For a "merge" strategy, combine all active geometries into a
            # single Geometry object under the `None` key.
            merged_geo = Geometry()
            for geo in geometries_to_process.values():
                merged_geo.extend(geo)
            final_geometries = {None: merged_geo}

        if isinstance(spec, PassthroughSpec) and spec.flatten_z:
            final_geometries = {
                layer_id: self._flatten_z(geo)
                for layer_id, geo in final_geometries.items()
            }

        if isinstance(spec, PassthroughSpec) and spec.repair_outlines:
            final_geometries = {
                layer_id: self._repair(geo, spec.repair_tolerance_mm)
                for layer_id, geo in final_geometries.items()
            }

        # Hack: Updating the parsing result bounds if layers were filtered
        # is a violation of the pipelines sequential nature. Ideally this
        # updated window would be communicated back to the layout engine in a
        # cleaner way. However, for now this ensures that the preview renderer
        # and layout engine remain in sync when layers are filtered out.

        # If we have filtered layers, we must recalculate the bounds in the
        # parsing result. Otherwise, the preview renderer (which renders based
        # on the filtered geometry) and the layout engine (which uses the
        # original whole-doc parsing result for the background image) will
        # disagree, causing the vector overlay to drift from the image.
        final_parse_result = parse_result
        if active_layers_set:
            union_bounds = self._calculate_geometry_union(
                final_geometries.values()
            )
            if union_bounds:
                final_parse_result = self._update_parse_result_bounds(
                    parse_result, union_bounds
                )

        if block_geometries is not None:
            final_parse_result = replace(
                final_parse_result,
                layers=self._block_layers(final_geometries),
            )

        return VectorizationResult(
            geometries_by_layer=final_geometries,
            source_parse_result=final_parse_result,
        )

    def _read_with_inserts(self) -> Any:
        """Reads the file with its INSERT structure, once."""
        if self._structure is not None:
            return self._structure
        data_str = self.raw_data.decode("utf-8", errors="replace")
        try:
            self._structure = read_with_inserts(data_str)
        except Exception as e:
            logger.warning(f"DXF block read failed: {e}")
            self.add_warning(
                _("DXF blocks could not be read as groups: {}").format(e)
            )
            return None
        return self._structure

    def _block_geometries(
        self,
        parse_result: ParsingResult,
        active_layers: set[str] | None,
    ) -> dict[str | None, Geometry] | None:
        """Splits the drawing into loose layers and one part per INSERT.

        Returns None when the file has no INSERT, so the caller keeps the
        normal flat result.
        """
        structure = self._read_with_inserts()
        if structure is None or not structure.inserts:
            return None

        def is_active(name: str) -> bool:
            return not active_layers or name in active_layers

        geometries: dict[str | None, Geometry] = {}
        for name, geo in structure.standalone_layers:
            if is_active(name) and not geo.is_empty():
                geometries[name] = geo.copy()
        for index, insert in enumerate(structure.inserts):
            for name, geo in insert.layers:
                if not is_active(name) or geo.is_empty():
                    continue
                key = f"block:{index}:{name}"
                geometries[key] = geo.copy()
                self._block_parts.append(
                    BlockPart(
                        key=key,
                        block_name=insert.block_name,
                        insert_index=index,
                        host_layer=insert.layer,
                        source_layer=name,
                    )
                )
        self._close_gaps(geometries, parse_result.document_bounds)
        return geometries

    def _block_layers(
        self, geometries: dict[str | None, Geometry]
    ) -> list[LayerGeometry]:
        """Layer entries for the layout engine, one per geometry."""
        parts = {part.key: part for part in self._block_parts}
        layers: list[LayerGeometry] = []
        for key, geo in geometries.items():
            if key is None or geo.is_empty():
                continue
            gx1, gy1, gx2, gy2 = geo.rect()
            part = parts.get(key)
            layers.append(
                LayerGeometry(
                    layer_id=key,
                    name=part.block_name if part else key,
                    content_bounds=(gx1, gy1, gx2 - gx1, gy2 - gy1),
                    source_layer_id=part.source_layer if part else None,
                )
            )
        return layers

    @staticmethod
    def _segment_key(item: DocItem) -> str | None:
        if isinstance(item, WorkPiece) and item.source_segment:
            return item.source_segment.layer_id
        return None

    def _post_process_payload(self, payload: ImportPayload) -> ImportPayload:
        """Turns the block parts into one Group per INSERT.

        Each part's WorkPiece leaves the one-layer wrapper the assembler
        gave it and joins the Group of its INSERT, which sits in the Layer
        of the INSERT's own DXF layer. Children from another DXF layer
        remember that layer in ORIGIN_LAYER_UID.
        """
        if not self._block_parts:
            return payload
        parts = {part.key: part for part in self._block_parts}

        kept: list[DocItem] = []
        part_items: dict[str, WorkPiece] = {}
        for item in payload.items:
            if isinstance(item, Layer):
                content = item.get_content_items()
            else:
                content = [item]
            found: tuple[str, WorkPiece] | None = None
            for child in content:
                key = self._segment_key(child)
                if (
                    key is not None
                    and key in parts
                    and isinstance(child, WorkPiece)
                ):
                    found = (key, child)
                    break
            if found is None:
                kept.append(item)
            else:
                part_items[found[0]] = found[1]

        layers_by_name: dict[str, Layer] = {
            item.name: item for item in kept if isinstance(item, Layer)
        }
        used_colors = {layer.color for layer in layers_by_name.values()}
        created: list[Layer] = []

        def layer_for(name: str) -> Layer:
            layer = layers_by_name.get(name)
            if layer is None:
                layer = Layer(name=name)
                layer.set_color(pick_unused_color(used_colors))
                used_colors.add(layer.color)
                layers_by_name[name] = layer
                created.append(layer)
            return layer

        by_insert: dict[int, list[BlockPart]] = {}
        for part in self._block_parts:
            if part.key in part_items:
                by_insert.setdefault(part.insert_index, []).append(part)

        name_counts: dict[str, int] = {}
        for index in sorted(by_insert):
            insert_parts = by_insert[index]
            host = layer_for(insert_parts[0].host_layer)
            workpieces = [part_items[p.key] for p in insert_parts]
            members: list[DocItem] = list(workpieces)
            grouping = Group.create_from_items(members, host)
            if grouping is None:
                continue
            for part in insert_parts:
                if part.source_layer != part.host_layer:
                    origin = layer_for(part.source_layer)
                    part_items[part.key].extra[ORIGIN_LAYER_UID] = origin.uid

            base = insert_parts[0].block_name
            count = name_counts.get(base, 0) + 1
            name_counts[base] = count
            group = grouping.new_group
            group.name = base if count == 1 else f"{base} ({count})"
            host.add_child(group)
            for wp in workpieces:
                if wp.parent:
                    wp.parent.remove_child(wp)
            group.add_children(members)
            for wp in workpieces:
                wp.matrix = grouping.child_matrices[wp.uid]

        return replace(payload, items=[*kept, *created])

    def _calculate_geometry_union(
        self, geometries: Iterable[Geometry]
    ) -> Rect | None:
        """Calculates the bounding box of a collection of geometries."""
        min_x, min_y, max_x, max_y = (
            float("inf"),
            float("inf"),
            float("-inf"),
            float("-inf"),
        )
        has_content = False

        for geo in geometries:
            if not geo or geo.is_empty():
                continue
            gx1, gy1, gx2, gy2 = geo.rect()
            min_x = min(min_x, gx1)
            min_y = min(min_y, gy1)
            max_x = max(max_x, gx2)
            max_y = max(max_y, gy2)
            has_content = True

        if not has_content:
            return None

        return (min_x, min_y, max_x - min_x, max_y - min_y)

    def _update_parse_result_bounds(
        self,
        original: ParsingResult,
        new_bounds: Rect,
    ) -> ParsingResult:
        """
        Creates a new ParsingResult with updated bounds and transform matrices
        to reflect a subset of the original document.
        """
        x, y, w, h = new_bounds
        scale = original.native_unit_to_mm

        # Recalculate world frame of reference (mm)
        new_world_frame = (x * scale, y * scale, w * scale, h * scale)

        # Create a temp result to allow the Engine to calculate the matrix
        # mapping the new bounds to the new world frame.
        # Note: We must preserve is_y_down=False for DXF (Y-Up).
        temp_result = replace(
            original,
            document_bounds=new_bounds,
            world_frame_of_reference=new_world_frame,
        )

        bg_item = NormalizationEngine.calculate_layout_item(
            new_bounds, temp_result
        )

        return replace(
            original,
            document_bounds=new_bounds,
            world_frame_of_reference=new_world_frame,
            background_world_transform=bg_item.world_matrix,
        )

    def parse(self) -> ParsingResult | None:
        output = self._read_document()
        if output is None:
            return None

        geometries_by_layer: dict[str | None, Geometry] = {
            name: geometry.copy() for name, geometry in output.layers
        }
        self._geometries_by_layer = geometries_by_layer

        min_x, min_y, max_x, max_y = output.bounds
        doc_bounds: Rect = (
            min_x,
            min_y,
            max_x - min_x,
            max_y - min_y,
        )
        if output.is_empty():
            doc_bounds = (0.0, 0.0, 0.0, 0.0)

        self._close_gaps(geometries_by_layer, doc_bounds)

        native_unit_to_mm = output.units_to_mm

        # Create a temporary result to calculate transforms
        temp_result = ParsingResult(
            document_bounds=doc_bounds,
            native_unit_to_mm=native_unit_to_mm,
            is_y_down=False,
            layers=[],
            # Dummy values, will be replaced
            world_frame_of_reference=(0, 0, 0, 0),
            background_world_transform=None,  # type: ignore
        )

        x, y, w, h = doc_bounds
        world_frame = (
            x * native_unit_to_mm,
            y * native_unit_to_mm,
            w * native_unit_to_mm,
            h * native_unit_to_mm,
        )
        bg_layout_item = NormalizationEngine.calculate_layout_item(
            doc_bounds, temp_result
        )

        result = ParsingResult(
            document_bounds=doc_bounds,
            native_unit_to_mm=native_unit_to_mm,
            is_y_down=False,
            layers=[],
            world_frame_of_reference=world_frame,
            background_world_transform=bg_layout_item.world_matrix,
        )

        for layer_name, geo in geometries_by_layer.items():
            if layer_name is None or geo.is_empty():
                continue
            gx1, gy1, gx2, gy2 = geo.rect()
            result.layers.append(
                LayerGeometry(
                    layer_id=layer_name,
                    name=layer_name,
                    content_bounds=(gx1, gy1, gx2 - gx1, gy2 - gy1),
                )
            )

        return result

    @staticmethod
    def _close_gaps(
        geometries_by_layer: dict[str | None, Geometry],
        doc_bounds: Rect,
    ) -> None:
        """Chains sub-paths into contours and bridges hairline gaps.

        DXF entities arrive as independent sub-paths, one per LINE, ARC
        or SPLINE, regardless of how they relate on the drawing. Joining
        those whose ends meet restores the contours the author drew, so
        parts come back as closed outlines rather than loose segments.

        CAD exporters also routinely leave the ends a fraction of a unit
        apart. Bridging those gaps within a contour is always safe; wider
        gaps are left for the optional outline repair, which the user
        enables in the import dialog. Separate sub-paths are never
        bridged here: that would draw lines the file does not contain.
        """
        w, h = doc_bounds[2], doc_bounds[3]
        diag = math.hypot(w, h)
        tolerance = max(0.01, diag / 20000.0)
        for layer_id, geo in geometries_by_layer.items():
            if geo and not geo.is_empty():
                joined, _ = geo.join_open_contours(tolerance)
                closed, _ = joined.close_open_contours(tolerance)
                geometries_by_layer[layer_id] = closed

    @staticmethod
    def _flatten_z(geo: Geometry) -> Geometry:
        """Returns a copy of *geo* with every point projected onto the
        Z=0 plane, transformed in Rust via a projection matrix.
        """
        if geo is None or geo.is_empty():
            return geo
        flat = geo.copy()
        flat.transform(PROJECT_ONTO_Z0)
        return flat

    def _repair(self, geo: Geometry, tolerance_mm: float | None) -> Geometry:
        """Joins and closes fragmented outlines in native units.

        The tolerance is chosen in millimetres, because that is what the
        user thinks in and what the import dialog shows. It is converted
        back to native units using the drawing's own scale.
        """
        if geo is None or geo.is_empty():
            return geo
        if tolerance_mm is None:
            tolerance_mm = self._adaptive_repair_tolerance_mm(geo)
        native = tolerance_mm / self._native_unit_to_mm()
        repaired, report = geo.repair_outlines(native)
        if report.joins or report.closed or report.duplicates_removed:
            self.add_warning(
                _(
                    "Repaired {joins} gap(s), closed {closed} outline(s) "
                    "and removed {duplicates} duplicate(s)."
                ).format(
                    joins=report.joins,
                    closed=report.closed,
                    duplicates=report.duplicates_removed,
                )
            )
        if report.open_remaining:
            self.add_warning(
                _(
                    "{count} outline(s) are still open after repair; "
                    "raising the tolerance may close them."
                ).format(count=report.open_remaining)
            )
        return repaired

    def _native_unit_to_mm(self) -> float:
        if self._read_output is None:
            return 1.0
        return self._read_output.units_to_mm or 1.0

    @staticmethod
    def _adaptive_repair_tolerance_mm(geo: Geometry) -> float:
        """A tolerance that suits the drawing's own size.

        A fixed default would be too small for a large panel and too
        large for a tiny engraving. Scaling with the diagonal keeps the
        repair proportional to the work.
        """
        min_x, min_y, max_x, max_y = geo.rect()
        diagonal = math.hypot(max_x - min_x, max_y - min_y)
        return max(0.05, diagonal / 5000.0)

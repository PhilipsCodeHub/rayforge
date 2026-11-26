from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.group import Group, effective_layer
from ..core.layer import Layer
from ..core.workpiece import WorkPiece

if TYPE_CHECKING:
    from ..core.doc import Doc


@dataclass(frozen=True)
class GroupBatch:
    """Workpieces for one group, bucketed by layer in doc.layers order."""

    group_uid: str | None  # None = the ungrouped batch
    by_layer: list[tuple[Layer, list[WorkPiece]]]


def _top_groups_by_uid(doc: Doc) -> dict[str, Group]:
    """Top-level groups (direct children of a layer), in document order."""
    top_groups: dict[str, Group] = {}
    for layer in doc.layers:
        for child in layer.children:
            if isinstance(child, Group):
                top_groups[child.uid] = child
    return top_groups


def _ordered_uids(
    listed: list[str], top_groups: dict[str, Group]
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for uid in listed:
        if uid in top_groups and uid not in seen:
            ordered.append(uid)
            seen.add(uid)
    for uid in top_groups:
        if uid not in seen:
            ordered.append(uid)
            seen.add(uid)
    return ordered


def top_level_groups(doc: Doc) -> list[Group]:
    """Top-level groups (direct children of a layer), in document order."""
    return list(_top_groups_by_uid(doc).values())


def effective_group_order(doc: Doc) -> list[str]:
    """
    UIDs of the top-level groups in execution order: those listed in
    doc.group_execution_order first, then the missing ones in document
    order. Stale and duplicate UIDs are ignored.
    """
    return _ordered_uids(doc.group_execution_order, _top_groups_by_uid(doc))


def resolve_group_batches(
    doc: Doc, use_origin: bool = False
) -> list[GroupBatch]:
    """
    Returns execution batches for group-based G-code ordering:
    top-level groups in doc.group_execution_order, then any top-level
    groups missing from that list (document order), then ungrouped
    workpieces. Stale or duplicate UIDs are ignored. Empty batches
    are omitted. With use_origin True, 
    grouped workpieces are bucketed by their effective (origin) layer.
    """
    layers = list(doc.layers)
    layer_index = {layer.uid: i for i, layer in enumerate(layers)}
    layers_by_uid = {layer.uid: layer for layer in layers}

    top_groups = _top_groups_by_uid(doc)
    ordered = _ordered_uids(doc.group_execution_order, top_groups)

    batches: list[GroupBatch] = []
    for uid in ordered:
        buckets: dict[str, tuple[Layer, list[WorkPiece]]] = {}
        for wp in top_groups[uid].all_workpieces:
            if use_origin:
                layer = effective_layer(wp, layers_by_uid)
            else:
                layer = wp.get_ancestor_by_type(Layer)
            if not isinstance(layer, Layer) or layer.uid not in layer_index:
                continue
            buckets.setdefault(layer.uid, (layer, []))[1].append(wp)
        if buckets:
            by_layer = sorted(
                buckets.values(), key=lambda b: layer_index[b[0].uid]
            )
            batches.append(GroupBatch(uid, by_layer))

    # Ungrouped: direct workpieces of each layer, existing order.
    ungrouped = []
    for layer in layers:
        wps = [c for c in layer.children if isinstance(c, WorkPiece)]
        if wps:
            ungrouped.append((layer, wps))
    if ungrouped:
        batches.append(GroupBatch(None, ungrouped))

    return batches

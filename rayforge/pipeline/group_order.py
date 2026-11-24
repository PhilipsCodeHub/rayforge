from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.group import Group
from ..core.layer import Layer
from ..core.workpiece import WorkPiece

if TYPE_CHECKING:
    from ..core.doc import Doc


@dataclass(frozen=True)
class GroupBatch:
    """Workpieces for one group, bucketed by layer in doc.layers order."""

    group_uid: str | None  # None = the ungrouped batch
    by_layer: list[tuple[Layer, list[WorkPiece]]]


def resolve_group_batches(doc: Doc) -> list[GroupBatch]:
    """
    Returns execution batches for group-based G-code ordering:
    top-level groups in doc.group_execution_order, then any top-level
    groups missing from that list (document order), then ungrouped
    workpieces. Stale or duplicate UIDs are ignored. Empty batches
    are omitted.
    """
    layers = list(doc.layers)
    layer_index = {layer.uid: i for i, layer in enumerate(layers)}

    # Top-level groups in document order.
    top_groups: dict[str, Group] = {}
    for layer in layers:
        for child in layer.children:
            if isinstance(child, Group):
                top_groups[child.uid] = child

    ordered: list[str] = []
    seen: set[str] = set()
    for uid in doc.group_execution_order:
        if uid in top_groups and uid not in seen:
            ordered.append(uid)
            seen.add(uid)
    for uid in top_groups:
        if uid not in seen:
            ordered.append(uid)
            seen.add(uid)

    batches: list[GroupBatch] = []
    for uid in ordered:
        buckets: dict[str, tuple[Layer, list[WorkPiece]]] = {}
        for wp in top_groups[uid].all_workpieces:
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
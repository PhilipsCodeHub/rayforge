from rayforge.core.doc import Doc
from rayforge.core.group import Group
from rayforge.core.layer import Layer
from rayforge.core.workpiece import WorkPiece
from rayforge.pipeline.group_order import (
    effective_group_order,
    resolve_group_batches,
    top_level_groups,
)


def _group(*items, name="g"):
    g = Group(name=name)
    for item in items:
        g.add_child(item)
    return g


def _three_groups(doc):
    layer = doc.active_layer
    ga = _group(WorkPiece(name="a"), name="A")
    gb = _group(WorkPiece(name="b"), name="B")
    gc = _group(WorkPiece(name="c"), name="C")
    for g in (ga, gb, gc):
        layer.add_child(g)
    return ga, gb, gc


def test_no_groups_gives_empty_order():
    doc = Doc()
    doc.active_layer.add_child(WorkPiece(name="a"))
    assert top_level_groups(doc) == []
    assert effective_group_order(doc) == []


def test_without_explicit_order_follows_document_order():
    doc = Doc()
    ga, gb, gc = _three_groups(doc)
    assert effective_group_order(doc) == [ga.uid, gb.uid, gc.uid]


def test_listed_groups_first_then_missing_in_document_order():
    doc = Doc()
    ga, gb, gc = _three_groups(doc)
    doc.group_execution_order = [gc.uid]
    assert effective_group_order(doc) == [gc.uid, ga.uid, gb.uid]


def test_stale_and_duplicate_uids_are_dropped():
    doc = Doc()
    ga, gb, _gc = _three_groups(doc)
    doc.group_execution_order = ["gone", gb.uid, gb.uid]
    order = effective_group_order(doc)
    assert order[0] == gb.uid
    assert "gone" not in order
    assert order.count(gb.uid) == 1
    assert len(order) == 3


def test_nested_group_is_not_top_level():
    doc = Doc()
    inner = _group(WorkPiece(name="x"), name="inner")
    outer = _group(inner, name="outer")
    doc.active_layer.add_child(outer)
    doc.group_execution_order = [inner.uid, outer.uid]

    assert [g.uid for g in top_level_groups(doc)] == [outer.uid]
    assert effective_group_order(doc) == [outer.uid]


def test_top_level_groups_span_layers_in_document_order():
    doc = Doc()
    layer1 = doc.active_layer
    layer2 = Layer(name="L2")
    doc.add_child(layer2)
    ga = _group(WorkPiece(name="a"), name="A")
    gb = _group(WorkPiece(name="b"), name="B")
    layer1.add_child(ga)
    layer2.add_child(gb)

    assert [g.uid for g in top_level_groups(doc)] == [ga.uid, gb.uid]


def test_empty_group_is_in_order_but_has_no_batch():
    doc = Doc()
    empty = _group(name="empty")
    doc.active_layer.add_child(empty)

    assert effective_group_order(doc) == [empty.uid]
    assert resolve_group_batches(doc) == []


def test_setter_drives_batch_order():
    doc = Doc()
    ga, gb, gc = _three_groups(doc)
    doc.set_group_execution_order([gc.uid, ga.uid, gb.uid])

    assert effective_group_order(doc) == [gc.uid, ga.uid, gb.uid]
    batches = resolve_group_batches(doc)
    assert [b.group_uid for b in batches] == [gc.uid, ga.uid, gb.uid]

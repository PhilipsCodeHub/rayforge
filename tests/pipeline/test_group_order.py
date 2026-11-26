from rayforge.core.doc import Doc
from rayforge.core.group import Group
from rayforge.core.layer import Layer
from rayforge.core.workpiece import WorkPiece
from rayforge.pipeline.group_order import resolve_group_batches


def _uids(batch):
    return [[wp.uid for wp in wps] for _, wps in batch.by_layer]


def _group(*items, name="g"):
    g = Group(name=name)
    for item in items:
        g.add_child(item)
    return g


def test_no_groups_yields_only_ungrouped_batch():
    doc = Doc()
    wp1, wp2 = WorkPiece(name="a"), WorkPiece(name="b")
    doc.active_layer.add_child(wp1)
    doc.active_layer.add_child(wp2)

    batches = resolve_group_batches(doc)
    assert len(batches) == 1
    assert batches[0].group_uid is None
    assert _uids(batches[0]) == [[wp1.uid, wp2.uid]]


def test_groups_follow_execution_order_then_ungrouped():
    doc = Doc()
    layer = doc.active_layer
    wpa, wpb, wpu = (WorkPiece(name=n) for n in "abu")
    ga, gb = _group(wpa, name="A"), _group(wpb, name="B")
    layer.add_child(ga)
    layer.add_child(gb)
    layer.add_child(wpu)
    doc.group_execution_order = [gb.uid, ga.uid]

    batches = resolve_group_batches(doc)
    assert [b.group_uid for b in batches] == [gb.uid, ga.uid, None]
    assert _uids(batches[2]) == [[wpu.uid]]


def test_missing_groups_appended_in_document_order():
    doc = Doc()
    layer = doc.active_layer
    ga = _group(WorkPiece(name="a"), name="A")
    gb = _group(WorkPiece(name="b"), name="B")
    gc = _group(WorkPiece(name="c"), name="C")
    for g in (ga, gb, gc):
        layer.add_child(g)
    doc.group_execution_order = [gc.uid]

    batches = resolve_group_batches(doc)
    assert [b.group_uid for b in batches] == [gc.uid, ga.uid, gb.uid]


def test_stale_and_duplicate_uids_ignored():
    doc = Doc()
    ga = _group(WorkPiece(name="a"), name="A")
    doc.active_layer.add_child(ga)
    doc.group_execution_order = ["gone", ga.uid, ga.uid]

    batches = resolve_group_batches(doc)
    assert [b.group_uid for b in batches] == [ga.uid]


def test_nested_group_order_is_depth_first_child_order():
    doc = Doc()
    w1, w2, w3 = (WorkPiece(name=n) for n in ("1", "2", "3"))
    inner = _group(w2, name="inner")
    outer = _group(w1, inner, w3, name="outer")
    doc.active_layer.add_child(outer)
    doc.group_execution_order = [outer.uid]

    batches = resolve_group_batches(doc)
    assert len(batches) == 1
    assert _uids(batches[0]) == [[w1.uid, w2.uid, w3.uid]]


def test_nested_group_uid_in_order_list_is_ignored():
    doc = Doc()
    inner = _group(WorkPiece(name="x"), name="inner")
    outer = _group(inner, name="outer")
    doc.active_layer.add_child(outer)
    doc.group_execution_order = [inner.uid, outer.uid]

    batches = resolve_group_batches(doc)
    assert [b.group_uid for b in batches] == [outer.uid]


def test_empty_group_produces_no_batch():
    doc = Doc()
    doc.active_layer.add_child(_group(name="empty"))

    assert resolve_group_batches(doc) == []


def test_group_excludes_unrelated_layer_workpieces():
    doc = Doc()
    layer = doc.active_layer
    wp_in, wp_out = WorkPiece(name="in"), WorkPiece(name="out")
    g = _group(wp_in)
    layer.add_child(g)
    layer.add_child(wp_out)

    batches = resolve_group_batches(doc)
    assert _uids(batches[0]) == [[wp_in.uid]]
    assert _uids(batches[1]) == [[wp_out.uid]]


def test_ungrouped_batch_spans_layers_in_layer_order():
    doc = Doc()
    layer1 = doc.active_layer
    layer2 = Layer(name="L2")
    doc.add_child(layer2)
    w1, w2 = WorkPiece(name="1"), WorkPiece(name="2")
    layer1.add_child(w1)
    layer2.add_child(w2)

    batches = resolve_group_batches(doc)
    assert len(batches) == 1
    assert [layer.uid for layer, _ in batches[0].by_layer] == [
        layer1.uid,
        layer2.uid,
    ]
    assert _uids(batches[0]) == [[w1.uid], [w2.uid]]

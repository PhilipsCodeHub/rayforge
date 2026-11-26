from rayforge.core.doc import Doc
from rayforge.core.group import ORIGIN_LAYER_UID, Group, effective_layer
from rayforge.core.workpiece import WorkPiece
from rayforge.pipeline.group_order import resolve_group_batches
from rayforge.core.step import Step
from rayforge.pipeline.intent_builder import (
    IntentBuilder,
    group_step_key,
    job_key,
    step_key,
)


def _cross_layer_doc():
    """Group on layer 0 holding one item from layer 0 and one from 1."""
    doc = Doc()
    la, lb = doc.layers[0], doc.layers[1]
    wp_a, wp_b = WorkPiece(name="a"), WorkPiece(name="b")
    group = Group(name="g")
    group.add_child(wp_a)
    group.add_child(wp_b)
    la.add_child(group)
    wp_a.extra[ORIGIN_LAYER_UID] = la.uid
    wp_b.extra[ORIGIN_LAYER_UID] = lb.uid
    return doc, la, lb, group, wp_a, wp_b


def _uids(items):
    return {i.uid for i in items}


def test_effective_layer_uses_origin_tag_inside_group():
    doc, la, lb, _group, wp_a, wp_b = _cross_layer_doc()
    by_uid = {layer.uid: layer for layer in doc.layers}
    assert effective_layer(wp_b, by_uid) is lb
    assert effective_layer(wp_a, by_uid) is la


def test_effective_layer_ignores_tag_outside_group():
    doc = Doc()
    la, lb = doc.layers[0], doc.layers[1]
    wp = WorkPiece(name="loose")
    la.add_child(wp)
    wp.extra[ORIGIN_LAYER_UID] = lb.uid  # stale tag
    by_uid = {layer.uid: layer for layer in doc.layers}
    assert effective_layer(wp, by_uid) is la


def test_effective_layer_without_tag_uses_hierarchy():
    doc = Doc()
    la = doc.layers[0]
    group = Group(name="g")
    wp = WorkPiece(name="untagged")
    group.add_child(wp)
    la.add_child(group)
    by_uid = {layer.uid: layer for layer in doc.layers}
    assert effective_layer(wp, by_uid) is la


def test_effective_layer_falls_back_when_origin_layer_missing():
    doc, la, lb, _group, _wp_a, wp_b = _cross_layer_doc()
    by_uid = {la.uid: la}  # layer B no longer exists
    assert effective_layer(wp_b, by_uid) is la


def test_effective_layer_nested_group_uses_workpiece_tag():
    doc = Doc()
    la, lb = doc.layers[0], doc.layers[1]
    inner, outer = Group(name="inner"), Group(name="outer")
    wp = WorkPiece(name="w")
    inner.add_child(wp)
    outer.add_child(inner)
    la.add_child(outer)
    wp.extra[ORIGIN_LAYER_UID] = lb.uid
    by_uid = {layer.uid: layer for layer in doc.layers}
    assert effective_layer(wp, by_uid) is lb


def test_workpieces_by_layer_off_matches_hierarchy():
    doc, la, lb, _group, wp_a, wp_b = _cross_layer_doc()
    result = doc.workpieces_by_layer(False)
    assert _uids(result[la.uid]) == _uids([wp_a, wp_b])
    assert result[lb.uid] == []


def test_workpieces_by_layer_on_moves_cross_layer_child():
    doc, la, lb, _group, wp_a, wp_b = _cross_layer_doc()
    result = doc.workpieces_by_layer(True)
    assert _uids(result[la.uid]) == _uids([wp_a])
    assert _uids(result[lb.uid]) == _uids([wp_b])


def test_workpieces_by_layer_on_equals_off_without_groups():
    doc = Doc()
    for layer, name in zip(doc.layers, ("x", "y", "z")):
        layer.add_child(WorkPiece(name=name))
    off = doc.workpieces_by_layer(False)
    on = doc.workpieces_by_layer(True)
    assert {k: _uids(v) for k, v in off.items()} == {
        k: _uids(v) for k, v in on.items()
    }


def test_workpieces_by_layer_has_entry_for_every_layer():
    doc = Doc()
    assert set(doc.workpieces_by_layer(True)) == {
        layer.uid for layer in doc.layers
    }


def test_batches_off_put_cross_layer_group_in_one_bucket():
    doc, la, _lb, group, wp_a, wp_b = _cross_layer_doc()
    batches = resolve_group_batches(doc, use_origin=False)
    assert len(batches) == 1
    assert batches[0].group_uid == group.uid
    assert [layer.uid for layer, _ in batches[0].by_layer] == [la.uid]
    assert _uids(batches[0].by_layer[0][1]) == _uids([wp_a, wp_b])


def test_batches_on_split_cross_layer_group_by_origin_layer():
    doc, la, lb, group, wp_a, wp_b = _cross_layer_doc()
    batches = resolve_group_batches(doc, use_origin=True)
    assert len(batches) == 1
    assert batches[0].group_uid == group.uid
    by_layer = batches[0].by_layer
    assert [layer.uid for layer, _ in by_layer] == [la.uid, lb.uid]
    assert _uids(by_layer[0][1]) == _uids([wp_a])
    assert _uids(by_layer[1][1]) == _uids([wp_b])


def test_batches_on_deleted_origin_layer_falls_back_to_group_layer():
    doc, la, _lb, _group, wp_a, wp_b = _cross_layer_doc()
    wp_b.extra[ORIGIN_LAYER_UID] = "no-such-layer"
    batches = resolve_group_batches(doc, use_origin=True)
    by_layer = batches[0].by_layer
    assert [layer.uid for layer, _ in by_layer] == [la.uid]
    assert _uids(by_layer[0][1]) == _uids([wp_a, wp_b])


def test_batches_default_is_off_behavior():
    doc, _la, _lb, _group, _wp_a, _wp_b = _cross_layer_doc()
    default = resolve_group_batches(doc)
    off = resolve_group_batches(doc, use_origin=False)
    assert [
        (b.group_uid, [layer.uid for layer, _ in b.by_layer])
        for b in default
    ] == [
        (b.group_uid, [layer.uid for layer, _ in b.by_layer]) for b in off
    ]


class _Step(Step):
    def __init__(self, name: str):
        super().__init__(typelabel="test", name=name)

    def is_position_sensitive(self) -> bool:
        return False


def _doc_with_steps():
    """Cross-layer group; layer A and layer B each have one step."""
    doc, la, lb, group, _wp_a, _wp_b = _cross_layer_doc()
    step_a, step_b = _Step("sa"), _Step("sb")
    la.workflow.add_child(step_a)
    lb.workflow.add_child(step_b)
    return doc, group, step_a, step_b


def _job_input_keys(machine, doc, group_order, child_workflow):
    nodes = IntentBuilder(
        machine=machine,
        group_gcode_order=group_order,
        group_child_layer_workflow=child_workflow,
    ).build(doc)
    job = next(n for n in nodes if n.key == job_key())
    return [[i.source_key for i in g.inputs] for g in job.stage.spec.groups]


def test_builder_group_order_on_child_workflow_on(isolated_machine):
    doc, group, step_a, step_b = _doc_with_steps()
    keys = _job_input_keys(isolated_machine, doc, True, True)
    assert keys == [
        [group_step_key(step_a.uid, group.uid)],
        [group_step_key(step_b.uid, group.uid)],
    ]


def test_builder_group_order_on_child_workflow_off(isolated_machine):
    doc, group, step_a, _step_b = _doc_with_steps()
    keys = _job_input_keys(isolated_machine, doc, True, False)
    assert keys == [[group_step_key(step_a.uid, group.uid)]]


def test_builder_layer_order_child_workflow_on(isolated_machine):
    doc, _group, step_a, step_b = _doc_with_steps()
    keys = _job_input_keys(isolated_machine, doc, False, True)
    assert keys == [[step_key(step_a.uid)], [step_key(step_b.uid)]]


def test_builder_layer_order_child_workflow_off(isolated_machine):
    doc, _group, step_a, _step_b = _doc_with_steps()
    keys = _job_input_keys(isolated_machine, doc, False, False)
    assert keys == [[step_key(step_a.uid)]]


def test_builder_default_flag_matches_off(isolated_machine):
    doc, _group, _step_a, _step_b = _doc_with_steps()
    default = IntentBuilder(machine=isolated_machine).build(doc)
    explicit = IntentBuilder(
        machine=isolated_machine,
        group_gcode_order=False,
        group_child_layer_workflow=False,
    ).build(doc)
    assert [n.key for n in default] == [n.key for n in explicit]

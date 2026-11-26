from rayforge.core.doc import Doc
from rayforge.core.group import Group
from rayforge.core.step import Step
from rayforge.core.workpiece import WorkPiece
from raygeo.pipeline.stage import StageSpec
from rayforge.pipeline.intent_builder import (
    IntentBuilder,
    group_step_key,
    job_key,
    step_key,
    workpiece_key,
)


class _CommandLikeStep(Step):
    """Minimal geometry-less step (mirrors test_intent_builder_command)."""

    needs_workpieces = False

    def __init__(self, name: str = "command"):
        super().__init__(typelabel="command", name=name)
        self.lines = ["M101"]

    def get_cache_params(self):
        params = super().get_cache_params()
        params["command_text"] = "\n".join(self.lines)
        return params

    def build_command_payload(self, machine):
        from raygeo.cnc.execution.specs import ComputePayload
        from raygeo.ops.assembly import Assembler
        from raygeo.ops.assembly.custom import CustomSpec
        from raygeo.ops.part import Part

        return Part(size_mm=(0.0, 0.0)), ComputePayload(
            assembler=Assembler(CustomSpec(lines=self.lines))
        )


def test_on_command_step_runs_first_in_layers_first_slot(isolated_machine):
    doc, step, (ga, gb), _wps = _make_doc()
    cmd = _CommandLikeStep()
    # Added after the geometry step, so its workflow position is last.
    workflow = doc.active_layer.workflow
    assert workflow is not None
    workflow.add_child(cmd)

    job = _job_node(isolated_machine, doc)
    assert _group_input_keys(job) == [
        [step_key(cmd.uid), group_step_key(step.uid, ga.uid)],
        [group_step_key(step.uid, gb.uid)],
        [group_step_key(step.uid, None)],
    ]


def test_on_command_step_appears_exactly_once(isolated_machine):
    doc, _step, _groups, _wps = _make_doc()
    cmd = _CommandLikeStep()
    workflow = doc.active_layer.workflow
    assert workflow is not None
    workflow.add_child(cmd)

    job = _job_node(isolated_machine, doc)
    flat = [k for keys in _group_input_keys(job) for k in keys]
    assert flat.count(step_key(cmd.uid)) == 1


def test_on_command_only_layer_still_runs_after_batches(isolated_machine):
    doc, step, (ga, gb), _wps = _make_doc()
    layer2 = doc.layers[1]  # default layer without workpieces
    assert layer2.workflow is not None
    cmd2 = _CommandLikeStep(name="cmd2")
    layer2.workflow.add_child(cmd2)

    job = _job_node(isolated_machine, doc)
    keys = _group_input_keys(job)
    assert keys[-1] == [step_key(cmd2.uid)]
    assert len(keys) == 4


class _TestStep(Step):
    def __init__(self, name: str = "test"):
        super().__init__(typelabel="test", name=name)

    def is_position_sensitive(self) -> bool:
        return False


def _job_node(machine, doc):
    nodes = IntentBuilder(machine=machine, group_gcode_order=True).build(doc)
    job = next(n for n in nodes if n.key == job_key())
    assert isinstance(job.stage, StageSpec.Aggregate)
    return job


def _group_input_keys(job):
    return [[i.source_key for i in g.inputs] for g in job.stage.spec.groups]


def test_on_job_aggregate_follows_group_order(isolated_machine):
    doc, step, (ga, gb), _wps = _make_doc()
    job = _job_node(isolated_machine, doc)
    assert _group_input_keys(job) == [
        [group_step_key(step.uid, ga.uid)],
        [group_step_key(step.uid, gb.uid)],
        [group_step_key(step.uid, None)],
    ]


def test_on_reordering_groups_changes_job_and_order(isolated_machine):
    doc, step, (ga, gb), _wps = _make_doc()
    before = _job_node(isolated_machine, doc)
    doc.group_execution_order = [gb.uid, ga.uid]
    after = _job_node(isolated_machine, doc)

    assert _group_input_keys(after) == [
        [group_step_key(step.uid, gb.uid)],
        [group_step_key(step.uid, ga.uid)],
        [group_step_key(step.uid, None)],
    ]
    assert before.version_token != after.version_token


def test_on_job_token_differs_from_off(isolated_machine):
    doc, _step, _groups, _wps = _make_doc()
    on = IntentBuilder(machine=isolated_machine, group_gcode_order=True)
    off = IntentBuilder(machine=isolated_machine, group_gcode_order=False)
    on_job = next(n for n in on.build(doc) if n.key == job_key())
    off_job = next(n for n in off.build(doc) if n.key == job_key())
    assert on_job.version_token != off_job.version_token


def test_on_groups_run_before_ungrouped_across_layers(isolated_machine):
    doc, step1, (ga, _gb), _wps = _make_doc()
    layer2 = doc.layers[1]
    assert layer2.workflow is not None
    step2 = _TestStep(name="s2")
    layer2.workflow.add_child(step2)
    layer2.add_child(WorkPiece(name="l2"))

    job = _job_node(isolated_machine, doc)
    keys = _group_input_keys(job)
    # Group A and B first (layer 1), then ungrouped per layer in
    # doc.layers order.
    assert keys[0] == [group_step_key(step1.uid, ga.uid)]
    assert keys[-2:] == [
        [group_step_key(step1.uid, None)],
        [group_step_key(step2.uid, None)],
    ]
    assert len(keys) == 4


def test_on_markers_repeat_per_layer_appearance(isolated_machine):
    doc, _step, _groups, _wps = _make_doc()
    job = _job_node(isolated_machine, doc)
    groups = job.stage.spec.groups
    # Layer 1 appears three times (A, B, ungrouped), each wrapped by
    # its own start/end markers.
    assert len(groups) == 3
    assert all(len(g.start_markers) == 1 for g in groups)
    assert all(len(g.end_markers) == 1 for g in groups)


def _group(*items, name="g"):
    g = Group(name=name)
    for item in items:
        g.add_child(item)
    return g


def _make_doc():
    """Two groups (A, B) plus one ungrouped workpiece, one step."""
    doc = Doc()
    layer = doc.active_layer
    step = _TestStep(name="s1")
    assert layer.workflow is not None
    layer.workflow.add_child(step)
    wpa, wpb, wpu = (WorkPiece(name=n) for n in ("a", "b", "u"))
    ga, gb = _group(wpa, name="A"), _group(wpb, name="B")
    layer.add_child(ga)
    layer.add_child(gb)
    layer.add_child(wpu)
    doc.group_execution_order = [ga.uid, gb.uid]
    return doc, step, (ga, gb), (wpa, wpb, wpu)


def _keys(machine, doc, group_order):
    builder = IntentBuilder(machine=machine, group_gcode_order=group_order)
    return [n.key for n in builder.build(doc)]


def test_off_keeps_layer_first_keys(isolated_machine):
    doc, step, _groups, _wps = _make_doc()
    keys = _keys(isolated_machine, doc, False)
    assert step_key(step.uid) in keys
    assert not any(k.startswith("stepgroup:") for k in keys)


def test_on_builds_one_step_aggregate_per_group(isolated_machine):
    doc, step, (ga, gb), (wpa, wpb, wpu) = _make_doc()
    keys = _keys(isolated_machine, doc, True)
    assert group_step_key(step.uid, ga.uid) in keys
    assert group_step_key(step.uid, gb.uid) in keys
    assert group_step_key(step.uid, None) in keys
    assert step_key(step.uid) not in keys
    assert job_key() in keys


def test_on_keeps_workpiece_compute_nodes(isolated_machine):
    doc, step, _groups, wps = _make_doc()
    keys = _keys(isolated_machine, doc, True)
    for wp in wps:
        assert workpiece_key(wp.uid, step.uid) in keys


def test_on_step_filter_limits_groups(isolated_machine):
    doc, step, (ga, gb), (wpa, _wpb, _wpu) = _make_doc()
    step.workpiece_uids = [wpa.uid]
    keys = _keys(isolated_machine, doc, True)
    assert group_step_key(step.uid, ga.uid) in keys
    assert group_step_key(step.uid, gb.uid) not in keys
    assert group_step_key(step.uid, None) not in keys


def test_on_without_groups_uses_ungrouped_only(isolated_machine):
    doc = Doc()
    layer = doc.active_layer
    step = _TestStep(name="s1")
    assert layer.workflow is not None
    layer.workflow.add_child(step)
    layer.add_child(WorkPiece(name="x"))
    keys = _keys(isolated_machine, doc, True)
    assert group_step_key(step.uid, None) in keys
    assert job_key() in keys

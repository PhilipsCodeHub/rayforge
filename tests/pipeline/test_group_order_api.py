from rayforge.core.doc import Doc


def _listen(doc: Doc):
    calls: list[str] = []

    def on_invalidated(sender, **kwargs):
        calls.append("invalidated")

    def on_updated(sender, **kwargs):
        calls.append("updated")

    # Keep references: blinker holds only weak references.
    doc.job_assembly_invalidated.connect(on_invalidated)
    doc.updated.connect(on_updated)
    return calls, (on_invalidated, on_updated)


def test_set_group_execution_order_stores_list():
    doc = Doc()
    doc.set_group_execution_order(["b", "a"])
    assert doc.group_execution_order == ["b", "a"]


def test_set_group_execution_order_removes_duplicates():
    doc = Doc()
    doc.set_group_execution_order(["a", "b", "a"])
    assert doc.group_execution_order == ["a", "b"]


def test_set_group_execution_order_copies_input():
    doc = Doc()
    uids = ["a", "b"]
    doc.set_group_execution_order(uids)
    uids.append("c")
    assert doc.group_execution_order == ["a", "b"]


def test_set_group_execution_order_sends_signals():
    doc = Doc()
    calls, _refs = _listen(doc)
    doc.set_group_execution_order(["a", "b"])
    assert calls.count("invalidated") == 1
    assert "updated" in calls


def test_set_group_execution_order_unchanged_sends_nothing():
    doc = Doc()
    doc.set_group_execution_order(["a", "b"])
    calls, _refs = _listen(doc)
    doc.set_group_execution_order(["a", "b"])
    assert calls == []


def test_group_execution_order_round_trips_through_dict():
    doc = Doc()
    doc.set_group_execution_order(["b", "a"])
    restored = Doc.from_dict(doc.to_dict())
    assert restored.group_execution_order == ["b", "a"]

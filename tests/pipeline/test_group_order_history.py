from rayforge.core.doc import Doc
from rayforge.core.undo.setter_cmd import SetterCommand


def _command(doc: Doc, new_order: list[str]) -> SetterCommand:
    return SetterCommand(
        doc,
        "set_group_execution_order",
        new_args=(list(new_order),),
        old_args=(list(doc.group_execution_order),),
        name="Change group order",
    )


def test_order_change_through_history_leaves_checkpoint():
    doc = Doc()
    history = doc.history_manager
    history.set_checkpoint()
    assert history.is_at_checkpoint()

    history.execute(_command(doc, ["b", "a"]))

    assert doc.group_execution_order == ["b", "a"]
    assert not history.is_at_checkpoint()


def test_order_change_can_be_undone_and_redone():
    doc = Doc()
    doc.set_group_execution_order(["a", "b"])
    history = doc.history_manager
    history.set_checkpoint()

    history.execute(_command(doc, ["b", "a"]))
    history.undo()
    assert doc.group_execution_order == ["a", "b"]
    assert history.is_at_checkpoint()

    history.redo()
    assert doc.group_execution_order == ["b", "a"]
    assert not history.is_at_checkpoint()


def test_undo_and_redo_send_the_rebuild_signal():
    doc = Doc()
    calls: list[str] = []

    def on_invalidated(sender, **kwargs):
        calls.append("invalidated")

    doc.job_assembly_invalidated.connect(on_invalidated)
    history = doc.history_manager

    history.execute(_command(doc, ["a", "b"]))
    history.undo()
    history.redo()

    assert calls.count("invalidated") == 3

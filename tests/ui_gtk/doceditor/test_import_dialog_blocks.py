# flake8: noqa: E402
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest
from gi.repository import Gtk
from raygeo.formats.dxf.writer import write
from raygeo.geo import Geometry

from rayforge.core.vectorization_spec import PassthroughSpec
from rayforge.doceditor.editor import DocEditor
from rayforge.image.base_importer import ImporterFeature
from rayforge.image.coreldraw.importer import CorelDrawImporter
from rayforge.image.dxf.importer import DxfImporter
from rayforge.image.gcode.importer import GcodeImporter
from rayforge.image.hpgl.importer import HpglImporter
from rayforge.ui_gtk.doceditor.import_dialog import ImportDialog

FEATURES = DxfImporter.features


@pytest.fixture
def editor(ui_context_initializer, ui_task_mgr):
    editor = DocEditor(
        task_manager=ui_task_mgr, context=ui_context_initializer
    )
    yield editor
    editor.cleanup()


def _make_dialog(editor, tmp_path, features=FEATURES) -> ImportDialog:
    geometry = Geometry()
    geometry.move_to(0, 0, 0)
    geometry.line_to(100, 0, 0)
    path = tmp_path / "part.dxf"
    path.write_bytes(write(geometry).encode("utf-8"))
    return ImportDialog(
        Gtk.Window(), editor, path, "image/vnd.dxf", features
    )


@pytest.mark.ui
def test_blocks_switch_visible_and_default_off(editor, tmp_path):
    dialog = _make_dialog(editor, tmp_path)

    assert dialog.blocks_group.get_visible()
    assert not dialog.blocks_switch.get_active()
    dialog.destroy()


@pytest.mark.ui
def test_blocks_switch_hidden_without_feature(editor, tmp_path):
    features = FEATURES - {ImporterFeature.BLOCK_GROUPS}
    dialog = _make_dialog(editor, tmp_path, features)

    assert not dialog.blocks_group.get_visible()
    dialog.destroy()


@pytest.mark.ui
def test_blocks_switch_goes_into_the_spec(editor, tmp_path):
    dialog = _make_dialog(editor, tmp_path)

    spec = dialog._get_current_spec()
    assert isinstance(spec, PassthroughSpec)
    assert not spec.blocks_as_groups

    dialog.blocks_switch.set_active(True)
    spec = dialog._get_current_spec()
    assert isinstance(spec, PassthroughSpec)
    assert spec.blocks_as_groups
    dialog.destroy()


@pytest.mark.ui
def test_blocks_switch_restored_from_spec(editor, tmp_path):
    dialog = _make_dialog(editor, tmp_path)

    dialog._apply_spec_to_widgets(PassthroughSpec(blocks_as_groups=True))
    assert dialog.blocks_switch.get_active()

    dialog._apply_spec_to_widgets(PassthroughSpec(blocks_as_groups=False))
    assert not dialog.blocks_switch.get_active()
    dialog.destroy()


@pytest.mark.ui
def test_dxf_derived_importers_do_not_offer_blocks():
    for cls in (CorelDrawImporter, GcodeImporter, HpglImporter):
        assert ImporterFeature.BLOCK_GROUPS not in cls.features

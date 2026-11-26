import pytest
import asyncio
from raygeo.geo import Matrix

from rayforge.core.layer import Layer
from rayforge.core.group import Group
from rayforge.core.item import DocItem
from rayforge.core.workpiece import WorkPiece
from rayforge.doceditor.editor import DocEditor


@pytest.fixture
def doc_editor(task_mgr, context_initializer):
    """
    Provides a DocEditor instance using the standard, fully-initialized
    test context provided by the `context_initializer` fixture.
    """
    editor = DocEditor(task_manager=task_mgr, context=context_initializer)
    yield editor
    # The editor's cleanup handles its internal resources like the pipeline.
    # The fixtures will handle their own teardown (context, task manager).
    editor.cleanup()


@pytest.fixture
def two_layers_with_items(doc_editor):
    doc = doc_editor.doc
    layer_a = doc.active_layer
    layer_b = Layer(name="Layer B")
    doc.add_layer(layer_b)

    item_a = WorkPiece(name="item_a.svg")
    item_b = WorkPiece(name="item_b.svg")
    layer_a.add_child(item_a)
    layer_b.add_child(item_b)

    return layer_a, layer_b, item_a, item_b


@pytest.mark.asyncio
async def wait_for_grouping(doc_editor):
    while doc_editor.is_processing:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_group_tags_origin_layer(doc_editor, two_layers_with_items):
    layer_a, layer_b, item_a, item_b = two_layers_with_items
    doc_editor.group.group_items(layer_a, [item_a, item_b])
    await wait_for_grouping(doc_editor)
    assert item_a.extra["origin_layer_uid"] == layer_a.uid
    assert item_b.extra["origin_layer_uid"] == layer_b.uid


@pytest.mark.asyncio
async def test_ungroup_restores_original_layers(
    doc_editor, two_layers_with_items
):
    layer_a, layer_b, item_a, item_b = two_layers_with_items
    world_a = item_a.get_world_transform()
    world_b = item_b.get_world_transform()

    doc_editor.group.group_items(layer_a, [item_a, item_b])
    await wait_for_grouping(doc_editor)
    group = next(c for c in layer_a.children if isinstance(c, Group))
    doc_editor.group.ungroup_items([group])
    await wait_for_grouping(doc_editor)

    assert item_a.parent is layer_a
    assert item_b.parent is layer_b
    # world position preserved
    assert item_a.get_world_transform() == world_a
    assert item_b.get_world_transform() == world_b
    assert "origin_layer_uid" not in item_a.extra
    assert "origin_layer_uid" not in item_b.extra


@pytest.mark.asyncio
async def test_ungroup_falls_back_when_origin_layer_gone(
    doc_editor, two_layers_with_items
):
    layer_a, layer_b, item_a, item_b = two_layers_with_items
    doc_editor.group.group_items(layer_a, [item_a, item_b])
    await wait_for_grouping(doc_editor)
    group = next(c for c in layer_a.children if isinstance(c, Group))
    # Adapt to the actual API used in other tests.
    doc_editor.doc.remove_layer(layer_b)
    doc_editor.group.ungroup_items([group])
    await wait_for_grouping(doc_editor)
    assert item_a.parent is layer_a
    assert item_b.parent is layer_a  # fallback


@pytest.mark.asyncio
async def test_regroup_overwrites_stale_tag(doc_editor, two_layers_with_items):
    layer_a, layer_b, item_a, item_b = two_layers_with_items
    doc_editor.group.group_items(layer_a, [item_a, item_b])
    await wait_for_grouping(doc_editor)
    group = next(c for c in layer_a.children if isinstance(c, Group))
    doc_editor.group.ungroup_items([group])
    await wait_for_grouping(doc_editor)
    # item_b's tag was removed on ungroup; regroup should re-tag with
    # whatever its current layer is.
    doc_editor.group.group_items(layer_a, [item_a, item_b])
    await wait_for_grouping(doc_editor)
    assert item_b.extra["origin_layer_uid"] == layer_b.uid


@pytest.mark.asyncio
async def test_group_and_ungroup_preserves_transform(doc_editor: DocEditor):
    """
    Tests that grouping and then ungrouping items restores their original
    local transformations relative to their parent layer. This is the core
    test for the user-reported bug.
    """
    # 1. Setup: Create items with known transforms in the active layer
    layer = doc_editor.doc.active_layer

    wp1 = WorkPiece(name="wp1.svg")
    # A simple translation and scale
    wp1.matrix = Matrix.translation(10, 20) @ Matrix.scale(5, 5)
    layer.add_child(wp1)

    wp2 = WorkPiece(name="wp2.svg")
    # A more complex transform with rotation
    wp2.matrix = (
        Matrix.translation(50, 60) @ Matrix.rotation(45) @ Matrix.scale(8, 2)
    )
    layer.add_child(wp2)

    # Capture original state (local matrices and world transforms for
    # good measure)
    original_wp1_matrix = wp1.matrix.copy()
    original_wp2_matrix = wp2.matrix.copy()
    original_wp1_world = wp1.get_world_transform()
    original_wp2_world = wp2.get_world_transform()

    # 2. Group the items using the command handler
    # Explicitly type hint the list to satisfy type checkers (List[Sub] is not
    # a subtype of List[Base]).
    items_to_group: list[DocItem] = [wp1, wp2]
    doc_editor.group.group_items(layer, items_to_group)
    await doc_editor.wait_until_settled(timeout=2)

    # Assert that grouping worked as expected
    assert len(layer.get_content_items()) == 1
    group = layer.get_content_items()[0]
    assert isinstance(group, Group)
    assert len(group.children) == 2
    assert wp1 in group.children
    assert wp2 in group.children

    # World transforms must be preserved after grouping
    assert wp1.get_world_transform() == original_wp1_world
    assert wp2.get_world_transform() == original_wp2_world

    # 3. Ungroup the items using the command handler
    doc_editor.group.ungroup_items([group])
    await doc_editor.wait_until_settled()

    # 4. Assert: Check if items are back in the layer with original transforms
    content_items = layer.get_content_items()
    assert len(content_items) == 2
    assert wp1 in content_items
    assert wp2 in content_items
    assert group.parent is None  # Group should be detached

    # The critical check: are the local matrices restored?
    assert wp1.matrix == original_wp1_matrix
    assert wp2.matrix == original_wp2_matrix

    # Double check world transforms as well
    assert wp1.get_world_transform() == original_wp1_world
    assert wp2.get_world_transform() == original_wp2_world

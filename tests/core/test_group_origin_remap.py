from rayforge.core.group import ORIGIN_LAYER_UID, Group, remap_origin_layers
from rayforge.core.layer import Layer


def test_remaps_tags_in_nested_items():
    layer = Layer(name="Imported")
    outer = Group(name="outer")
    inner = Group(name="inner")
    outer.add_child(inner)
    layer.add_child(outer)
    outer.extra[ORIGIN_LAYER_UID] = "old"
    inner.extra[ORIGIN_LAYER_UID] = "old"

    remap_origin_layers([layer], {"old": "new"})

    assert outer.extra[ORIGIN_LAYER_UID] == "new"
    assert inner.extra[ORIGIN_LAYER_UID] == "new"


def test_leaves_other_and_missing_tags_alone():
    layer = Layer(name="Imported")
    tagged = Group(name="tagged")
    plain = Group(name="plain")
    layer.add_child(tagged)
    layer.add_child(plain)
    tagged.extra[ORIGIN_LAYER_UID] = "other"

    remap_origin_layers([layer], {"old": "new"})

    assert tagged.extra[ORIGIN_LAYER_UID] == "other"
    assert ORIGIN_LAYER_UID not in plain.extra


def test_empty_map_changes_nothing():
    group = Group(name="g")
    group.extra[ORIGIN_LAYER_UID] = "old"
    remap_origin_layers([group], {})
    assert group.extra[ORIGIN_LAYER_UID] == "old"

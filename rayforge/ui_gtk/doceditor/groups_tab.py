"""
Groups tab for the bottom panel.

Shows one row per group: optional visibility toggle, editable name,
child count, current layer and (optionally) the layer(s) the group's
elements were preserved from.

All access to the document model is isolated in the "adapter" section
below, so that the widgets do not change when the model changes, the
"preserved from" information is optional (it only exists on
feature/preserve-layer-on-group) and the group order can later be
changed in exactly one place (get_ordered_groups).
"""

import logging
from collections.abc import Iterator
from gettext import gettext as _
from gettext import ngettext
from typing import TYPE_CHECKING, NamedTuple

from blinker import Signal
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from ...core.doc import Doc
from ...core.group import Group
from ...core.workpiece import WorkPiece
from ..icons import get_icon
from ..shared.gtk import apply_css
from .group_row import GroupRow

if TYPE_CHECKING:
    from ...doceditor.editor import DocEditor

logger = logging.getLogger(__name__)

_CSS = """
.groups-list > row.selected-row {
    background-color: alpha(@accent_bg_color, 0.2);
}
"""

# Indentation per nesting level, in pixels.
_INDENT = 12

# Written by feature/preserve-layer-on-group (group_cmd.ORIGIN_LAYER_UID)
# into each grouped child's `extra` dict. Kept as a literal on purpose:
# the constant does not exist on main, so importing it would break there.
ORIGIN_LAYER_KEY = "origin_layer_uid"

# Window actions used by the context menu. They operate on the current
# canvas selection, exactly like the main menu and the keyboard.
# TODO: verify these names against ui_gtk/actions.py.
ACTION_UNGROUP = "win.ungroup"
ACTION_DELETE = "win.remove"


# --------------------------------------------------------------------------
# Adapters: the ONLY place that knows how groups are stored.
# --------------------------------------------------------------------------


class GroupState(NamedTuple):
    """Everything a row displays. Equal states need no UI update."""

    name: str
    child_count: int
    layer_name: str
    origin_names: tuple[str, ...]
    has_visibility: bool
    visible: bool


def _iter_groups(item, depth: int = 0) -> Iterator[tuple[Group, int]]:
    for child in getattr(item, "children", ()):
        if isinstance(child, Group):
            yield child, depth
            yield from _iter_groups(child, depth + 1)
        elif not isinstance(child, WorkPiece):
            yield from _iter_groups(child, depth)


def get_ordered_groups(doc: Doc) -> list[tuple[Group, int]]:
    """
    Returns (group, nesting depth) in display order.

    Currently: document traversal order, groups at all depths. To list
    only top-level groups (like the Layers panel), keep depth == 0.
    When the group-ordering feature exists, change only this function.
    """
    return list(_iter_groups(doc))


def _layer_name(layer) -> str:
    if layer is None:
        return ""
    return layer.name or _("Layer")


def group_origin_layers(group: Group, layers_by_uid: dict) -> list:
    """
    Layers the group's children were taken from, in child order, without
    duplicates. Returns [] when there is no origin information (always
    the case on main), when every child came from the group's own layer
    or when the origin layer no longer exists.
    """
    origins: list = []
    for child in group.children:
        uid = (getattr(child, "extra", None) or {}).get(ORIGIN_LAYER_KEY)
        layer = layers_by_uid.get(uid) if uid else None
        if layer is not None and layer not in origins:
            origins.append(layer)
    if origins == [getattr(group, "layer", None)]:
        return []
    return origins


def get_group_state(group: Group, layers_by_uid: dict) -> GroupState:
    return GroupState(
        name=group.name or _("Group"),
        child_count=len(group.children),
        layer_name=_layer_name(getattr(group, "layer", None)),
        origin_names=tuple(
            _layer_name(layer)
            for layer in group_origin_layers(group, layers_by_uid)
        ),
        # DocItem and Group have no visibility attribute today, so the
        # toggle stays hidden until the model provides one.
        has_visibility=hasattr(group, "visible"),
        visible=bool(getattr(group, "visible", True)),
    )


def set_group_name(editor: "DocEditor", group: Group, name: str):
    # Same undoable command that LayerColumn uses for item renames.
    editor.edit.rename_item(group, name)


def set_group_visible(editor: "DocEditor", group: Group, visible: bool):
    # TODO: replace with an undoable command, like
    # editor.layer.set_layer_visibility, once groups can be hidden.
    group.visible = visible


# --------------------------------------------------------------------------
# Row widget
# --------------------------------------------------------------------------


class GroupListRow(Gtk.ListBoxRow):
    def __init__(self, tab: "GroupsTab", group: Group, depth: int):
        super().__init__()
        self._tab = tab
        self.group = group
        self._state: GroupState | None = None

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        box.set_margin_start(_INDENT * depth)
        self.set_child(box)

        self.vis_button = Gtk.ToggleButton()
        self.vis_button.add_css_class("flat")
        self.vis_button.set_valign(Gtk.Align.CENTER)
        self.vis_button.set_tooltip_text(_("Toggle group visibility"))
        self._vis_handler_id = self.vis_button.connect(
            "toggled", self._on_visibility_toggled
        )
        box.append(self.vis_button)

        # Reuse the Layers panel row: it provides in-place rename
        # (double-click, Enter, Escape, focus-out, click-outside).
        self.group_row = GroupRow(group, on_rename=tab.rename_group)
        self.group_row.set_hexpand(True)
        box.append(self.group_row)

        self.count_label = Gtk.Label()
        self.count_label.add_css_class("dim-label")
        box.append(self.count_label)

        self.layer_name_label = Gtk.Label()
        self.layer_name_label.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(self.layer_name_label)

        self.origin_label = Gtk.Label()
        self.origin_label.add_css_class("dim-label")
        self.origin_label.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(self.origin_label)

        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)

        right_click = Gtk.GestureClick()
        right_click.set_button(Gdk.BUTTON_SECONDARY)
        right_click.connect("pressed", self._on_right_pressed)
        self.add_controller(right_click)

    def apply(self, state: GroupState):
        """Updates the widgets, but only if the displayed data changed."""
        if state == self._state:
            return
        self._state = state

        n = state.child_count
        self.count_label.set_label(
            ngettext("{n} item", "{n} items", n).format(n=n)
        )

        self.layer_name_label.set_label(state.layer_name)
        self.layer_name_label.set_visible(bool(state.layer_name))

        origin = ", ".join(state.origin_names)
        self.origin_label.set_visible(bool(origin))
        if origin:
            self.origin_label.set_label(
                _("from: {layers}").format(layers=origin)
            )

        self.vis_button.set_visible(state.has_visibility)
        self.vis_button.handler_block(self._vis_handler_id)
        self.vis_button.set_active(state.visible)
        self.vis_button.handler_unblock(self._vis_handler_id)
        self.vis_button.set_child(
            get_icon(
                "visibility-on-symbolic"
                if state.visible
                else "visibility-off-symbolic"
            )
        )
        self.set_opacity(1.0 if state.visible else 0.5)

    def set_selected(self, selected: bool):
        if selected:
            self.add_css_class("selected-row")
        else:
            self.remove_css_class("selected-row")

    def _on_visibility_toggled(self, button):
        set_group_visible(self._tab.editor, self.group, button.get_active())

    def _on_right_pressed(self, gesture, n_press, x, y):
        # While the name editor is open, leave the entry's own menu alone.
        if not self.group_row.is_renaming():
            self._tab.show_context_menu(self, gesture)

    def _on_pressed(self, gesture, n_press, x, y):
        # Double-click is handled by GroupRow (rename). While the name
        # editor is open, clicks belong to the entry.
        if n_press == 1 and not self.group_row.is_renaming():
            self._tab.select_group(self.group)


# --------------------------------------------------------------------------
# Tab
# --------------------------------------------------------------------------


class GroupsTab(Gtk.Box):
    def __init__(self, editor: "DocEditor"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        apply_css(_CSS)
        self.editor = editor
        self.doc: Doc | None = editor.doc
        self._rows: list[GroupListRow] = []
        self._structure: tuple | None = None
        self._selected_uids: set = set()
        self._refresh_pending = False
        self._context_popover: Gtk.PopoverMenu | None = None
        self._context_group: Group | None = None
        self._pending_rename: Group | None = None

        self.select_items_requested = Signal()

        # Local action for the context menu. Ungroup and Delete use the
        # window actions instead (see ACTION_UNGROUP / ACTION_DELETE).
        actions = Gio.SimpleActionGroup()
        rename_action = Gio.SimpleAction.new("rename", None)
        rename_action.connect("activate", self._on_rename_action)
        actions.add_action(rename_action)
        self.insert_action_group("groups", actions)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        # "layer-workpiece-list" gives the rows the same look as the
        # Layers panel, including the compact rename entry of GroupRow.
        self.listbox.add_css_class("layer-workpiece-list")
        self.listbox.add_css_class("groups-list")

        placeholder = Gtk.Label(label=_("No groups in this document"))
        placeholder.add_css_class("dim-label")
        placeholder.set_margin_top(18)
        placeholder.set_margin_bottom(18)
        self.listbox.set_placeholder(placeholder)

        scrolled.set_child(self.listbox)
        self.append(scrolled)

        self._connect_signals()
        self._refresh_now()
        self.connect("destroy", self._on_destroy)

    # Document ---------------------------------------------------------

    def set_doc(self, doc: Doc | None):
        if self.doc is doc:
            return
        self._disconnect_signals()
        self.doc = doc
        self._structure = None  # force a rebuild
        self._connect_signals()
        self._refresh_now()

    def _connect_signals(self):
        if self.doc is None:
            return
        self.doc.descendant_added.connect(self._on_changed)
        self.doc.descendant_removed.connect(self._on_changed)
        self.doc.descendant_renamed.connect(self._on_changed)
        self.doc.updated.connect(self._on_changed)

    def _disconnect_signals(self):
        if self.doc is None:
            return
        self.doc.descendant_added.disconnect(self._on_changed)
        self.doc.descendant_removed.disconnect(self._on_changed)
        self.doc.descendant_renamed.disconnect(self._on_changed)
        self.doc.updated.disconnect(self._on_changed)

    def _on_destroy(self, *args):
        self._dismiss_popover()
        self._disconnect_signals()

    # Actions used by the rows -------------------------------------------

    def rename_group(self, group: Group, new_name: str):
        set_group_name(self.editor, group, new_name)

    def select_group(self, group: Group):
        self.select_items_requested.send(
            self, items=[group], extend=False
        )

    # Context menu -------------------------------------------------------

    def show_context_menu(self, row: GroupListRow, gesture: Gtk.Gesture):
        group = row.group
        # Like the Layers panel: right-clicking an unselected group
        # selects it; right-clicking a selected one keeps the selection,
        # so Ungroup and Delete apply to everything selected.
        if group.uid not in self._selected_uids:
            self.select_items_requested.send(
                self, items=[group], extend=False
            )
        self._context_group = group

        edit_section = Gio.Menu.new()
        edit_section.append_item(
            Gio.MenuItem.new(_("Rename"), "groups.rename")
        )
        edit_section.append_item(
            Gio.MenuItem.new(_("Ungroup"), ACTION_UNGROUP)
        )
        delete_section = Gio.Menu.new()
        delete_section.append_item(
            Gio.MenuItem.new(_("Delete"), ACTION_DELETE)
        )
        menu = Gio.Menu.new()
        menu.append_section(None, edit_section)
        menu.append_section(None, delete_section)

        self._dismiss_popover()
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(row)
        popover.set_has_arrow(False)
        ok, rect = gesture.get_bounding_box()
        if ok:
            popover.set_pointing_to(rect)
        popover.connect("closed", self._on_popover_closed)
        self._context_popover = popover
        popover.popup()

    def _dismiss_popover(self):
        if self._context_popover is not None:
            self._context_popover.unparent()
            self._context_popover = None

    def _on_rename_action(self, action, param):
        # Don't start the editor yet: the closing popover would give the
        # focus back to the row and immediately end the rename.
        self._pending_rename = self._context_group

    def _on_popover_closed(self, popover):
        group = self._pending_rename
        self._pending_rename = None
        if group is not None:
            GLib.idle_add(self._start_rename, group)

    def _start_rename(self, group: Group):
        for row in self._rows:
            if row.group is group:
                row.group_row.start_rename()
                break
        return GLib.SOURCE_REMOVE

    # Selection feedback -------------------------------------------------

    def update_row_selection(self, selected_uids: set):
        self._selected_uids = set(selected_uids)
        for row in self._rows:
            row.set_selected(row.group.uid in self._selected_uids)

    # Refresh ------------------------------------------------------------

    def _on_changed(self, sender, **kwargs):
        # Several signals usually fire for one user action (and may come
        # from worker threads), so coalesce them into one refresh on the
        # main loop.
        if self._refresh_pending:
            return
        self._refresh_pending = True
        GLib.idle_add(self._on_idle)

    def _on_idle(self):
        self._refresh_pending = False
        self._refresh_now()
        return GLib.SOURCE_REMOVE

    def _refresh_now(self):
        if self.doc is None:
            entries: list[tuple[Group, int]] = []
            layers_by_uid: dict = {}
        else:
            entries = get_ordered_groups(self.doc)
            layers_by_uid = {layer.uid: layer for layer in self.doc.layers}

        structure = tuple((g.uid, depth) for g, depth in entries)
        if structure != self._structure:
            self._rebuild(entries)
            self._structure = structure

        # Same groups in the same order: update rows in place. This
        # keeps an open rename editor alive while other things change.
        for row in self._rows:
            row.apply(get_group_state(row.group, layers_by_uid))

    def _rebuild(self, entries: list[tuple[Group, int]]):
        self._dismiss_popover()
        child = self.listbox.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.listbox.remove(child)
            child = next_child
        self._rows.clear()

        for group, depth in entries:
            row = GroupListRow(self, group, depth)
            row.set_selected(group.uid in self._selected_uids)
            self._rows.append(row)
            self.listbox.append(row)

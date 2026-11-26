import asyncio
import logging
from collections import defaultdict
from gettext import gettext as _
from typing import TYPE_CHECKING, Any

from raygeo.geo import Matrix

from ..core.group import ORIGIN_LAYER_UID, Group, GroupingResult
from ..core.item import DocItem
from ..core.layer import Layer
from ..core.undo.command import Command

if TYPE_CHECKING:
    from ..pipeline.pipeline import Pipeline
    from ..shared.tasker.context import ExecutionContext
    from ..shared.tasker.manager import TaskManager
    from ..shared.tasker.task import Task
    from .editor import DocEditor

logger = logging.getLogger(__name__)


def _extra(item: DocItem) -> dict[str, Any]:
    """The ``extra`` dict of a WorkPiece or Group (both declare one)."""
    return getattr(item, "extra")


class _CreateGroupCommand(Command):
    """An undoable command to group a list of DocItems into a new Group."""

    def __init__(
        self,
        layer: Layer,
        items_to_group: list[DocItem],
        pipeline: "Pipeline",
        name: str = "Group Items",
        precalculated_result: GroupingResult | None = None,
    ):
        super().__init__(name)
        self.layer = layer
        self.items_to_group = list(items_to_group)
        self.pipeline = pipeline
        self.new_group: Group | None = None
        self._original_parents: dict[str, DocItem] = {
            item.uid: item.parent
            for item in self.items_to_group
            if item.parent
        }
        self._original_matrices: dict[str, Matrix] = {
            item.uid: item.matrix.copy() for item in self.items_to_group
        }
        # Snapshot the previous origin-layer tags so undo() can restore
        # them exactly (including the "was absent" case).
        self._previous_origin_tags: dict[str, str | None] = {
            item.uid: _extra(item).get(ORIGIN_LAYER_UID)
            for item in self.items_to_group
        }
        self._precalculated_result = precalculated_result

    def execute(self) -> None:
        """Performs the grouping operation."""
        # The result must be pre-calculated by the background task.
        if not self._precalculated_result:
            return

        result = self._precalculated_result

        # Pause the generator to prevent it from reacting to the storm of
        # add/remove signals during the model mutation.
        with self.pipeline.paused():
            # Tag each selected item with the Layer it currently sits on,
            # BEFORE reparenting. Always overwrite a stale tag (e.g. an item
            # copied out of a group would otherwise carry the old uid).
            for item in self.items_to_group:
                owner = item.get_ancestor_by_type(Layer)
                if owner is not None:
                    _extra(item)[ORIGIN_LAYER_UID] = owner.uid

            self.new_group = result.new_group
            self.layer.add_child(self.new_group)

            # --- Bulk Reparenting to Prevent Signal Storm ---
            # 1. Group items by their original parent.
            items_by_parent = defaultdict(list)
            for item in self.items_to_group:
                if item.parent:
                    items_by_parent[item.parent].append(item)

            # 2. Remove items from old parents in batches.
            for parent, items in items_by_parent.items():
                parent.remove_children(items)

            # 3. Add all items to the new group in one batch.
            self.new_group.add_children(self.items_to_group)

            # 4. Set final local matrices *after* reparenting is complete.
            for item in self.items_to_group:
                item.matrix = result.child_matrices[item.uid]

    def undo(self) -> None:
        """Reverts the grouping operation."""
        if not self.new_group:
            return

        with self.pipeline.paused():
            # --- Bulk Reparenting for Undo ---
            # 1. Remove all children from the group in one batch.
            self.new_group.remove_children(self.items_to_group)

            # 2. Group items by their original parent for re-adding.
            items_by_original_parent = defaultdict(list)
            for item in self.items_to_group:
                original_parent = self._original_parents.get(item.uid)
                if original_parent:
                    items_by_original_parent[original_parent].append(item)

            # 3. Re-add items to their original parents in batches and
            # restore their original matrices.
            for parent, items in items_by_original_parent.items():
                parent.add_children(items)
            for item in self.items_to_group:
                item.matrix = self._original_matrices[item.uid]

            # Restore origin-layer tags exactly.
            for item in self.items_to_group:
                previous = self._previous_origin_tags.get(item.uid)
                if previous is None:
                    _extra(item).pop(ORIGIN_LAYER_UID, None)
                else:
                    _extra(item)[ORIGIN_LAYER_UID] = previous

            # 5. Finally, remove the now-empty group.
            self.layer.remove_child(self.new_group)


class _UngroupCommand(Command):
    """An undoable command to dissolve one or more Groups."""

    def __init__(
        self,
        groups_to_ungroup: list[Group],
        pipeline: "Pipeline",
        name: str = "Ungroup Items",
        precalculated_matrices: dict[str, dict[str, Matrix]] | None = None,
        child_targets: dict[str, DocItem] | None = None,
    ):
        super().__init__(name)
        self.groups_to_ungroup = list(groups_to_ungroup)
        self.pipeline = pipeline
        self._precalculated_matrices = precalculated_matrices
        # child_uid -> the parent the child should actually be moved to
        # on execute(). If missing, the group's own parent is used.
        self._child_targets: dict[str, DocItem] = child_targets or {}
        # child_uid -> prior value of the origin-layer tag (None = absent).
        self._previous_tags: dict[str, str | None] = {}
        for group in self.groups_to_ungroup:
            for child in group.children:
                self._previous_tags[child.uid] = _extra(child).get(
                    ORIGIN_LAYER_UID
                )
        self._undo_data = []
        for group in self.groups_to_ungroup:
            if group.parent:
                self._undo_data.append(
                    {
                        "group_uid": group.uid,
                        "group_matrix": group.matrix.copy(),
                        "parent": group.parent,
                        "group_index": group.parent.children.index(group),
                        "children": list(group.children),
                        "child_matrices": {
                            c.uid: c.matrix.copy() for c in group.children
                        },
                    }
                )

    @staticmethod
    def _calculate_ungroup_transforms(
        group: Group,
        target_for,
    ) -> dict[str, Matrix]:
        """
        Calculate new local matrices for a group's children, using the
        inverse world transform of each child's *actual* target parent.

        ``target_for(child)`` must return the parent the child will land on.
        """
        group_world_transform = group.get_world_transform()
        inverse_cache: dict[str, Matrix] = {}
        new_child_matrices: dict[str, Matrix] = {}
        for child in group.children:
            target = target_for(child)
            if target.uid not in inverse_cache:
                inverse_cache[target.uid] = (
                    target.get_world_transform().invert()
                )
            child_world_transform = group_world_transform @ child.matrix
            new_child_matrices[child.uid] = (
                inverse_cache[target.uid] @ child_world_transform
            )
        return new_child_matrices

    def execute(self) -> None:
        """Performs the ungrouping operation."""
        with self.pipeline.paused():
            for group in self.groups_to_ungroup:
                parent = group.parent
                if not parent:
                    continue

                try:
                    group_index = parent.children.index(group)
                except ValueError:
                    continue  # Should not happen

                children_to_move = list(group.children)

                if self._precalculated_matrices:
                    if group.uid not in self._precalculated_matrices:
                        continue
                    new_child_matrices = self._precalculated_matrices[
                        group.uid
                    ]
                else:
                    # This path is for safety but should not be used by the
                    # async command.
                    new_child_matrices = self._calculate_ungroup_transforms(
                        group,
                        lambda c, p=parent: self._child_targets.get(c.uid, p),
                    )

                # Set new matrices before reparenting
                for child in children_to_move:
                    child.matrix = new_child_matrices[child.uid]

                parent.remove_child(group)

                # Route each child to its target parent. Children that
                # share a target are added in one batch; children that
                # stay on the original parent keep the group's former
                # index, matching today's behavior.
                by_target: dict[DocItem, list[DocItem]] = defaultdict(list)
                for child in children_to_move:
                    target = self._child_targets.get(child.uid, parent)
                    by_target[target].append(child)

                for target, kids in by_target.items():
                    if target is parent:
                        target.add_children(kids, index=group_index)
                    else:
                        target.add_children(kids)

                # The tag has served its purpose.
                for child in children_to_move:
                    _extra(child).pop(ORIGIN_LAYER_UID, None)

    def undo(self) -> None:
        """Reverts the ungrouping by re-creating the original groups."""
        with self.pipeline.paused():
            for data in reversed(self._undo_data):
                parent: DocItem = data["parent"]
                group_index = data["group_index"]
                children = data["children"]
                group = next(
                    (
                        g
                        for g in self.groups_to_ungroup
                        if g.uid == data["group_uid"]
                    ),
                    None,
                )
                if not group:
                    continue

                # Restore tags before moving anything.
                for child in children:
                    previous = self._previous_tags.get(child.uid)
                    if previous is None:
                        _extra(child).pop(ORIGIN_LAYER_UID, None)
                    else:
                        _extra(child)[ORIGIN_LAYER_UID] = previous

                # Restore matrices first
                group.matrix = data["group_matrix"]
                for child in children:
                    child.matrix = data["child_matrices"][child.uid]

                # Remove each child from the parent it actually went to.
                by_actual: dict[DocItem, list[DocItem]] = defaultdict(list)
                for child in children:
                    actual = self._child_targets.get(child.uid, parent)
                    by_actual[actual].append(child)
                for actual, kids in by_actual.items():
                    actual.remove_children(kids)

                group.set_children(children)  # Fast, as group starts empty

                # Add group back to its parent and restore its matrix
                parent.add_child(group, index=group_index)


class GroupCmd:
    """Handles grouping and ungrouping of document items."""

    def __init__(self, editor: "DocEditor", task_manager: "TaskManager"):
        self._editor = editor
        self._task_manager = task_manager

    def group_items(self, layer: Layer, items_to_group: list[DocItem]):
        """
        Creates and executes an undoable command to group items. This operation
        runs as a background task.
        """
        if not items_to_group:
            return

        # Notify editor that we are starting a background task
        self._editor.notify_task_started()

        async def group_coro(context: "ExecutionContext"):
            context.set_message(_("Grouping items..."))
            context.flush()
            await asyncio.sleep(0.01)

            result = Group.create_from_items(items_to_group, layer)

            context.set_progress(1.0)
            return result

        def when_done(task: "Task"):
            try:
                if task.get_status() != "completed":
                    logger.error(
                        "Group task did not complete successfully. Status: %s",
                        task.get_status(),
                    )
                    return

                result: GroupingResult | None = task.result()
                if not result:
                    return

                command = _CreateGroupCommand(
                    layer=layer,
                    items_to_group=items_to_group,
                    pipeline=self._editor.pipeline,
                    precalculated_result=result,
                )
                self._editor.history_manager.execute(command)
            finally:
                # Always notify editor when done, even on failure
                self._editor.notify_task_ended()

        self._task_manager.add_coroutine(
            group_coro,
            when_done=when_done,
            key="group-items",
        )

    def _find_layer_by_uid(self, uid: str, anchor: DocItem) -> Layer | None:
        """
        Locate a Layer by uid.

        Walks up from ``anchor`` to the topmost ancestor and then DFSes
        the tree. This assumes layers share a common root in the
        document model.

        TODO(reviewer): if the document exposes a layer registry, use it
        here instead. This tree walk is a proof-of-concept shortcut.
        """
        node = anchor
        seen_up: set[str] = set()
        while node.parent is not None and node.uid not in seen_up:
            seen_up.add(node.uid)
            node = node.parent
        stack = [node]
        seen_down: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur.uid in seen_down:
                continue
            seen_down.add(cur.uid)
            if isinstance(cur, Layer) and cur.uid == uid:
                return cur
            for child in getattr(cur, "children", []) or []:
                stack.append(child)
        return None

    def ungroup_items(self, groups_to_ungroup: list[Group]):
        """
        Creates and executes an undoable command to ungroup items. This
        operation runs as a background task.
        """
        if not groups_to_ungroup:
            return

        # Notify editor that we are starting a background task
        self._editor.notify_task_started()

        def do_calculation_sync():
            # Decide where each child should land.
            child_targets: dict[str, DocItem] = {}
            for group in groups_to_ungroup:
                parent = group.parent
                if not parent:
                    continue
                if not isinstance(parent, Layer):
                    # Nested group: keep today's behavior.
                    for child in group.children:
                        child_targets[child.uid] = parent
                    continue
                for child in group.children:
                    uid = _extra(child).get(ORIGIN_LAYER_UID)
                    target = parent
                    if uid:
                        origin = self._find_layer_by_uid(uid, parent)
                        if origin is not None:
                            target = origin
                        else:
                            logger.warning(
                                "Ungroup: origin layer uid=%s not found "
                                "for item uid=%s; falling back to the "
                                "group's parent.",
                                uid,
                                child.uid,
                            )
                    child_targets[child.uid] = target

            # Compute new local matrices using per-child target inverses.
            results: dict[str, dict[str, Matrix]] = {}
            for group in groups_to_ungroup:
                if not group.parent:
                    continue
                parent = group.parent

                def target_for(child, p=parent, t=child_targets):
                    return t.get(child.uid, p)

                results[group.uid] = (
                    _UngroupCommand._calculate_ungroup_transforms(
                        group, target_for
                    )
                )
            return child_targets, results

        async def ungroup_coro(context: "ExecutionContext"):
            context.set_message(_("Ungrouping items..."))
            context.flush()
            await asyncio.sleep(0.01)

            child_targets, matrices = do_calculation_sync()

            context.set_progress(1.0)
            return child_targets, matrices

        def when_done(task: "Task"):
            try:
                if task.get_status() != "completed":
                    logger.error(
                        "Ungroup task did not complete successfully. "
                        f"Status: {task.get_status()}",
                    )
                    return

                result = task.result()
                if not result:
                    return
                child_targets, calculated_matrices = result

                command = _UngroupCommand(
                    groups_to_ungroup=groups_to_ungroup,
                    pipeline=self._editor.pipeline,
                    precalculated_matrices=calculated_matrices,
                    child_targets=child_targets,
                )
                self._editor.history_manager.execute(command)
            finally:
                # Always notify editor when done, even on failure
                self._editor.notify_task_ended()

        self._task_manager.add_coroutine(
            ungroup_coro,
            when_done=when_done,
            key="ungroup-items",
        )

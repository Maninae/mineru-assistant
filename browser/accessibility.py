#!/usr/bin/env python3
"""
Mineru Browser Server — Accessibility Tree & Ref Registry

Builds CDP-based accessibility tree snapshots and assigns short ref IDs (e1,
e2, ...) to interactive elements so callers can refer to them in subsequent
action requests. Falls back to ARIA snapshot if CDP fails.
"""

from typing import Any, Dict, List

from browser.config import INTERACTIVE_ROLES, VISIBLE_ROLES, logger


class RefRegistry:
    """Manages ref IDs for interactive elements across tabs."""

    def __init__(self):
        # type: () -> None
        self._ref_maps = {}  # type: Dict[str, Dict[str, Dict[str, Any]]]
        self._ref_counter = 0

    def next_ref(self):
        # type: () -> str
        self._ref_counter += 1
        return "e%d" % self._ref_counter

    def reset_counter(self):
        # type: () -> None
        """Reset the ref counter (called at the start of each snapshot)."""
        self._ref_counter = 0

    def get_map(self, target_id):
        # type: (str) -> Dict[str, Dict[str, Any]]
        return self._ref_maps.get(target_id, {})

    def set_map(self, target_id, ref_map):
        # type: (str, Dict[str, Dict[str, Any]]) -> None
        self._ref_maps[target_id] = ref_map

    def pop(self, target_id):
        # type: (str) -> Any
        return self._ref_maps.pop(target_id, None)

    def clear_all(self):
        # type: () -> None
        """Clear all ref maps. (Counter is reset per-snapshot, not here —
        matches the original _cleanup_browser behavior.)"""
        self._ref_maps.clear()


def build_snapshot(page, cdp, registry, target_id):
    # type: (Any, Any, RefRegistry, str) -> Dict[str, Any]
    """Build accessibility tree snapshot for a page. Returns dict.

    The caller is responsible for:
      - Holding the BrowserManager lock
      - Ensuring the browser is alive
      - Storing the returned ref_map back into the registry

    This function resets the ref counter, fetches the CDP accessibility tree,
    walks it, and produces a flat text representation + ref map. On CDP
    failure, falls back to ARIA snapshot (no refs available).
    """
    # Reset ref counter for this snapshot
    registry.reset_counter()
    ref_map = {}  # type: Dict[str, Dict[str, Any]]

    try:
        tree_data = cdp.send("Accessibility.getFullAXTree")
        nodes = tree_data.get("nodes", [])
    except Exception as e:
        logger.error("CDP accessibility tree failed: %s", e)
        # Fallback to ARIA snapshot
        try:
            aria_text = page.locator("body").aria_snapshot()
            registry.set_map(target_id, {})
            return {
                "targetId": target_id,
                "url": page.url,
                "title": page.title(),
                "tree": aria_text,
                "refs": {},
                "note": "CDP failed, using ARIA snapshot (no refs available)",
            }
        except Exception as e2:
            raise RuntimeError("Both CDP and ARIA snapshot failed: %s / %s" % (e, e2))

    # Build node lookup by nodeId
    node_by_id = {}  # type: Dict[str, Dict[str, Any]]
    for node in nodes:
        nid = node.get("nodeId", "")
        node_by_id[nid] = node

    # Build tree string and ref map
    lines = []  # type: List[str]
    _walk_tree(nodes, node_by_id, lines, ref_map, registry, indent=0)

    tree_text = "\n".join(lines)
    registry.set_map(target_id, ref_map)

    return {
        "targetId": target_id,
        "url": page.url,
        "title": page.title(),
        "tree": tree_text,
        "refs": ref_map,
    }


def _walk_tree(
    nodes,  # type: List[Dict]
    node_by_id,  # type: Dict[str, Dict]
    lines,  # type: List[str]
    ref_map,  # type: Dict[str, Dict[str, Any]]
    registry,  # type: RefRegistry
    indent,  # type: int
):
    # type: (...) -> None
    """Build a flat text representation of the accessibility tree."""
    # The CDP tree is already flat with parent/child references.
    # Build a parent->children mapping and walk from root.
    children_map = {}  # type: Dict[str, List[str]]
    root_id = None

    for node in nodes:
        nid = node.get("nodeId", "")
        parent = node.get("parentId")
        if parent is None:
            root_id = nid
        else:
            if parent not in children_map:
                children_map[parent] = []
            children_map[parent].append(nid)

    if root_id is not None:
        _walk_node(root_id, node_by_id, children_map, lines, ref_map, registry, indent=0)


def _walk_node(
    node_id,  # type: str
    node_by_id,  # type: Dict[str, Dict]
    children_map,  # type: Dict[str, List[str]]
    lines,  # type: List[str]
    ref_map,  # type: Dict[str, Dict[str, Any]]
    registry,  # type: RefRegistry
    indent,  # type: int
):
    # type: (...) -> None
    node = node_by_id.get(node_id)
    if node is None:
        return

    role_obj = node.get("role", {})
    role = role_obj.get("value", "") if isinstance(role_obj, dict) else str(role_obj)
    name_obj = node.get("name", {})
    name = name_obj.get("value", "") if isinstance(name_obj, dict) else str(name_obj)
    backend_id = node.get("backendDOMNodeId")

    role_lower = role.lower()

    # Skip noise roles
    if role_lower in ("rootwebarea", "none", "generic", "statictext", "inlinetextbox",
                      "linebreak", "ignored"):
        # Still recurse into children for structural roles
        if role_lower in ("rootwebarea", "none", "generic"):
            for child_id in children_map.get(node_id, []):
                _walk_node(child_id, node_by_id, children_map, lines, ref_map, registry, indent)
        return

    # Check properties for additional info
    props = node.get("properties", [])
    prop_strs = []
    for p in props:
        pname = p.get("name", "")
        pval = p.get("value", {})
        if isinstance(pval, dict):
            pval = pval.get("value", "")
        if pname == "level" and pval:
            prop_strs.append("level=%s" % pval)
        elif pname == "checked":
            prop_strs.append("checked=%s" % pval)
        elif pname == "selected" and pval:
            prop_strs.append("selected")
        elif pname == "disabled" and pval:
            prop_strs.append("disabled")
        elif pname == "expanded":
            prop_strs.append("expanded=%s" % pval)
        elif pname == "required" and pval:
            prop_strs.append("required")

    # Build display line
    prefix = "  " * indent
    ref_str = ""

    if role_lower in INTERACTIVE_ROLES and backend_id is not None:
        ref_id = registry.next_ref()
        ref_str = " [%s]" % ref_id
        ref_map[ref_id] = {
            "role": role,
            "name": name,
            "backendDOMNodeId": backend_id,
        }

    name_display = ""
    if name:
        # Truncate very long names
        if len(name) > 120:
            name_display = ' "%s..."' % name[:117]
        else:
            name_display = ' "%s"' % name

    prop_display = ""
    if prop_strs:
        prop_display = " (%s)" % ", ".join(prop_strs)

    # Only output lines for visible roles
    if role_lower in VISIBLE_ROLES or role_lower in INTERACTIVE_ROLES:
        lines.append("%s%s%s%s%s" % (prefix, role, ref_str, name_display, prop_display))

    # Recurse children
    child_indent = indent + 1 if (role_lower in VISIBLE_ROLES or role_lower in INTERACTIVE_ROLES) else indent
    for child_id in children_map.get(node_id, []):
        _walk_node(child_id, node_by_id, children_map, lines, ref_map, registry, child_indent)

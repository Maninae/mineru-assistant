#!/usr/bin/env python3
"""
Mineru Browser Server — Element Actions

Stateless functions that operate on a (page, cdp) pair plus an explicit
backendDOMNodeId resolved from the ref registry. The dispatcher routes an
incoming action request to the appropriate handler.

Callers are responsible for holding the BrowserManager lock and providing
a live page + CDP session.
"""

import json
import random
import time
from typing import Any, Dict, Optional, Tuple

from browser.config import logger


def scrub_secret_from_text(message, secret):
    # type: (str, str) -> str
    """Remove a secret (raw AND JSON-escaped forms) from a string for safe logging.

    Playwright embeds a fill/type value verbatim in its error messages, and that
    string is later JSON-serialized in the HTTP error path, so the escaped form
    (e.g. a password containing `"` becomes `\\"`) must be scrubbed too — a plain
    raw-only replace silently misses it. No-op on an empty secret.
    """
    if not secret:
        return message
    marker = "<redacted>"  # fixed-width: do not disclose the secret's length
    message = message.replace(secret, marker)
    escaped = json.dumps(secret)[1:-1]  # JSON string-escaped form, minus the wrapping quotes
    if escaped != secret:
        message = message.replace(escaped, marker)
    return message


# ----------------------------------------------------------------------------
# Element resolution & low-level helpers
# ----------------------------------------------------------------------------


def resolve_element(page, cdp, backend_id):
    # type: (Any, Any, int) -> Any
    """Resolve a backendDOMNodeId to a Playwright ElementHandle."""
    result = cdp.send("DOM.resolveNode", {"backendNodeId": backend_id})
    object_id = result["object"]["objectId"]
    # Use Runtime.callFunctionOn to get a JSHandle, then convert
    # Actually, we can use page.evaluate_handle with the objectId
    # But simpler: use DOM.focus + keyboard, or find a CSS selector

    # Get element info to build a selector
    try:
        desc = cdp.send("DOM.describeNode", {"backendNodeId": backend_id})
        node_info = desc.get("node", {})
        attrs = node_info.get("attributes", [])
        attrs_dict = dict(zip(attrs[::2], attrs[1::2]))
        tag = node_info.get("localName", "")

        # Try various selector strategies
        if attrs_dict.get("id"):
            selector = "#%s" % attrs_dict["id"]
            try:
                el = page.locator(selector).first
                el.wait_for(timeout=2000)
                return el
            except Exception:
                pass

        if attrs_dict.get("name"):
            selector = '%s[name="%s"]' % (tag, attrs_dict["name"])
            try:
                el = page.locator(selector).first
                el.wait_for(timeout=2000)
                return el
            except Exception:
                pass

        # Use aria-label
        if attrs_dict.get("aria-label"):
            try:
                el = page.get_by_label(attrs_dict["aria-label"]).first
                el.wait_for(timeout=2000)
                return el
            except Exception:
                pass

    except Exception as e:
        logger.debug("Selector resolution failed: %s", e)

    # Fallback: use CDP focus + keyboard
    return None


def cdp_center(cdp, backend_id):
    # type: (Any, int) -> Optional[Tuple[float, float]]
    """Get the center coordinates of an element via CDP quads. Returns None if unavailable."""
    try:
        quads = cdp.send("DOM.getContentQuads", {"backendNodeId": backend_id})
        if quads.get("quads"):
            q = quads["quads"][0]
            return ((q[0] + q[2] + q[4] + q[6]) / 4, (q[1] + q[3] + q[5] + q[7]) / 4)
    except Exception:
        pass
    return None


def focus_element(page, cdp, backend_id, el):
    # type: (Any, Any, int, Any) -> None
    """Focus an element via Playwright locator or CDP fallback."""
    if el is not None:
        el.focus(timeout=5000)
    else:
        cdp.send("DOM.focus", {"backendNodeId": backend_id})


def clear_field(page):
    # type: (Any) -> None
    """Select all + delete to clear a focused field."""
    page.keyboard.press("Meta+a")
    page.keyboard.press("Backspace")


def ref_label(ref_info):
    # type: (Dict) -> str
    return ref_info.get("role", "") + " " + ref_info.get("name", "")


def humanized_move_and_click(page, x, y):
    # type: (Any, float, float) -> None
    """Move mouse along a quadratic Bézier curve, then click with dwell."""
    steps = random.randint(18, 32)
    # Randomized control point for natural curve
    cx = x * random.uniform(0.3, 0.7) + random.uniform(-60, 60)
    cy = y * random.uniform(0.3, 0.7) + random.uniform(-40, 40)
    for i in range(steps + 1):
        t = i / steps
        mx = (1 - t) ** 2 * 0 + 2 * (1 - t) * t * cx + t ** 2 * x
        my = (1 - t) ** 2 * 0 + 2 * (1 - t) * t * cy + t ** 2 * y
        page.mouse.move(mx, my)
        time.sleep(random.uniform(0.004, 0.012))
    time.sleep(random.uniform(0.05, 0.15))
    page.mouse.down()
    time.sleep(random.uniform(0.04, 0.10))
    page.mouse.up()


# ----------------------------------------------------------------------------
# Action implementations
# ----------------------------------------------------------------------------


def act_click(page, cdp, backend_id, ref_info):
    # type: (Any, Any, int, Dict) -> Dict[str, Any]
    el = resolve_element(page, cdp, backend_id)
    if el is not None:
        box = el.bounding_box(timeout=5000)
        if box:
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            humanized_move_and_click(page, x, y)
        else:
            el.click(timeout=5000)
    else:
        center = cdp_center(cdp, backend_id)
        if center:
            humanized_move_and_click(page, center[0], center[1])
        else:
            cdp.send("DOM.focus", {"backendNodeId": backend_id})
            page.keyboard.press("Enter")

    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass

    return {"action": "click", "ref": ref_label(ref_info), "url": page.url}


def act_type(page, cdp, backend_id, ref_info, text, clear, humanize=False):
    # type: (Any, Any, int, Dict, str, bool, bool) -> Dict[str, Any]
    # Always use keystroke-by-keystroke typing (never el.fill() which
    # sets values via CDP directly with no input events — a detection tell).
    el = resolve_element(page, cdp, backend_id)
    if el is not None:
        el.focus(timeout=5000)
    else:
        focus_element(page, cdp, backend_id, None)
    if clear:
        clear_field(page)

    for ch in text:
        page.keyboard.type(ch)
        if humanize:
            delay = random.gauss(0.085, 0.030)
            if random.random() < 0.04:
                delay += random.uniform(0.2, 0.5)
            time.sleep(max(0.02, delay))
        else:
            time.sleep(random.uniform(0.005, 0.02))

    if humanize:
        time.sleep(random.uniform(0.4, 1.2))

    return {"action": "type", "ref": ref_label(ref_info), "text": text, "humanize": humanize}


def act_select(page, cdp, backend_id, ref_info, value):
    # type: (Any, Any, int, Dict, str) -> Dict[str, Any]
    el = resolve_element(page, cdp, backend_id)
    if el is None:
        raise ValueError("Cannot resolve element for select — take a new snapshot")
    el.select_option(value, timeout=5000)
    return {"action": "select", "ref": ref_label(ref_info), "value": value}


def act_check(page, cdp, backend_id, ref_info, checked):
    # type: (Any, Any, int, Dict, bool) -> Dict[str, Any]
    el = resolve_element(page, cdp, backend_id)
    if el is not None:
        if checked:
            el.check(timeout=5000)
        else:
            el.uncheck(timeout=5000)
    else:
        focus_element(page, cdp, backend_id, None)
        page.keyboard.press("Space")
    return {"action": "check", "ref": ref_label(ref_info), "checked": checked}


def act_hover(page, cdp, backend_id, ref_info):
    # type: (Any, Any, int, Dict) -> Dict[str, Any]
    el = resolve_element(page, cdp, backend_id)
    if el is not None:
        el.hover(timeout=5000)
    else:
        center = cdp_center(cdp, backend_id)
        if center:
            page.mouse.move(center[0], center[1])
        else:
            raise ValueError("Cannot resolve element for hover — take a new snapshot")
    return {"action": "hover", "ref": ref_label(ref_info)}


def act_click_coords(page, x, y):
    # type: (Any, float, float) -> Dict[str, Any]
    """Click at viewport coordinates without needing a ref."""
    humanized_move_and_click(page, x, y)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass
    return {"action": "click-coords", "x": x, "y": y, "url": page.url}


def act_fill(page, cdp, backend_id, ref_info, text):
    # type: (Any, Any, int, Dict, str) -> Dict[str, Any]
    """Fill a form field instantly via Playwright's fill(). Clears existing value first."""
    el = resolve_element(page, cdp, backend_id)
    if el is None:
        raise ValueError("Cannot resolve element for fill — take a new snapshot")
    el.fill(text, timeout=5000)
    return {"action": "fill", "ref": ref_label(ref_info), "text": text}


def act_scroll_into_view(page, cdp, backend_id, ref_info):
    # type: (Any, Any, int, Dict) -> Dict[str, Any]
    """Scroll an element into the viewport."""
    el = resolve_element(page, cdp, backend_id)
    if el is not None:
        el.scroll_into_view_if_needed(timeout=5000)
    else:
        cdp.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_id})
    return {"action": "scrollIntoView", "ref": ref_label(ref_info)}


def act_scroll(page, x, y):
    # type: (Any, int, int) -> Dict[str, Any]
    """Scroll the page. x/y are pixel deltas (positive y = down)."""
    page.evaluate("([dx, dy]) => window.scrollBy(dx, dy)", [x, y])
    return {"action": "scroll", "x": x, "y": y}


# ----------------------------------------------------------------------------
# Dispatcher
# ----------------------------------------------------------------------------


def dispatch(page, get_cdp, ref_registry, target_id, request):
    # type: (Any, Any, Any, str, Dict[str, Any]) -> Dict[str, Any]
    """Route an action request to the appropriate handler.

    get_cdp is a zero-arg callable that returns the CDP session for the page,
    supplied by BrowserManager so this module doesn't need to know how CDP
    sessions are cached. It's invoked eagerly (matching the original
    behavior) for any action that resolves a ref.
    """
    kind = request.get("kind", "")

    # Actions that don't need a ref
    if kind == "scroll":
        x = int(request.get("x", 0))
        y = int(request.get("y", 500))
        return act_scroll(page, x, y)

    if kind == "click-coords":
        x = float(request.get("x", 0))
        y = float(request.get("y", 0))
        return act_click_coords(page, x, y)

    ref = request.get("ref", "")
    ref_info = ref_registry.get_map(target_id).get(ref)

    if ref_info is None:
        raise ValueError(
            "Unknown ref '%s'. Take a new snapshot to get current refs." % ref
        )

    backend_id = ref_info["backendDOMNodeId"]
    cdp = get_cdp()

    if kind == "click":
        return act_click(page, cdp, backend_id, ref_info)
    elif kind == "type":
        text = request.get("text", "")
        clear = request.get("clear", True)
        humanize = request.get("humanize", False)
        redact = bool(request.get("redact"))
        # redact=true (credential fills): the value must never reach the client
        # echo, the server logs, OR an exception message. act_type can raise with
        # `text` embedded (Playwright call log), so scrub on the raise path too.
        try:
            result = act_type(page, cdp, backend_id, ref_info, text, clear, humanize)
        except Exception as exc:
            if redact:
                raise RuntimeError(scrub_secret_from_text(str(exc), text)) from None
            raise
        if redact:
            result["text"] = "<redacted>"
            logger.info("Redacted type: ref=%s target=%s url=%s",
                        request.get("ref", ""), target_id, page.url)
        return result
    elif kind == "fill":
        text = request.get("text", "")
        redact = bool(request.get("redact"))
        try:
            result = act_fill(page, cdp, backend_id, ref_info, text)
        except Exception as exc:
            if redact:
                raise RuntimeError(scrub_secret_from_text(str(exc), text)) from None
            raise
        if redact:
            result["text"] = "<redacted>"
            logger.info("Redacted fill: ref=%s target=%s url=%s",
                        request.get("ref", ""), target_id, page.url)
        return result
    elif kind == "select":
        value = request.get("value", "")
        return act_select(page, cdp, backend_id, ref_info, value)
    elif kind == "check":
        checked = request.get("checked", True)
        return act_check(page, cdp, backend_id, ref_info, checked)
    elif kind == "hover":
        return act_hover(page, cdp, backend_id, ref_info)
    elif kind == "scrollIntoView":
        return act_scroll_into_view(page, cdp, backend_id, ref_info)
    else:
        raise ValueError("Unknown act kind: %s (valid: click, click-coords, type, fill, select, check, hover, scroll, scrollIntoView)" % kind)

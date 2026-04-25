"""Shared clear-block helper for pending UI blocks (permission, plan, question).

These three turn-modal UIs each track a "pending block" region in the view
and a dict of named button hit boxes. Their clear-block algorithms were
near-identical; this module factors that out. Render and key-handling logic
stay in the per-UI methods on OutputView since the layouts differ.

We don't introduce a shared dataclass base because Python 3.8 dataclass
inheritance with mixed default/non-default fields is unwieldy, and the
shared shape is only 3 fields.
"""
from typing import Dict, Optional


def clear_pending_block(
    view,
    block_region_key: str,
    button_prefix: str,
    button_keys: Dict[str, tuple],
    fallback_region_end: Optional[int] = None,
    replacement: str = "",
    extra_region_keys: tuple = (),
) -> Optional[tuple]:
    """Erase a pending UI block from the view.

    Returns the (begin, end) of the cleared region, or None if nothing was cleared.

    Args:
        view: sublime View
        block_region_key: e.g. "claude_permission_block" — the named region
            holding the block's text span
        button_prefix: e.g. "claude_btn_" — per-button region keys are
            f"{button_prefix}{button_type}"
        button_keys: button_regions dict whose keys to erase
        fallback_region_end: if the block region is missing/empty, fall back
            to erasing everything from this offset to view.size()
        replacement: text to insert at the cleared region (default: "" — pure
            removal). Question UI uses this for an inline summary line.
        extra_region_keys: additional named regions to erase (e.g. question
            UI also has "claude_question_keys").
    """
    # Erase per-button hit-box regions
    for btn_type in button_keys:
        view.erase_regions(f"{button_prefix}{btn_type}")

    cleared: Optional[tuple] = None
    regions = view.get_regions(block_region_key)
    if regions and regions[0].size() > 0:
        r = regions[0]
        cleared = (r.begin(), r.end())
        view.set_read_only(False)
        view.run_command("claude_replace", {"start": r.begin(), "end": r.end(), "text": replacement})
        view.set_read_only(True)
    elif fallback_region_end is not None and view.size() > fallback_region_end:
        # Fallback: tracked region was lost; remove everything after the current
        # conversation's last known end position
        cleared = (fallback_region_end, view.size())
        view.set_read_only(False)
        view.run_command("claude_replace", {"start": fallback_region_end, "end": view.size(), "text": replacement})
        view.set_read_only(True)

    view.erase_regions(block_region_key)
    for key in extra_region_keys:
        view.erase_regions(key)
    return cleared

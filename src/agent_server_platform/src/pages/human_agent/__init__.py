# Human Agent — minimal identity-persistence bootstrap for the human_tasks page.
#
# The timer-based task loading is handled server-side by Gradio. This module
# only injects a tiny script to persist the identity input across page reloads.
import os
import json
from pathlib import Path

_STATIC_DIR = Path(__file__).parent / "static"


def mount_static(demo) -> None:
    """No-op — SharedWorker static route no longer needed.

    Kept for backward compatibility with app.py lifecycle.
    """
    pass


def inject_head(identity_selector: str = "#ha-identity") -> str:
    """Return minimal <head> content: identity persistence across page reloads."""
    sel = identity_selector.replace("'", "\\'")
    return (
        "<script>"
        "(function(){"
        "  var sel='" + sel + "';"
        "  var key='human_agent_id';"
        "  function load(){var el=document.querySelector(sel);"
        "    if(el){var v=localStorage.getItem(key);if(v)el.value=v;"
        "    el.addEventListener('change',function(){localStorage.setItem(key,el.value.trim());});}}"
        "  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',load);"
        "  else load();"
        "})();"
        "</script>"
    )
"""Rule-enforcement helpers shared by the CLI, the daemon hooks, and the server.

The goal: keep a project's mandatory/forbidden rules continuously visible to
Claude so they survive context compaction and never get silently dropped.
"""

from memory_mcp.container import container


def format_rules_block(slug: str, mandatory: list, forbidden: list) -> str:
    """Render rules as an injectable text block. Empty string when there are none."""
    if not mandatory and not forbidden:
        return ""
    lines = [
        f"[Memory MCP] Binding rules for project '{slug}' — follow every one of these:",
    ]
    if mandatory:
        lines.append("MANDATORY (must always do):")
        for m in mandatory:
            lines.append(f"  - {m.title}: {m.content}")
    if forbidden:
        lines.append("FORBIDDEN (must never do):")
        for m in forbidden:
            lines.append(f"  - {m.title}: {m.content}")
    lines.append(
        "If anything you are about to do conflicts with a rule above, stop and "
        "tell the user instead of proceeding."
    )
    return "\n".join(lines)


def format_intro(slug: str) -> str:
    """Session-start nudge text for a detected memory project."""
    return (
        f"[Memory MCP] This directory is memory project '{slug}'. "
        f"Call memory_session_start('{slug}') now, before doing any work, to load "
        f"its rules, last session summary, sprint goals, and recent decisions."
    )


def format_session_end(slug: str) -> str:
    """Stop-hook reminder to persist the session for a memory project."""
    return (
        f"[Memory MCP] Before finishing work on project '{slug}': call "
        f"memory_session_end(session_id, summary) with a summary of decisions "
        f"made and context for the next session, and store any new rules or "
        f"decisions with memory_store."
    )


def rules_text_for_project(slug: str) -> str:
    """Fetch and format the rules block for a project (empty string if none)."""
    rules = container.rules_service.get_rules(slug)
    return format_rules_block(slug, rules.mandatory_rules, rules.forbidden_rules)


def rules_digest(slug: str) -> dict | None:
    """Compact rules summary embedded in tool responses to keep rules in view.

    Returns None when the project has no rules so responses stay clean.
    """
    try:
        rules = container.rules_service.get_rules(slug)
    except Exception:  # noqa: BLE001
        return None
    if not rules.mandatory_rules and not rules.forbidden_rules:
        return None
    return {
        "_reminder": "Active project rules — keep following these for the whole session.",
        "mandatory": [m.title for m in rules.mandatory_rules],
        "forbidden": [m.title for m in rules.forbidden_rules],
    }

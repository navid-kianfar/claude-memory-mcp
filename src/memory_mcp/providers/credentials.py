"""Credentials, one store, keyed by platform rather than by one platform's URL.

The PAT store was written when asoode was the only platform, so it keys on the
API base URL: `app_settings['cred:https://api.asoode.com']`. That works for
asoode and is wrong as a general rule - Trello authenticates with a key AND a
token and has one fixed URL, Jira needs a per-site URL plus an email, and Monday
has neither. Keying on "the server URL" assumes every platform is shaped like
asoode.

So: credentials are keyed by (provider, account), where `account` is whatever
that platform uses to tell one credential from another - an API base URL for
asoode and Jira, the empty string for a platform with a single global endpoint.

BACKWARD COMPATIBILITY IS NOT OPTIONAL HERE. The asoode PAT already sits under
the old key and the user should never be asked for it twice, so asoode reads
through `memory_mcp.asoode.get_pat`, which owns that key. A lookup that finds
nothing under the new scheme falls back to the legacy one before giving up.
"""

from memory_mcp.db.registry import get_setting, set_setting


def _key(provider: str, account: str = "") -> str:
    scope = (account or "").rstrip("/")
    return f"cred:{provider}:{scope}" if scope else f"cred:{provider}"


def get_credential(provider: str, account: str = "") -> str | None:
    """The stored secret for a platform, or None.

    asoode is special-cased to its existing URL-keyed entry: that token is
    already on the machine and re-prompting for it would be a regression the
    user would feel immediately.
    """
    stored = get_setting(_key(provider, account))
    if stored:
        return stored
    if provider == "asoode":
        from memory_mcp.asoode import get_pat

        return get_pat(account or None)
    # Legacy URL-only key, from before credentials were provider-scoped.
    if account:
        return get_setting(f"cred:{account.rstrip('/')}") or None
    return None


def set_credential(provider: str, token: str, account: str = "") -> None:
    set_setting(_key(provider, account), token)


def clear_credential(provider: str, account: str = "") -> None:
    set_setting(_key(provider, account), "")


def fingerprint(token: str | None) -> dict | None:
    """Prefix + last4, safe to show. The same shape the asoode status uses, so a
    credential is recognisable across platforms without any of them printing it."""
    tok = (token or "").strip()
    if not tok:
        return None
    return {
        "prefix": tok[:6],
        "last4": tok[-4:] if len(tok) >= 8 else "",
        "length": len(tok),
    }

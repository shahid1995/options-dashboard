"""Platform session token detection — shared across all broker-calling routes.

Platform sessions (email/password, Google) store the session identifier
as the "access_token" in the in-memory token store.  This identifier
starts with a known prefix and must NEVER be passed to a broker adapter
as a real access token.

This module provides a single authoritative check used by:

- chains.py: require_market_data_token(), call_upstox(), chain_ws()
- live_gex.py: _fetch_chain()
- gex.py: trigger_capture()
- broker_profile.py: get_broker_profile_summary() (Phase P fix)
"""

# Prefixes used by auth.py for platform-only session tokens.
# Broker access tokens (from Upstox OAuth) are opaque strings that
# never start with these prefixes.
PLATFORM_TOKEN_PREFIXES = ("email:", "google:")


def is_platform_session_token(token: str | None) -> bool:
    """Return True if *token* is a platform session identifier, not a broker token.

    Platform session tokens are created by:
    - POST /auth/login-email  →  "email:<user_id>:<random>"
    - POST /auth/google        →  "google:<user_id>:<random>"

    Broker tokens from Upstox OAuth are base64-encoded strings that
    never start with ``email:`` or ``google:``.
    """
    if not isinstance(token, str):
        return False
    return any(token.startswith(prefix) for prefix in PLATFORM_TOKEN_PREFIXES)

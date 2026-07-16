"""Codex request identity helpers shared by agent client builders.

This leaf module intentionally has no dependency on the large auxiliary-client
router. Long-lived processes can therefore import a newly added client builder
without resolving a new symbol from an older cached ``auxiliary_client`` module.
"""

from __future__ import annotations

from typing import Any, Dict
from urllib.parse import urlparse


CODEX_AUX_BASE_URL = "https://chatgpt.com/backend-api/codex"


def is_official_codex_base_url(base_url: str) -> bool:
    """Identify OpenAI's Codex endpoint without matching custom proxies."""
    try:
        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/")
        return (
            parsed.scheme == "https"
            and parsed.hostname == "chatgpt.com"
            and parsed.port in (None, 443)
            and (path == "/backend-api/codex" or path.startswith("/backend-api/codex/"))
        )
    except (TypeError, ValueError):
        return False


def codex_cloudflare_headers(
    access_token: str, *, base_url: str = CODEX_AUX_BASE_URL,
) -> Dict[str, str]:
    """Identity and account headers for chatgpt.com/backend-api/codex.

    OpenAI requires third-party harnesses to identify themselves. Requests to
    the official endpoint always send Hermes' originator and version. Custom
    endpoints retain the existing compatibility identity. In either case,
    preserve ``ChatGPT-Account-ID`` from the OAuth JWT's
    ``chatgpt_account_id`` claim.

    Malformed tokens are tolerated — we drop the account-ID header rather than
    raise, so a bad token still surfaces as an auth error (401) instead of a
    crash at client construction.
    """
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent)",
        "originator": "codex_cli_rs",
    }
    if is_official_codex_base_url(base_url):
        from hermes_cli import __version__

        headers.update({
            "User-Agent": f"HermesAgent/{__version__}",
            "originator": "hermes-agent",
        })
    from agent.account_usage import codex_account_id_from_token

    account_id = codex_account_id_from_token(access_token)
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def apply_required_codex_headers(
    client_kwargs: Dict[str, Any], *, access_token: str, base_url: str,
) -> None:
    """Keep required Codex identity after user/provider header overrides."""
    if not is_official_codex_base_url(base_url):
        return
    required = codex_cloudflare_headers(access_token, base_url=base_url)
    required_names = {name.lower() for name in required}
    existing = client_kwargs.get("default_headers") or {}
    client_kwargs["default_headers"] = {
        **{
            name: value
            for name, value in existing.items()
            if str(name).lower() not in required_names
        },
        **required,
    }

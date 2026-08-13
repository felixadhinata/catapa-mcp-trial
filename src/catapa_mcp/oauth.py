"""Interactive OAuth2 authorization-code login for CATAPA, shared by the public and private clients.

CATAPA's private API (`catapa-private`) has no OAuth of its own -- its only auth is a direct
username/password login. But its client also accepts a static bearer `access_token`, and CATAPA's
*public* API already has a real, browser-redirect-based OAuth2 authorization-code flow (see
`catapa.auth.grant_type.authorization_code`). This module drives that flow through a local
loopback HTTP server, caches the resulting access/refresh token pair to disk, and hands back a
single bearer token that authenticates both `catapa.Catapa` and `catapa_private.CatapaPrivate`.

Reference: GDP-ADMIN/gl-connectors-sdk's `catapa/plugin.py`, which implements the same
authorization-code exchange (`POST {base_url}/oauth/token`) server-side.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socketserver
import stat
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import requests

from catapa_mcp.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_CACHE_PATH = Path.home() / ".catapa-mcp" / "oauth-token.json"
LOGIN_TIMEOUT_SECONDS = 300
TOKEN_EXPIRATION_BUFFER_SECONDS = 60
DEFAULT_EXPIRES_IN_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 30


class OAuthLoginError(Exception):
    """Raised when the interactive OAuth login flow fails."""


@dataclass
class OAuthTokenSet:
    """A cached CATAPA OAuth2 token pair."""

    access_token: str
    refresh_token: str | None
    tenant: str
    expires_at: float

    def is_valid(self) -> bool:
        """Check whether the access token is still usable, with a safety buffer before expiry.

        Returns:
            bool: True if the token has not expired (within the buffer).
        """
        return time.time() < (self.expires_at - TOKEN_EXPIRATION_BUFFER_SECONDS)


def _token_cache_path() -> Path:
    """Resolve the on-disk path used to cache the OAuth token between server launches.

    Returns:
        Path: `CATAPA_MCP_TOKEN_CACHE` if set, otherwise `~/.catapa-mcp/oauth-token.json`.
    """
    override = os.environ.get("CATAPA_MCP_TOKEN_CACHE")
    return Path(override).expanduser() if override else DEFAULT_TOKEN_CACHE_PATH


def _load_cached_token() -> OAuthTokenSet | None:
    """Load the cached token from disk, if present and readable.

    Returns:
        OAuthTokenSet | None: The cached token, or None if there isn't a usable one.
    """
    path = _token_cache_path()
    if not path.is_file():
        return None
    try:
        return OAuthTokenSet(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        logger.warning("Ignoring unreadable OAuth token cache at %s", path, exc_info=True)
        return None


def _save_cached_token(token_set: OAuthTokenSet) -> None:
    """Persist the token to disk, restricted to the owner.

    Args:
        token_set: The token to cache.
    """
    path = _token_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(token_set)))
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _token_set_from_response(token_response: dict[str, Any], tenant: str) -> OAuthTokenSet:
    """Build an OAuthTokenSet from a CATAPA `/oauth/token` JSON response.

    Args:
        token_response: The parsed JSON body of the token response.
        tenant: The CATAPA tenant this token belongs to (not included in the token response itself).

    Returns:
        OAuthTokenSet: The parsed token pair.
    """
    return OAuthTokenSet(
        access_token=token_response["access_token"],
        refresh_token=token_response.get("refresh_token"),
        tenant=tenant,
        expires_at=time.time() + token_response.get("expires_in", DEFAULT_EXPIRES_IN_SECONDS),
    )


def _exchange_code_for_token(settings: Settings, code: str, redirect_uri: str, tenant: str) -> OAuthTokenSet:
    """Exchange an authorization code for an access/refresh token pair.

    Args:
        settings: The server configuration (client_id/client_secret/base_url).
        code: The single-use authorization code from the redirect callback.
        redirect_uri: The redirect URI used in the authorization request (must match exactly).
        tenant: The CATAPA tenant returned in the redirect callback.

    Returns:
        OAuthTokenSet: The obtained token pair.
    """
    response = requests.post(
        f"{settings.public_base_url}/oauth/token",
        headers={"Tenant": tenant, "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        auth=(settings.public_client_id, settings.public_client_secret),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return _token_set_from_response(response.json(), tenant)


def _refresh_token(settings: Settings, refresh_token: str, tenant: str) -> OAuthTokenSet:
    """Refresh an access token using a previously issued refresh token.

    Args:
        settings: The server configuration (client_id/client_secret/base_url).
        refresh_token: The refresh token from a prior login.
        tenant: The CATAPA tenant this token belongs to.

    Returns:
        OAuthTokenSet: The refreshed token pair.
    """
    response = requests.post(
        f"{settings.public_base_url}/oauth/token",
        headers={"Tenant": tenant, "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(settings.public_client_id, settings.public_client_secret),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return _token_set_from_response(response.json(), tenant)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the single OAuth redirect callback, then lets the loopback server shut down."""

    def do_GET(self) -> None:  # noqa: N802 -- required name from BaseHTTPRequestHandler
        """Parse the callback's query params and render a short confirmation page."""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        callback_params = {key: values[0] for key, values in params.items()}
        self.server.callback_params = callback_params  # type: ignore[attr-defined]

        body = "<p>Login complete. You may close this window and return to your MCP client.</p>"
        if "error" in callback_params:
            body = f"<p>CATAPA login failed: {callback_params['error']}</p>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, log_format: str, *args: Any) -> None:
        """Suppress default request logging; stdio owns stdout and this would clutter stderr."""


def run_interactive_login(settings: Settings) -> OAuthTokenSet:
    """Run the CATAPA OAuth2 authorization-code flow via a local loopback browser redirect.

    Opens the user's default browser to CATAPA's hosted login page, waits for the redirect back to
    a temporary localhost server, and exchanges the resulting code for an access/refresh token pair.

    Args:
        settings: The server configuration (needs `public_client_id`/`public_client_secret`).

    Returns:
        OAuthTokenSet: The newly obtained token pair.

    Raises:
        OAuthLoginError: If the login times out, is denied, fails state verification, or the
            callback is missing the authorization code or tenant.
    """
    with socketserver.TCPServer(("127.0.0.1", 0), _CallbackHandler) as httpd:
        httpd.callback_params = {}  # type: ignore[attr-defined]
        port = httpd.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        state = secrets.token_urlsafe(24)

        params = {
            "response_type": "code",
            "client_id": settings.public_client_id,
            "redirect_uri": redirect_uri,
            "scope": "all",
            "state": state,
        }
        authorization_url = f"{settings.authorization_url}?{urllib.parse.urlencode(params)}"

        print(f"CATAPA login required. Opening your browser to:\n{authorization_url}", file=sys.stderr)
        webbrowser.open(authorization_url)

        httpd.timeout = LOGIN_TIMEOUT_SECONDS
        httpd.handle_request()

        callback_params: dict[str, str] = httpd.callback_params  # type: ignore[attr-defined]

    if not callback_params:
        raise OAuthLoginError(f"Timed out waiting for CATAPA login (waited {LOGIN_TIMEOUT_SECONDS}s)")
    if "error" in callback_params:
        raise OAuthLoginError(f"CATAPA login failed: {callback_params['error']}")
    if callback_params.get("state") != state:
        raise OAuthLoginError("OAuth state mismatch on CATAPA login callback; aborting")

    code = callback_params.get("code")
    tenant = callback_params.get("tenant") or settings.public_tenant
    if not code or not tenant:
        raise OAuthLoginError("CATAPA login callback was missing 'code' or 'tenant'")

    token_set = _exchange_code_for_token(settings, code, redirect_uri, tenant)
    _save_cached_token(token_set)
    return token_set


def get_access_token(settings: Settings) -> tuple[str, str]:
    """Get a valid CATAPA OAuth access token, refreshing or logging in interactively as needed.

    Args:
        settings: The server configuration.

    Returns:
        tuple[str, str]: (access_token, tenant).
    """
    token_set = _load_cached_token()

    if token_set and token_set.is_valid():
        return token_set.access_token, token_set.tenant

    if token_set and token_set.refresh_token:
        try:
            token_set = _refresh_token(settings, token_set.refresh_token, token_set.tenant)
            _save_cached_token(token_set)
            return token_set.access_token, token_set.tenant
        except requests.exceptions.RequestException:
            logger.warning("Failed to refresh cached CATAPA OAuth token; re-authenticating", exc_info=True)

    token_set = run_interactive_login(settings)
    return token_set.access_token, token_set.tenant

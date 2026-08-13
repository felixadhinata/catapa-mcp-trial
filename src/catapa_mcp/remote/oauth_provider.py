"""OAuth broker bridging MCP clients (Claude) to CATAPA's own OAuth2 login.

Implements `mcp.server.auth.provider.OAuthAuthorizationServerProvider` using the "proxy to a
third-party OAuth server" pattern the SDK's own docstring describes: a connecting MCP client
authorizes against *this* server, which redirects the end user to CATAPA's real login page, then
mints its own MCP tokens once CATAPA's redirect back completes. CATAPA has no OAuth for its
private API, but its client accepts a static bearer token -- see `remote/private_tools.py` for
how the sealed CATAPA access token ends up authenticating catapa-private calls.

MCP tokens are self-contained (see `crypto.py`): no user-token table, only the small,
low-churn client-registration and in-flight-login-state records in `TokenStore`.
"""

from __future__ import annotations

import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from catapa_mcp.remote import crypto
from catapa_mcp.remote.store import AUTH_CODE_TTL_SECONDS, TokenStore

logger = logging.getLogger(__name__)

DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 3600
# CATAPA's own refresh token governs the real lifetime; this only caps how long we'll keep
# *offering* it before forcing a fresh CATAPA login regardless.
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
REQUEST_TIMEOUT_SECONDS = 30
SCOPE = "private"


class CatapaAuthorizationCode(AuthorizationCode):
    """An MCP authorization code, plus the CATAPA credentials it was minted for."""

    catapa_access_token: str
    catapa_refresh_token: str | None
    catapa_tenant: str
    catapa_expires_in: int


class CatapaRefreshToken(RefreshToken):
    """An MCP refresh token, plus the CATAPA refresh token it wraps."""

    catapa_refresh_token: str
    catapa_tenant: str


@dataclass
class CatapaOAuthProvider:
    """Bridges MCP's OAuth flow to CATAPA's real OAuth2 authorization-code login.

    Structurally implements `mcp.server.auth.provider.OAuthAuthorizationServerProvider` (a
    `Protocol`, so no inheritance is required).
    """

    store: TokenStore
    client_id: str
    client_secret: str
    base_url: str
    authorization_url: str
    callback_url: str

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Look up a previously registered MCP client.

        Args:
            client_id: The client's ID.

        Returns:
            OAuthClientInformationFull | None: The client's registration, or None if unknown.
        """
        data = await self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Persist a newly (dynamically) registered MCP client.

        Args:
            client_info: The client's registration metadata.
        """
        await self.store.save_client(client_info.client_id, client_info.model_dump(mode="json"))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Start the login: remember the MCP client's request, then hand back CATAPA's login URL.

        Args:
            client: The MCP client requesting authorization.
            params: The authorization request's parameters (redirect_uri, PKCE challenge, ...).

        Returns:
            str: CATAPA's own `/oauth2/authorize` URL to redirect the user's browser to.
        """
        flow_id = secrets.token_urlsafe(24)
        await self.store.save_flow(
            flow_id,
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "state": params.state,
                "code_challenge": params.code_challenge,
                "scopes": params.scopes or [SCOPE],
                "resource": params.resource,
            },
        )

        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.callback_url,
                "scope": "all",
                "state": flow_id,
            }
        )
        return f"{self.authorization_url}?{query}"

    async def handle_catapa_callback(self, request: Request) -> Response:
        """Complete the CATAPA leg of the login and redirect back to the connecting MCP client.

        Args:
            request: The incoming GET to `/catapa/callback`.

        Returns:
            Response: A redirect back to the connecting MCP client's own redirect_uri, carrying
                a freshly minted MCP authorization code (or an error, if the login failed).
        """
        flow_id = request.query_params.get("state")
        flow = await self.store.load_flow(flow_id) if flow_id else None
        if not flow:
            return Response("Login session expired or invalid; please try connecting again.", status_code=400)

        error = request.query_params.get("error")
        if error:
            return self._redirect_with_error(flow, error, request.query_params.get("error_description"))

        code = request.query_params.get("code")
        tenant = request.query_params.get("tenant")
        if not code or not tenant:
            return self._redirect_with_error(flow, "server_error", "CATAPA callback was missing code or tenant")

        try:
            token_response = await self._exchange_catapa_code(code, tenant)
        except httpx.HTTPStatusError:
            logger.warning("CATAPA token exchange failed", exc_info=True)
            return self._redirect_with_error(flow, "server_error", "Failed to exchange the CATAPA login code")

        subject = await self._fetch_subject(token_response["access_token"], tenant) or tenant
        expires_in = token_response.get("expires_in", DEFAULT_ACCESS_TOKEN_TTL_SECONDS)

        mcp_code = secrets.token_urlsafe(32)
        await self.store.save_auth_code(
            mcp_code,
            {
                "client_id": flow["client_id"],
                "code_challenge": flow["code_challenge"],
                "redirect_uri": flow["redirect_uri"],
                "scopes": flow["scopes"],
                "resource": flow.get("resource"),
                "subject": subject,
                "catapa_access_token": token_response["access_token"],
                "catapa_refresh_token": token_response.get("refresh_token"),
                "catapa_tenant": tenant,
                "catapa_expires_in": expires_in,
                "expires_at": time.time() + AUTH_CODE_TTL_SECONDS,
            },
        )

        redirect_query = {"code": mcp_code}
        if flow.get("state") is not None:
            redirect_query["state"] = flow["state"]
        return RedirectResponse(f"{flow['redirect_uri']}?{urllib.parse.urlencode(redirect_query)}", status_code=302)

    def _redirect_with_error(self, flow: dict[str, Any], error: str, description: str | None) -> Response:
        """Build a redirect back to the MCP client's redirect_uri carrying an OAuth error.

        Args:
            flow: The in-flight login's stored state.
            error: The OAuth error code.
            description: An optional human-readable error description.

        Returns:
            Response: The redirect response.
        """
        query = {"error": error}
        if description:
            query["error_description"] = description
        if flow.get("state") is not None:
            query["state"] = flow["state"]
        return RedirectResponse(f"{flow['redirect_uri']}?{urllib.parse.urlencode(query)}", status_code=302)

    async def _exchange_catapa_code(self, code: str, tenant: str) -> dict[str, Any]:
        """Exchange a CATAPA authorization code for a CATAPA access/refresh token pair.

        Args:
            code: The single-use authorization code from CATAPA's redirect callback.
            tenant: The CATAPA tenant returned in the redirect callback.

        Returns:
            dict[str, Any]: The parsed `/oauth/token` JSON response.
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as http_client:
            response = await http_client.post(
                f"{self.base_url}/oauth/token",
                headers={"Tenant": tenant, "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": self.callback_url},
                auth=(self.client_id, self.client_secret),
            )
            response.raise_for_status()
            return response.json()

    async def _refresh_catapa_token(self, refresh_token: str, tenant: str) -> dict[str, Any]:
        """Refresh a CATAPA access token using a previously issued CATAPA refresh token.

        Args:
            refresh_token: The CATAPA refresh token.
            tenant: The CATAPA tenant this token belongs to.

        Returns:
            dict[str, Any]: The parsed `/oauth/token` JSON response.
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as http_client:
            response = await http_client.post(
                f"{self.base_url}/oauth/token",
                headers={"Tenant": tenant, "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(self.client_id, self.client_secret),
            )
            response.raise_for_status()
            return response.json()

    async def _fetch_subject(self, access_token: str, tenant: str) -> str | None:
        """Best-effort lookup of the logged-in CATAPA user's identity, for auditability.

        Args:
            access_token: The CATAPA access token just obtained.
            tenant: The CATAPA tenant.

        Returns:
            str | None: `{email}_{tenant}`, or None if the lookup fails (non-fatal).
        """
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as http_client:
                response = await http_client.get(
                    f"{self.base_url}/v1/users/me",
                    headers={"Authorization": f"Bearer {access_token}", "Tenant": tenant},
                )
                response.raise_for_status()
                email = response.json().get("email")
                return f"{email}_{tenant}" if email else None
        except httpx.HTTPError:
            logger.warning("Failed to fetch CATAPA user identity; falling back to tenant", exc_info=True)
            return None

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> CatapaAuthorizationCode | None:
        """Load a previously minted MCP authorization code's data.

        Args:
            client: The client presenting the code.
            authorization_code: The authorization code string.

        Returns:
            CatapaAuthorizationCode | None: The code's data, or None if unknown/expired/mismatched.
        """
        data = await self.store.load_auth_code(authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        return CatapaAuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=data["expires_at"],
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=True,
            resource=data.get("resource"),
            subject=data.get("subject"),
            catapa_access_token=data["catapa_access_token"],
            catapa_refresh_token=data.get("catapa_refresh_token"),
            catapa_tenant=data["catapa_tenant"],
            catapa_expires_in=data["catapa_expires_in"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: CatapaAuthorizationCode
    ) -> OAuthToken:
        """Exchange an MCP authorization code for a sealed MCP access/refresh token pair.

        Args:
            client: The client exchanging the code.
            authorization_code: The code's data, as returned by `load_authorization_code`.

        Returns:
            OAuthToken: The newly minted MCP tokens.
        """
        await self.store.delete_auth_code(authorization_code.code)

        access_expires_at = time.time() + authorization_code.catapa_expires_in
        access_token = crypto.seal(
            {
                "typ": "access",
                "jti": secrets.token_urlsafe(16),
                "client_id": authorization_code.client_id,
                "scopes": authorization_code.scopes,
                "subject": authorization_code.subject,
                "catapa_access_token": authorization_code.catapa_access_token,
                "catapa_tenant": authorization_code.catapa_tenant,
                "expires_at": access_expires_at,
            }
        )

        refresh_token = None
        if authorization_code.catapa_refresh_token:
            refresh_token = crypto.seal(
                {
                    "typ": "refresh",
                    "jti": secrets.token_urlsafe(16),
                    "client_id": authorization_code.client_id,
                    "scopes": authorization_code.scopes,
                    "subject": authorization_code.subject,
                    "catapa_refresh_token": authorization_code.catapa_refresh_token,
                    "catapa_tenant": authorization_code.catapa_tenant,
                    "expires_at": time.time() + REFRESH_TOKEN_TTL_SECONDS,
                }
            )

        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=authorization_code.catapa_expires_in,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> CatapaRefreshToken | None:
        """Decode and validate a sealed MCP refresh token.

        Args:
            client: The client presenting the refresh token.
            refresh_token: The sealed refresh token string.

        Returns:
            CatapaRefreshToken | None: The decoded token, or None if invalid/revoked/mismatched.
        """
        try:
            payload = crypto.unseal(refresh_token)
        except crypto.TokenCryptoError:
            return None
        if payload.get("typ") != "refresh" or payload.get("client_id") != client.client_id:
            return None
        if await self.store.is_jti_revoked(payload["jti"]):
            return None
        return CatapaRefreshToken(
            token=refresh_token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=int(payload["expires_at"]),
            subject=payload.get("subject"),
            catapa_refresh_token=payload["catapa_refresh_token"],
            catapa_tenant=payload["catapa_tenant"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: CatapaRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Refresh a CATAPA access token and mint a new, rotated MCP token pair.

        Args:
            client: The client exchanging the refresh token.
            refresh_token: The decoded refresh token, as returned by `load_refresh_token`.
            scopes: Requested scopes (falls back to the refresh token's own scopes if empty).

        Returns:
            OAuthToken: The newly minted MCP tokens.

        Raises:
            TokenError: If CATAPA rejects the refresh token.
        """
        try:
            token_response = await self._refresh_catapa_token(
                refresh_token.catapa_refresh_token, refresh_token.catapa_tenant
            )
        except httpx.HTTPStatusError as e:
            raise TokenError(error="invalid_grant", error_description="CATAPA rejected the refresh token") from e

        old_payload = crypto.unseal(refresh_token.token)
        await self.store.revoke_jti(old_payload["jti"], old_payload["expires_at"])

        granted_scopes = scopes or refresh_token.scopes
        access_expires_in = token_response.get("expires_in", DEFAULT_ACCESS_TOKEN_TTL_SECONDS)
        access_token = crypto.seal(
            {
                "typ": "access",
                "jti": secrets.token_urlsafe(16),
                "client_id": client.client_id,
                "scopes": granted_scopes,
                "subject": refresh_token.subject,
                "catapa_access_token": token_response["access_token"],
                "catapa_tenant": refresh_token.catapa_tenant,
                "expires_at": time.time() + access_expires_in,
            }
        )

        new_catapa_refresh = token_response.get("refresh_token", refresh_token.catapa_refresh_token)
        new_refresh_token = None
        if new_catapa_refresh:
            new_refresh_token = crypto.seal(
                {
                    "typ": "refresh",
                    "jti": secrets.token_urlsafe(16),
                    "client_id": client.client_id,
                    "scopes": granted_scopes,
                    "subject": refresh_token.subject,
                    "catapa_refresh_token": new_catapa_refresh,
                    "catapa_tenant": refresh_token.catapa_tenant,
                    "expires_at": time.time() + REFRESH_TOKEN_TTL_SECONDS,
                }
            )

        return OAuthToken(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=access_expires_in,
            scope=" ".join(granted_scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Decode and validate a sealed MCP access token.

        The CATAPA credentials embedded in a valid token are exposed via `AccessToken.claims`,
        which `remote/private_tools.py` reads to build a per-request `CatapaPrivate` client.

        Args:
            token: The sealed access token string.

        Returns:
            AccessToken | None: The decoded token, or None if invalid/expired/revoked.
        """
        try:
            payload = crypto.unseal(token)
        except crypto.TokenCryptoError:
            return None
        if payload.get("typ") != "access" or payload["expires_at"] < time.time():
            return None
        if await self.store.is_jti_revoked(payload["jti"]):
            return None
        return AccessToken(
            token=token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=int(payload["expires_at"]),
            subject=payload.get("subject"),
            claims={
                "catapa_access_token": payload["catapa_access_token"],
                "catapa_tenant": payload["catapa_tenant"],
            },
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke a sealed MCP access or refresh token ahead of its natural expiry.

        Args:
            token: The token to revoke.
        """
        try:
            payload = crypto.unseal(token.token)
        except crypto.TokenCryptoError:
            return
        await self.store.revoke_jti(payload["jti"], payload.get("expires_at", time.time()))

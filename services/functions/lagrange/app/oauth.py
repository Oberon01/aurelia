"""Signing in to a service (OAuth 2.1 with PKCE), the way MCP servers ask for it.

1. The server answers 401 and says where its metadata is (or I look in the usual places).
2. That names its authorization server; its metadata gives the sign-in, token and registration addresses.
3. I register Aurelia as a client (dynamic client registration), unless you gave a client id.
4. You open the sign-in link on your display and approve; the service sends you back to
   /lagrange/oauth/callback with a one-time code, which only the state I made a few minutes earlier accepts.
5. The tokens go in the vault and refresh themselves. No client secret is needed (PKCE), and none of it
   ever passes through the orchestrator.
"""
import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx


class OAuthError(Exception):
    pass


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _origin(url: str) -> tuple[str, str]:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}", u.path.rstrip("/")


def _safe(url: str) -> bool:
    """https, or http only to this machine or your own network."""
    import ipaddress
    u = urlsplit(url)
    if u.scheme == "https" and u.hostname:
        return True
    if u.scheme != "http" or not u.hostname:
        return False
    if u.hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(u.hostname)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return "." not in u.hostname                    # a name on your own network (a container's name)


async def _json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        r = await client.get(url, headers={"Accept": "application/json"}, timeout=10)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


async def discover(client: httpx.AsyncClient, resource_url: str, metadata_url: str | None = None) -> dict[str, Any]:
    """Where to sign in for this server: its authorization server's endpoints, and the resource to ask for."""
    origin, path = _origin(resource_url)
    prm = None
    for u in ([metadata_url] if metadata_url else []) + [f"{origin}/.well-known/oauth-protected-resource{path}",
                                                         f"{origin}/.well-known/oauth-protected-resource"]:
        prm = await _json(client, u)
        if prm:
            break
    issuer = ((prm or {}).get("authorization_servers") or [origin])[0]
    resource = (prm or {}).get("resource") or resource_url
    io, ip = _origin(issuer)
    tries = ([f"{io}/.well-known/oauth-authorization-server{ip}", f"{io}/.well-known/openid-configuration{ip}",
              f"{io}{ip}/.well-known/openid-configuration"] if ip else
             [f"{io}/.well-known/oauth-authorization-server", f"{io}/.well-known/openid-configuration"])
    meta = None
    for u in tries:
        meta = await _json(client, u)
        if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            break
        meta = None
    if not meta:
        raise OAuthError("The server wants a sign-in, but I couldn't find where to sign in.")
    for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if meta.get(k) and not _safe(str(meta[k])):
            raise OAuthError("The service's sign-in isn't on a secure address (https), so I won't sign in to it.")
    if "S256" not in (meta.get("code_challenge_methods_supported") or ["S256"]):
        raise OAuthError("The service doesn't support a secure sign-in (PKCE), so I won't sign in to it.")
    scopes = (prm or {}).get("scopes_supported") or []
    return {"issuer": issuer, "authorization_endpoint": meta["authorization_endpoint"],
            "token_endpoint": meta["token_endpoint"], "registration_endpoint": meta.get("registration_endpoint"),
            "resource": resource, "scope": " ".join(scopes) if scopes else None}


async def register(client: httpx.AsyncClient, meta: dict, redirect_uri: str) -> dict:
    if not meta.get("registration_endpoint"):
        raise OAuthError("The service doesn't let me register myself. Add a client id for Aurelia from the "
                         "service's settings to the connection, then sign in again.")
    try:
        r = await client.post(meta["registration_endpoint"], json={
            "client_name": "Aurelia", "redirect_uris": [redirect_uri], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}, timeout=15)
    except httpx.HTTPError as exc:
        raise OAuthError(f"I couldn't register with the service: {exc.__class__.__name__}") from None
    if r.status_code not in (200, 201):
        raise OAuthError(f"The service refused to register me ({r.status_code}).")
    d = r.json()
    if not d.get("client_id"):
        raise OAuthError("The service registered me without a client id.")
    return {"client_id": d["client_id"], "client_secret": d.get("client_secret")}


def sign_in_url(meta: dict, client_id: str, redirect_uri: str, state: str, challenge: str, scope: str | None) -> str:
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256", "resource": meta["resource"]}
    if scope:
        q["scope"] = scope
    sep = "&" if "?" in meta["authorization_endpoint"] else "?"
    return meta["authorization_endpoint"] + sep + urlencode(q)


async def _token(client: httpx.AsyncClient, endpoint: str, form: dict, client_secret: str | None) -> dict:
    auth = None
    if client_secret:
        auth = (form["client_id"], client_secret)
    try:
        r = await client.post(endpoint, data=form, auth=auth, headers={"Accept": "application/json"}, timeout=15)
    except httpx.HTTPError as exc:
        raise OAuthError(f"I couldn't reach the service's sign-in: {exc.__class__.__name__}") from None
    if r.status_code != 200:
        raise OAuthError("The service didn't accept the sign-in. Sign in again." if form.get("grant_type") == "refresh_token"
                         else f"The service refused the sign-in ({r.status_code}).")
    d = r.json()
    if not d.get("access_token"):
        raise OAuthError("The service signed me in without a token.")
    return d


def _keep(d: dict, old: dict) -> dict:
    return {**old, "access": d["access_token"], "refresh": d.get("refresh_token") or old.get("refresh"),
            "expires_at": time.time() + float(d.get("expires_in") or 3600) - 30}


async def exchange(client: httpx.AsyncClient, saved: dict, code: str, verifier: str) -> dict:
    d = await _token(client, saved["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": saved["redirect_uri"],
        "client_id": saved["client_id"], "code_verifier": verifier, "resource": saved["resource"]},
        saved.get("client_secret"))
    return _keep(d, saved)


async def refresh(client: httpx.AsyncClient, saved: dict) -> dict:
    if not saved.get("refresh"):
        raise OAuthError("The sign-in ran out. Sign in again.")
    d = await _token(client, saved["token_endpoint"], {
        "grant_type": "refresh_token", "refresh_token": saved["refresh"], "client_id": saved["client_id"],
        "resource": saved["resource"]}, saved.get("client_secret"))
    return _keep(d, saved)


def fresh(saved: dict | None) -> bool:
    return bool(saved and saved.get("access") and float(saved.get("expires_at") or 0) > time.time())

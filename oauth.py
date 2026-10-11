"""OAuth 2.1 authorization server for OnCall's MCP endpoint (Alexa+ account linking).

Implements the MCP SDK's OAuthAuthorizationServerProvider over the same SQLite
database. The SDK handles protocol validation (PKCE S256, redirect URI match,
client authentication, metadata); this module owns policy and storage:

- Alexa+ uses a *static* client (ONCALL_OAUTH_CLIENT_ID/SECRET + the exact
  redirect URIs the Alexa AI CLI prints); it does not support dynamic
  registration. Dynamic registration stays available for other MCP clients,
  limited to HTTPS redirect URIs on an explicit host allowlist.
- Two tiers, per the Alexa+ MCP authentication model:
  * client_credentials -> `mcp:service` token (no user, no refresh). It can
    discover (initialize, tools/list, resources) but never call a tool, so the
    first tool call returns 401 and Alexa starts account linking.
  * authorization_code + PKCE S256 -> `oncall` token after operator consent.
- Authorization requires the *operator* to approve on a consent page by entering
  ONCALL_API_KEY. Single operator, so the API key is the identity proof.
- Consent requests are single-use, CSRF-bound and expire in 10 minutes.
- Codes expire in 5 minutes and are single-use. Access tokens live 1 hour.
- Refresh tokens rotate on every use; presenting a rotated token revokes the
  whole grant family (refresh-token theft detection, OAuth 2.1 §4.3.1).
- Codes and tokens are stored only as SHA-256 hashes.

OAuth access grants the same `oncall` scope as the shared key: approvals are
still bound to an expiring action ID, so a linked client cannot execute a
rollback without the explicit approve step.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import time
from urllib.parse import urlsplit

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from target_service import store

SCOPE = 'oncall'
SERVICE_SCOPE = 'mcp:service'
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
CONSENT_TTL = 600


def settings():
    import server
    return server.settings


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def init():
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth_consents (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, client_id TEXT NOT NULL,
          params TEXT NOT NULL, expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth_codes (hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, data TEXT NOT NULL,
          expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth_tokens (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
          family TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT, expires_at REAL NOT NULL,
          status TEXT NOT NULL DEFAULT 'active');
        CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family);''')


class OnCallOAuthProvider:
    # ── clients ────────────────────────────────────────────────────────
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        static = static_client()
        if static and hmac.compare_digest(client_id.encode(), static.client_id.encode()):
            return static
        init()
        with store.connect() as db:
            row = db.execute('SELECT info FROM oauth_clients WHERE client_id=?', (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row['info']) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        init()
        allowed = settings().oauth_redirect_hosts
        for uri in client_info.redirect_uris or []:
            parts = urlsplit(str(uri))
            if parts.scheme != 'https' or (parts.hostname or '').lower() not in allowed:
                raise RegistrationError('invalid_redirect_uri', 'Redirect URI host is not on the operator allowlist')
        scopes = (client_info.scope or SCOPE).split()
        if scopes != [SCOPE]:
            raise RegistrationError('invalid_client_metadata', f'Only the {SCOPE} scope is available')
        client_info.scope = SCOPE
        with store.connect() as db:
            count = db.execute('SELECT count(*) FROM oauth_clients').fetchone()[0]
            if count >= 50:
                raise RegistrationError('invalid_client_metadata', 'Client registration limit reached')
            db.execute('INSERT INTO oauth_clients VALUES(?,?,?)', (client_info.client_id, client_info.model_dump_json(), time.time()))

    # ── authorization (operator consent) ───────────────────────────────
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        init()
        if params.scopes and params.scopes != [SCOPE]:
            raise AuthorizeError('invalid_scope', f'Only the {SCOPE} scope is available')
        if params.resource and not same_resource(params.resource):
            raise AuthorizeError('invalid_request', 'resource must be this MCP server')
        request_id, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        with store.connect() as db:
            db.execute('DELETE FROM oauth_consents WHERE expires_at<?', (time.time(),))
            db.execute('INSERT INTO oauth_consents VALUES(?,?,?,?,?)', (request_id, _hash(csrf), client.client_id,
                       params.model_dump_json(), time.time() + CONSENT_TTL))
        # The CSRF secret travels only in this redirect to the operator's browser and is
        # stored hashed; the consent form must echo it back (stateless across restarts).
        return f"{settings().public_url.rstrip('/')}/oauth/consent?request={request_id}&t={csrf}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        init()
        with store.connect() as db:
            row = db.execute('SELECT * FROM oauth_codes WHERE hash=? AND client_id=?',
                             (_hash(authorization_code), client.client_id)).fetchone()
        return AuthorizationCode.model_validate_json(row['data']) if row else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Single use: delete wins exactly once even under concurrent exchanges.
            if db.execute('DELETE FROM oauth_codes WHERE hash=?', (_hash(authorization_code.code),)).rowcount != 1:
                raise TokenError('invalid_grant', 'authorization code already used')
            return self._issue(db, client.client_id, secrets.token_hex(16), authorization_code.scopes, authorization_code.resource)

    def _issue(self, db, client_id, family, scopes, resource) -> OAuthToken:
        access, refresh, now = secrets.token_urlsafe(32), secrets.token_urlsafe(48), time.time()
        for token, kind, ttl in ((access, 'access', ACCESS_TTL), (refresh, 'refresh', REFRESH_TTL)):
            db.execute('INSERT INTO oauth_tokens(hash,kind,client_id,family,scopes,resource,expires_at) VALUES(?,?,?,?,?,?,?)',
                       (_hash(token), kind, client_id, family, ' '.join(scopes), resource, now + ttl))
        return OAuthToken(access_token=access, token_type='Bearer', expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=' '.join(scopes))

    # ── refresh (rotation + reuse detection) ───────────────────────────
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        init()
        with store.connect() as db:
            row = db.execute("SELECT * FROM oauth_tokens WHERE hash=? AND kind='refresh'", (_hash(refresh_token),)).fetchone()
            if not row or row['client_id'] != client.client_id:
                return None
            if row['status'] == 'rotated':
                # A rotated token came back: assume theft and kill the whole grant.
                db.execute("UPDATE oauth_tokens SET status='revoked' WHERE family=?", (row['family'],))
                return None
            if row['status'] != 'active' or row['expires_at'] < time.time():
                return None
        return RefreshToken(token=refresh_token, client_id=row['client_id'], scopes=row['scopes'].split(),
                            expires_at=int(row['expires_at']), resource=row['resource'])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT family FROM oauth_tokens WHERE hash=? AND kind='refresh' AND status='active'",
                             (_hash(refresh_token.token),)).fetchone()
            if not row:
                raise TokenError('invalid_grant', 'refresh token is no longer valid')
            db.execute("UPDATE oauth_tokens SET status='rotated' WHERE hash=?", (_hash(refresh_token.token),))
            db.execute("UPDATE oauth_tokens SET status='revoked' WHERE family=? AND kind='access'", (row['family'],))
            return self._issue(db, client.client_id, row['family'], scopes, refresh_token.resource)

    # ── access tokens ──────────────────────────────────────────────────
    async def load_access_token(self, token: str) -> AccessToken | None:
        init()
        with store.connect() as db:
            row = db.execute("SELECT * FROM oauth_tokens WHERE hash=? AND kind='access' AND status='active'", (_hash(token),)).fetchone()
        if not row or row['expires_at'] < time.time():
            return None
        return AccessToken(token=token, client_id=row['client_id'], scopes=row['scopes'].split(),
                           expires_at=int(row['expires_at']), resource=row['resource'], subject='oncall-operator')

    async def revoke_token(self, token) -> None:
        with store.connect() as db:
            row = db.execute('SELECT family FROM oauth_tokens WHERE hash=?', (_hash(token.token),)).fetchone()
            if row:
                db.execute("UPDATE oauth_tokens SET status='revoked' WHERE family=?", (row['family'],))


    # ── client credentials (Alexa+ tier 1, service level) ──────────────
    def issue_service_token(self, client_id: str, resource: str) -> OAuthToken:
        init()
        access = secrets.token_urlsafe(32)
        with store.connect() as db:
            db.execute("DELETE FROM oauth_tokens WHERE kind='service' AND expires_at<?", (time.time(),))
            db.execute('INSERT INTO oauth_tokens(hash,kind,client_id,family,scopes,resource,expires_at) VALUES(?,?,?,?,?,?,?)',
                       (_hash(access), 'service', client_id, secrets.token_hex(16), SERVICE_SCOPE, resource, time.time() + ACCESS_TTL))
        # No refresh token for client credentials: the client asks again on expiry.
        return OAuthToken(access_token=access, token_type='Bearer', expires_in=ACCESS_TTL, scope=SERVICE_SCOPE)

    async def load_service_token(self, token: str) -> AccessToken | None:
        init()
        with store.connect() as db:
            row = db.execute("SELECT * FROM oauth_tokens WHERE hash=? AND kind='service' AND status='active'", (_hash(token),)).fetchone()
        if not row or row['expires_at'] < time.time():
            return None
        return AccessToken(token=token, client_id=row['client_id'], scopes=[SERVICE_SCOPE],
                           expires_at=int(row['expires_at']), resource=row['resource'], subject=None)


_provider = OnCallOAuthProvider()


def provider() -> OnCallOAuthProvider:
    return _provider


def resource_url() -> str:
    return settings().public_url.rstrip('/') + '/mcp'


def same_resource(value) -> bool:
    return str(value or '').rstrip('/') == resource_url()


def static_client() -> OAuthClientInformationFull | None:
    """The pre-registered Alexa+ client, built from settings on each call (no copy in the DB)."""
    cfg = settings()
    if not (cfg.oauth_client_id and cfg.oauth_client_secret and cfg.oauth_redirect_uris):
        return None
    return OAuthClientInformationFull(
        client_id=cfg.oauth_client_id, client_secret=cfg.oauth_client_secret, client_name='Alexa+',
        redirect_uris=list(cfg.oauth_redirect_uris), scope=SCOPE,
        grant_types=['authorization_code', 'refresh_token', 'client_credentials'],
        response_types=['code'], token_endpoint_auth_method='client_secret_basic')


def _basic_credentials(header: str) -> tuple[str, str] | None:
    import base64
    from urllib.parse import unquote
    if not header.startswith('Basic '):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if ':' not in decoded:
        return None
    client_id, secret = decoded.split(':', 1)
    return unquote(client_id), unquote(secret)


def _oauth_error(status: int, error: str, description: str) -> Response:
    from starlette.responses import JSONResponse
    return JSONResponse({'error': error, 'error_description': description}, status,
                        headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})


async def client_credentials_token(request: Request) -> Response:
    """RFC 6749 §4.4 for the static client: HTTP Basic or POST-body credentials,
    resource bound to this MCP server, `mcp:service` scope only, no refresh token."""
    form = await request.form()
    static = static_client()
    creds = _basic_credentials(request.headers.get('Authorization', ''))
    if creds is None and isinstance(form.get('client_id'), str) and isinstance(form.get('client_secret'), str):
        creds = (str(form['client_id']), str(form['client_secret']))
    if 'client_secret' in request.query_params:
        return _oauth_error(400, 'invalid_request', 'client credentials must not be sent in the query string')
    if not static or not creds or not (hmac.compare_digest(creds[0].encode(), static.client_id.encode())
                                       and hmac.compare_digest(creds[1].encode(), static.client_secret.encode())):
        return _oauth_error(401, 'invalid_client', 'client authentication failed')
    resource = str(form.get('resource') or '')
    if not resource:
        return _oauth_error(400, 'invalid_request', 'resource is required')
    if not same_resource(resource):
        return _oauth_error(403, 'access_denied', 'resource must be this MCP server')
    requested = str(form.get('scope') or SERVICE_SCOPE).split()
    if requested != [SERVICE_SCOPE]:
        return _oauth_error(400, 'invalid_scope', f'client credentials grant issues only {SERVICE_SCOPE}')
    token = provider().issue_service_token(static.client_id, resource_url())
    return Response(token.model_dump_json(exclude_none=True), 200, media_type='application/json',
                    headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})


def _consent(request_id):
    with store.connect() as db:
        return db.execute('SELECT * FROM oauth_consents WHERE id=? AND expires_at>?', (request_id, time.time())).fetchone()


PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize {name} — OnCall</title><style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem}}
label{{display:block;margin:1rem 0 .25rem;font-weight:600}}input{{width:100%;padding:.5rem;font:inherit}}
button{{padding:.6rem 1rem;font:inherit;margin-right:.5rem}}dl{{background:#f4f4f1;padding:1rem;border-radius:6px}}dt{{font-weight:600}}</style></head>
<body><main><h1>Authorize {name}</h1><p>This client is requesting access to OnCall's incident tools. It will be able to read
evidence and request rollback proposals. Every rollback still needs explicit approval of the exact target.</p>
<dl><dt>Client</dt><dd>{name}</dd><dt>Returns to</dt><dd>{host}</dd><dt>Access</dt><dd>oncall (1-hour tokens, revocable)</dd></dl>
<form method="post" action="/oauth/consent"><input type="hidden" name="request" value="{request}"><input type="hidden" name="csrf" value="{csrf}">
<label for="key">Operator API key</label><input id="key" name="operator_key" type="password" autocomplete="off" required>
<p><button name="decision" value="approve" type="submit">Approve</button><button name="decision" value="deny" type="submit" formnovalidate>Deny</button></p>
</form></main></body></html>'''


async def consent_page(request: Request) -> Response:
    request_id, csrf = request.query_params.get('request', ''), request.query_params.get('t', '')
    row = _consent(request_id)
    if not row or not hmac.compare_digest(row['csrf'], _hash(csrf)):
        return HTMLResponse('<p>This authorization request expired or was already used. Start linking again.</p>', 400,
                            headers={'Cache-Control': 'no-store'})
    client = await provider().get_client(row['client_id'])
    params = AuthorizationParams.model_validate_json(row['params'])
    return HTMLResponse(PAGE.format(name=html.escape(client.client_name or client.client_id),
                                    host=html.escape(urlsplit(str(params.redirect_uri)).hostname or ''),
                                    request=html.escape(request_id), csrf=html.escape(csrf)),
                        headers={'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
                                 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"})


async def consent_submit(request: Request) -> Response:
    form = await request.form()
    request_id, csrf = str(form.get('request', '')), str(form.get('csrf', ''))
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM oauth_consents WHERE id=? AND expires_at>?', (request_id, time.time())).fetchone()
        if not row or not hmac.compare_digest(row['csrf'], _hash(csrf)):
            return HTMLResponse('<p>Invalid or expired authorization request.</p>', 400)
        params = AuthorizationParams.model_validate_json(row['params'])
        if form.get('decision') != 'approve':
            db.execute('DELETE FROM oauth_consents WHERE id=?', (request_id,))
            return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), error='access_denied', state=params.state), 302)
        key = settings().api_key
        if not key or not hmac.compare_digest(str(form.get('operator_key', '')).encode(), key.encode()):
            # Consent stays valid for a retry; a wrong key never consumes it.
            return HTMLResponse('<p>Operator key not accepted. Go back and try again.</p>', 401)
        db.execute('DELETE FROM oauth_consents WHERE id=?', (request_id,))
        code = secrets.token_urlsafe(32)
        data = AuthorizationCode(code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + CODE_TTL,
                                 client_id=row['client_id'], code_challenge=params.code_challenge,
                                 redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                                 resource=params.resource, subject='oncall-operator')
        db.execute('INSERT INTO oauth_codes VALUES(?,?,?,?)', (_hash(code), row['client_id'], data.model_dump_json(), data.expires_at))
    import oncall_core
    oncall_core.audit('MCP client linked', f'OAuth access approved for {json.dumps(row["client_id"])[:80]}.', 'auth')
    return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 302)

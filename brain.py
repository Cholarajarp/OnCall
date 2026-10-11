"""Same-origin REST/UI entrypoint. MCP carries real workflow calls and evidence."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import re
import time
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from config import settings
import oncall_core as core

MCP_URL = settings.mcp_url
ESCALATION_INTERVAL = 30.0
SANDBOX_TRAFFIC_INTERVAL = 10.0
_background: dict[str, Any] = {'escalation': None, 'traffic': None}


async def _escalation_loop():
    """Re-page / escalate unacknowledged incidents. One failed pass never stops the loop."""
    while True:
        await asyncio.sleep(ESCALATION_INTERVAL)
        try:
            await core.escalation_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # paging is best effort; the incident and its audit remain authoritative


async def _sandbox_traffic_loop():
    """Sandbox only: steady real checkout traffic so impact and health stay fresh."""
    while True:
        await asyncio.sleep(SANDBOX_TRAFFIC_INTERVAL)
        try:
            await core.sandbox_traffic_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # a missed batch only means slightly older telemetry; assess() fails closed


@contextlib.asynccontextmanager
async def lifespan(_app):
    tasks = {'escalation': asyncio.create_task(_escalation_loop())}
    if settings.mode == 'sandbox':
        tasks['traffic'] = asyncio.create_task(_sandbox_traffic_loop())
    _background.update(tasks)
    try:
        yield
    finally:
        for task in tasks.values():
            task.cancel()
        for task in tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for name in tasks:
            _background[name] = None


app = FastAPI(title='OnCall incident command', docs_url=None, redoc_url=None, lifespan=lifespan)


class CommandBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=2000)


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approved: StrictBool
    action_id: str = Field(min_length=64, max_length=64, pattern=r'^[a-f0-9]+$')


class AlertItem(BaseModel):
    # Alertmanager sends many extra fields (generatorURL, endsAt...); ignore them.
    model_config = ConfigDict(extra='ignore')
    status: str | None = Field(default=None, max_length=16)
    labels: dict[str, str] = Field(max_length=64)
    annotations: dict[str, str] = Field(default_factory=dict, max_length=64)
    fingerprint: str | None = Field(default=None, max_length=128)


class AlertBody(BaseModel):
    model_config = ConfigDict(extra='ignore')
    status: str | None = Field(default=None, max_length=16)
    alerts: list[AlertItem] = Field(min_length=1, max_length=core.MAX_ALERTS)


class AckBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    incident_id: str = Field(min_length=16, max_length=16, pattern=r'^[a-f0-9]+$')


class VerifyBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    watch_seconds: int | None = None


class InvestigateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str | None = Field(default=None, max_length=2000)


class RateLimiter:
    """Per-client token bucket. In-process: correct for this single-instance deployment."""
    def __init__(self, capacity: int, per_seconds: float):
        self.capacity, self.rate = capacity, capacity / per_seconds
        self.buckets: dict[tuple[str, str], tuple[float, float]] = {}

    def allow(self, client: str, route: str) -> bool:
        now = time.monotonic()
        tokens, last = self.buckets.get((client, route), (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if len(self.buckets) > 10000:  # bound memory under address spraying
            self.buckets.clear()
        if tokens < 1:
            self.buckets[(client, route)] = (tokens, now)
            return False
        self.buckets[(client, route)] = (tokens - 1, now)
        return True


# Generous for an operator and Alertmanager; tight enough to blunt key guessing and floods.
# investigate can spend Bedrock credits; demo controls change what the stack serves.
LIMITS = {'public': RateLimiter(60, 60), 'alerts': RateLimiter(120, 60), 'operator': RateLimiter(240, 60),
          'auth_failure': RateLimiter(20, 300), 'investigate': RateLimiter(6, 60), 'demo': RateLimiter(3, 180)}
COSTLY = {'/api/investigate': 'investigate', '/api/demo/start': 'demo', '/api/demo/reset': 'demo'}
# Conversational Bedrock replies (per session). Over the cap, chat falls back to help text.
CHAT_LIMIT = RateLimiter(20, 60)


def client_ip(peer: str | None, forwarded: str | None) -> str:
    """Trust X-Forwarded-For only from the loopback reverse proxy (Caddy)."""
    if peer in ('127.0.0.1', '::1') and forwarded:
        return forwarded.split(',')[0].strip()[:64]
    return peer or 'unknown'


def _limit_class(path: str) -> str | None:
    if path.startswith('/api/public/'):
        return 'public'
    if path == '/api/alerts':
        return 'alerts'
    if path.startswith('/api/') or path in ('/command', '/confirm'):
        return 'operator'
    return None


def _too_many():
    return JSONResponse({'detail': 'Too many requests. Slow down and retry.'}, status_code=429, headers={'Retry-After': '30'})


@app.middleware('http')
async def authorize(request: Request, call_next):
    # /api/public/* is the only unauthenticated API surface (redacted, read-only, opt-in).
    operational = (request.url.path.startswith('/api/') and not request.url.path.startswith('/api/public/')) \
        or request.url.path in ('/command', '/confirm')
    limit = _limit_class(request.url.path)
    ip = client_ip(request.client.host if request.client else None, request.headers.get('X-Forwarded-For'))
    if limit and not LIMITS[limit].allow(ip, limit):
        return _too_many()
    costly = COSTLY.get(request.url.path) if request.method == 'POST' else None
    if costly and not LIMITS[costly].allow(ip, costly):
        return _too_many()
    if operational:
        if settings.api_key and not hmac.compare_digest(request.headers.get('Authorization', ''), f'Bearer {settings.api_key}'):
            # Failed keys drain a separate, slow bucket: brute force stalls quickly.
            if not LIMITS['auth_failure'].allow(ip, 'auth'):
                return _too_many()
    if operational:
        if settings.mode == 'live' and not settings.api_key:
            return JSONResponse({'detail': 'Live API is not configured'}, status_code=503)
        if settings.api_key and not hmac.compare_digest(request.headers.get('Authorization', ''), f'Bearer {settings.api_key}'):
            return JSONResponse({'detail': 'Bearer authentication required'}, status_code=401, headers={'WWW-Authenticate': 'Bearer'})
        try:
            core.session_key(request.headers.get('X-OnCall-Session', 'local-cli'))
        except ValueError as exc:
            return JSONResponse({'detail': str(exc)}, status_code=400)
        length = request.headers.get('content-length', '0')
        if not length.isdigit() or int(length) > 16384:
            return JSONResponse({'detail': 'Request too large'}, status_code=413)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if operational:
        response.headers['Cache-Control'] = 'no-store'
    return response


class ToolFailure(RuntimeError):
    pass


def decode_tool_result(name, args, result, steps, at, duration_ms) -> dict[str, Any]:
    data = getattr(result, 'structuredContent', None)
    if isinstance(data, dict) and set(data) == {'result'}:
        data = data['result']
    if data is None:
        for block in result.content or []:
            if getattr(block, 'type', '') == 'text':
                try:
                    data = json.loads(block.text)
                except (ValueError, TypeError):
                    data = {'message': block.text}
                break
    is_error = bool(getattr(result, 'isError', False)) or not isinstance(data, dict)
    steps.append({'tool': name, 'args': args, 'result': data, 'at': at, 'duration_ms': round(duration_ms, 1), 'is_error': is_error})
    if is_error:
        raise ToolFailure(f'{name} failed; no operational conclusion can be drawn')
    return cast(dict[str, Any], data)


async def _call_tool(name: str, args: dict[str, Any], steps: list[dict]) -> dict[str, Any]:
    at = core.now()
    started = time.perf_counter()
    headers = {'Authorization': f'Bearer {settings.api_key}'} if settings.api_key else None
    try:
        # Read timeout covers the longest watch-window verification (120s) plus probes.
        async with streamablehttp_client(MCP_URL, headers=headers, timeout=15, sse_read_timeout=180) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, args)
        return decode_tool_result(name, args, result, steps, at, (time.perf_counter() - started) * 1000)
    except ToolFailure:
        raise
    except Exception as exc:
        steps.append({'tool': name, 'args': args, 'result': {'error': 'MCP connection or tool execution failed'},
                      'at': at, 'duration_ms': round((time.perf_counter() - started) * 1000, 1), 'is_error': True})
        raise ToolFailure('MCP is unavailable; health and execution are not confirmed') from exc


HELP_REPLY = 'I can diagnose the incident, inspect logs, propose a rollback, verify recovery, or save a handoff.'


async def route(text: str, session_id='local-cli'):
    core.session_key(session_id)
    steps = []
    t = text.strip().lower()
    try:
        # Text alone never executes a mutation. The only spoken path to a change is a
        # one-time code delivered out-of-band and bound to the exact pending action ID.
        challenge = core.voice_challenge(session_id)
        action = core.pending(session_id)
        if challenge and challenge['status'] == 'pending' and core.spoken_code(text):
            result = await _call_tool('approve_with_voice_code', {'session_id': session_id, 'action_id': challenge['action_id'], 'spoken_code': text}, steps)
            speech = result['speech']
        elif action and re.fullmatch(r'(ok(ay)?\s+)?(approve|approve (it|the rollback|rollback)|voice approve|send (me )?(the|a) code)', t):
            result = await _call_tool('request_voice_approval', {'session_id': session_id, 'action_id': action['id']}, steps)
            speech = result['speech']
        elif action and re.fullmatch(r"(no|stop|cancel|cancel (it|that|the rollback|rollback)|don'?t( do it)?|never mind|abort)[.!]?", t):
            # Only a short, unambiguous refusal cancels, and only while something is pending.
            result = await _call_tool('execute_action', {'session_id': session_id, 'action_id': action['id'], 'approved': False}, steps)
            speech = result['speech']
        elif re.search(r"\b(don'?t|do not|never|no need to)\b.*\b(roll ?back|revert|approve|deploy)", t):
            # A negated action request must never be routed to the action it names.
            speech = ('Nothing was approved. The proposal stays pending until you cancel it or it expires.'
                      if action else 'Understood. Nothing will be changed.')
        elif re.fullmatch(r"(ok(ay)?[, ]*)?(yes|yeah|yep|sure|confirm(ed)?|go ahead|do it|maybe)( please| now)?[.!]?", t) or (action and re.search(r'\b(confirm|go ahead|do it)\b', t)):
            speech = ('Saying yes does not approve a change. Say "approve" to get a one-time code, or use the approval control.'
                      if action else 'There is nothing to approve. Ask me to review a rollback first.')
        else:
            intent = None
            if re.search(r'\b(rollback|revert)\b|roll.*back', t):
                intent = 'rollback'
            elif re.search(r'\b(verify|recovery|recovered)\b', t):
                intent = 'verify'
            elif re.search(r'\b(logs?|dig|why)\b|root cause', t):
                intent = 'logs'
            elif re.search(r'\b(repo|github|ci)\b', t):
                intent = 'repo'
            elif re.search(r'\b(report|handoff)\b', t):
                intent = 'report'
            elif re.search(r"on fire|\b(status|incident|diagnose|investigate|outage|broken|down)\b|\bhealth"
                           r"|what'?s (going on|happening|wrong)|what is (going on|happening|wrong)|is (it|checkout|\w+) (ok|okay|fine|working)", t):
                intent = 'status'
            # Anything else is conversation: Bedrock chat with read-only tools (below).
            tool_args = {
                'status': ('diagnose_incident', {'session_id': session_id, 'service': settings.service}),
                'logs': ('search_logs', {'query': '', 'window_minutes': 30}),
                'rollback': ('propose_action', {'session_id': session_id, 'service': settings.service}),
                'verify': ('verify_recovery', {'session_id': session_id}),
                'repo': ('get_repo_status', {}), 'report': ('generate_report', {})}
            selected = tool_args.get(intent) if intent else None
            if intent == 'status' and re.fullmatch(r"(what'?s|what is) on fire\??|status|brief me|briefing", t):
                # Fast, read-only voice briefing first; deep investigation on request.
                result = await _call_tool('incident_briefing', {'session_id': session_id}, steps)
                speech = result['speech']
                selected = None
            elif intent == 'status':
                # Multi-step investigation (Bedrock when configured, deterministic otherwise).
                import agent
                brief = await agent.investigate(session_id, question=text, call_tool=_call_tool, steps=steps)
                speech = brief['speech']
                selected = None
            elif selected:
                name, args = selected
                result = await _call_tool(name, args, steps)
                speech = result.get('speech')
                if not speech:
                    if intent == 'logs':
                        if result.get('status') == 'unavailable':
                            speech = 'Log evidence is unavailable. No conclusion can be drawn from the missing search result.'
                        else:
                            speech = f"Found {result.get('count', 0)} matching error records. Inspect the evidence; logs do not by themselves prove root cause."
                    elif intent == 'report':
                        speech = 'Incident handoff saved. Open or download the report.'
                    else:
                        speech = result.get('message', f"Repository evidence retrieved: {result.get('status', 'available')}.")
            else:
                # Conversation: Bedrock answers with read-only evidence tools; it cannot
                # propose, approve or execute. Deterministic help text if no model.
                import agent
                chat = None
                if CHAT_LIMIT.allow(session_id, 'chat'):
                    chat = await agent.converse(session_id, text=text, call_tool=_call_tool, steps=steps)
                speech = chat['speech'] if chat else HELP_REPLY
    except Exception:
        speech = 'Evidence is unavailable or the operation was rejected. Health and recovery are not confirmed. Inspect the tool results and retry read-only diagnosis.'
    # Chat memory for follow-ups. Codes are never stored; converse() records its own turns.
    if not core.spoken_code(text):
        import agent
        if not agent.last_recorded(session_id, text, speech):
            agent.remember(session_id, text, speech)
    return {'speech': speech, 'steps': steps, 'pending': core.pending(session_id)}


def session(request):
    return request.headers.get('X-OnCall-Session', 'local-cli')


@app.get('/health')
async def health():
    return {'status': 'ok'}


STATUS_PAGE = Path(__file__).resolve().parent / 'app' / 'status.html'


@app.get('/status', include_in_schema=False)
async def status_page():
    if not settings.public_status:
        raise HTTPException(status_code=404)
    from fastapi.responses import FileResponse
    return FileResponse(STATUS_PAGE, media_type='text/html', headers={'Cache-Control': 'no-cache'})


PUBLIC_TTL = 5.0
_public_cache: dict[str, Any] = {'at': 0.0, 'body': None}


@app.get('/api/public/status')
async def public_status():
    """Unauthenticated, redacted, read-only. Enabled only with ONCALL_PUBLIC_STATUS=true.

    Cached for 5s server-side so any number of viewers costs at most one
    Prometheus + ledger read per interval on the small host.
    """
    if not settings.public_status:
        raise HTTPException(status_code=404)
    if _public_cache['body'] is None or time.monotonic() - _public_cache['at'] > PUBLIC_TTL:
        _public_cache.update(body=await core.public_status(), at=time.monotonic())
    return JSONResponse(_public_cache['body'], headers={'Cache-Control': 'public, max-age=5'})


@app.get('/api/state')
async def state(request: Request):
    return await core.state(session(request))


@app.post('/command')
async def command(body: CommandBody, request: Request):
    return await route(body.text, session(request))


@app.post('/confirm')
async def confirm(body: ConfirmBody, request: Request):
    steps = []
    sid = session(request)
    try:
        result = await _call_tool('execute_action', {'session_id': sid, 'action_id': body.action_id, 'approved': body.approved}, steps)
        speech = result['speech']
    except ToolFailure:
        speech = 'Execution could not be confirmed. Refresh state; do not assume the deployment changed.'
    return {'speech': speech, 'steps': steps, 'pending': core.pending(sid)}


class VoiceCodeBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action_id: str = Field(min_length=64, max_length=64, pattern=r'^[a-f0-9]+$')
    code: str | None = Field(default=None, min_length=1, max_length=200)


@app.post('/api/voice-approval')
async def voice_approval(body: VoiceCodeBody, request: Request):
    """Without `code`: send a one-time code. With `code`: approve the exact action once if it matches."""
    steps = []
    sid = session(request)
    try:
        if body.code is None:
            result = await _call_tool('request_voice_approval', {'session_id': sid, 'action_id': body.action_id}, steps)
        else:
            result = await _call_tool('approve_with_voice_code', {'session_id': sid, 'action_id': body.action_id, 'spoken_code': body.code}, steps)
        speech = result['speech']
    except ToolFailure:
        speech = 'Voice approval could not be confirmed. Refresh state; do not assume the deployment changed.'
    return {'speech': speech, 'steps': steps, 'pending': core.pending(sid), 'voice_challenge': core.voice_challenge(sid)}


def _demo_error(exc):
    disabled = settings.mode == 'live' and not settings.demo_controls
    return HTTPException(status_code=403 if disabled else 409, detail=str(exc))


@app.post('/api/demo/start')
async def demo_start(request: Request):
    """Sandbox: inject the fault. Live: only with ONCALL_DEMO_CONTROLS=true, ships the known-bad release."""
    try:
        return await core.start_demo(session(request))
    except ValueError as exc:
        raise _demo_error(exc) from exc


@app.post('/api/demo/reset')
async def demo_reset(request: Request):
    """Back to a healthy baseline: close open incidents as 'Demo reset', cancel approvals, restore the good release."""
    try:
        return await core.reset_demo(session(request))
    except ValueError as exc:
        raise _demo_error(exc) from exc


@app.get('/api/incidents')
async def incidents():
    return {'incidents': core.incidents(20)}


@app.post('/api/check-recovery')
async def check_recovery(request: Request):
    """Non-blocking recovery check: starts the watch window, then reports progress or the verdict."""
    steps = []
    sid = session(request)
    try:
        result = await _call_tool('check_recovery', {'session_id': sid}, steps)
    except ToolFailure:
        result = {'status': 'unknown', 'health': 'unknown',
                  'speech': 'Recovery is not verified. The recovery check is unavailable.'}
    return {**result, 'steps': steps, 'pending': core.pending(sid)}


@app.post('/api/alerts', status_code=202)
async def alerts(body: AlertBody):
    """Monitoring webhook (Alertmanager shape). Opens/attaches the incident and pages once."""
    try:
        result = core.ingest_alert(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result['opened'] and result['incident']:
        result['page'] = await core.notify(result['incident']['title'], 'New incident. Open the workspace to investigate and acknowledge.')
    elif result['incident'] and core.renotify_due():
        result['page'] = await core.notify(result['incident']['title'], 'Still unacknowledged.')
    return result


@app.post('/api/ack')
async def ack(body: AckBody, request: Request):
    return core.acknowledge(session(request), body.incident_id)


@app.post('/api/investigate')
async def investigate(request: Request, body: InvestigateBody | None = None):
    import agent
    sid = session(request)
    steps = []
    try:
        brief = await agent.investigate(sid, question=body.question if body else None, call_tool=_call_tool, steps=steps)
    except ToolFailure:
        brief = {'mode': 'unavailable', 'speech': 'Evidence is unavailable. Health and recovery are not confirmed.'}
    return {'speech': brief['speech'], 'mode': brief['mode'], 'steps': steps, 'pending': core.pending(sid)}


@app.get('/api/brief')
async def brief():
    import agent
    return agent.latest_brief() or {'speech': None}


@app.post('/api/verify')
async def verify(request: Request, body: VerifyBody | None = None):
    steps = []
    sid = session(request)
    args = {'session_id': sid}
    if body and body.watch_seconds is not None:
        args['watch_seconds'] = body.watch_seconds
    try:
        result = await _call_tool('verify_recovery', args, steps)
        speech = result['speech']
    except ToolFailure:
        speech = 'Recovery is not verified. The verification tool is unavailable.'
    return {'speech': speech, 'steps': steps, 'pending': core.pending(sid), 'state': await core.state(sid)}


@app.get('/api/report')
async def report():
    return core.report(persist=False)


app.mount('/', StaticFiles(directory=Path(__file__).resolve().parent / 'app', html=True), name='ui')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)

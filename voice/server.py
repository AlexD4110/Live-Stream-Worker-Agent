"""The web server Plivo talks to.

POST /plivo/answer   Plivo asks what to do with an incoming call (its "Answer URL"). We check the request really came
                     from Plivo and that the caller is on the allow-list, then reply with XML that opens an audio stream.
WS   /plivo/stream   The call's audio. We read the stream's opening message, check the pass from the webhook, then hand
                     the call to the voice pipeline.
GET  /health         Is everything configured?
"""
import asyncio
import logging
import re
import secrets
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from gifting import analysis
from voice import config, plivo, security, tools, web

log = logging.getLogger(__name__)
XML = "application/xml"
HOST_OK = re.compile(r"^[A-Za-z0-9.-]+(:\d+)?$")
HANDSHAKE_SECONDS = 5


def create_app(cfg, data=None, run_bot=None, warm=False):
    app = FastAPI(title="Gifting Pulse voice")
    analyst = tools.Analyst(data or analysis.load(cfg.data_path),
                            web=web.Tavily(cfg.tavily_api_key) if cfg.tavily_api_key else None)
    app.state.analyst = analyst
    if warm:
        analyst.warm()                                # so the first question on a call is answered at once
    app.state.secret = secrets.token_hex(16)
    app.state.limiter = security.CallLimiter(1)

    if run_bot is None:
        from voice.pipeline import run_bot as _run_bot
        run_bot = _run_bot

    def host_of(request):
        return cfg.public_host or request.headers.get("x-forwarded-host") or request.headers.get("host", "")

    @app.get("/health")
    def health():
        found = config.problems(cfg)
        return {"ok": True, "ready": not found, "problems": found}

    @app.post("/plivo/answer")
    async def answer(request: Request):
        params = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
        host = host_of(request)
        if not HOST_OK.match(host):
            return Response("bad host", status_code=400)
        if cfg.plivo_auth_token and not plivo.valid_request(
                cfg.plivo_auth_token, "POST", f"https://{host}/plivo/answer", params,
                request.headers.get("x-plivo-signature-v3"), request.headers.get("x-plivo-signature-v3-nonce")):
            log.warning("rejected a request with a bad Plivo signature")
            return Response("forbidden", status_code=403)
        if not security.caller_allowed(params.get("From", ""), cfg.allowed_callers):
            log.info("turned away a caller who is not on the allow-list")
            return Response(plivo.private_xml(), media_type=XML)
        return Response(plivo.answer_xml(host, security.make_stream_token(app.state.secret)), media_type=XML)

    @app.websocket("/plivo/stream")
    async def stream(websocket: WebSocket):
        await websocket.accept()
        if not app.state.limiter.acquire():
            await websocket.close(code=1013)
            return
        try:
            try:
                hello = await asyncio.wait_for(
                    plivo.read_handshake(websocket, disconnected=(WebSocketDisconnect,)), HANDSHAKE_SECONDS)
            except (asyncio.TimeoutError, ValueError):
                await websocket.close(code=1008)
                return
            if not security.check_stream_token(app.state.secret, hello.token):
                log.warning("closed a stream with a missing or invalid pass")
                await websocket.close(code=1008)
                return
            from pipecat.runner.types import CallData
            call_data = CallData(stream_id=hello.stream_id, call_id=hello.call_id, body={})
            log.info("call started")
            await run_bot(websocket, call_data, cfg, analyst)
            log.info("call ended")
        finally:
            app.state.limiter.release()
            if websocket.client_state == WebSocketState.CONNECTED:       # always hang up cleanly
                try:
                    await websocket.close()
                except RuntimeError:
                    pass

    return app

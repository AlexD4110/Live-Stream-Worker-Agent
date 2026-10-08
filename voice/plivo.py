"""Everything specific to Plivo: checking a request really came from Plivo, the XML we answer a call with, and
reading the audio stream's opening message.

Plivo's request signing ("V3") is implemented the way Plivo's own Python SDK does it. A call works like this:
Plivo POSTs to our Answer URL; we reply with XML that tells it to open a two-way audio stream to our websocket;
the stream's first message is a `start` event carrying the call and stream ids and the `extraHeaders` we set.
"""
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

MAX_HANDSHAKE_MESSAGES = 8
QUOTE = {'"': "&quot;"}


class Disconnected(Exception):
    """The caller or Plivo hung up before the stream started."""


# --- request signing (V3) ------------------------------------------------------------------------

def _query_string(query):
    parts = []
    for key in sorted(query):
        parts.append("&".join(f"{key}={v}" for v in sorted(query[key])))
    return "&".join(parts)


def _fields_string(params):
    return "".join(f"{k}{params[k]}" for k in sorted(params))


def _base_string(method, url, params):
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}{u.path}"
    query = parse_qs(u.query, keep_blank_values=True)
    if method.upper() == "GET":
        merged = {**{k: [v] for k, v in params.items()}, **query}
        q = _query_string(merged)
        return base + ("?" + q if q else "")
    q = _query_string(query)
    if q or params:
        base += "?" + q
    if q and params:
        base += "."
    return base + _fields_string(params)


def signature_v3(auth_token, method, url, params, nonce):
    base = f"{_base_string(method, url, params)}.{nonce}"
    return base64.b64encode(hmac.new(auth_token.encode(), base.encode(), hashlib.sha256).digest()).decode()


def valid_request(auth_token, method, url, params, signature_header, nonce):
    if not signature_header or not nonce:
        return False
    expected = signature_v3(auth_token, method, url, params, nonce)
    return any(hmac.compare_digest(expected, s.strip()) for s in signature_header.split(","))


# --- the XML we answer with --------------------------------------------------------------------

def answer_xml(host, token):
    """Open a two-way phone-quality (8 kHz mu-law) audio stream to us. The token is our short-lived pass."""
    header = escape(f"token={token}", QUOTE)
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000" '
            f'extraHeaders="{header}">wss://{escape(host)}/plivo/stream</Stream></Response>')


def private_xml():
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Speak>Sorry, this line is private. Goodbye.</Speak><Hangup/></Response>'


# --- the stream's opening message --------------------------------------------------------------

def parse_extra_headers(raw):
    """'a=1;b=2' -> {'a': '1', 'b': '2'}. Anything else gives an empty dict."""
    out = {}
    if not isinstance(raw, str):
        return out
    for part in raw.replace(",", ";").split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            if key.strip():
                out[key.strip()] = value.strip()
    return out


@dataclass
class Handshake:
    call_id: str
    stream_id: str
    token: str | None


async def read_handshake(websocket, disconnected=(Disconnected,)):
    """Read messages until Plivo's `start` event. Raises ValueError if it never comes or has no ids."""
    for _ in range(MAX_HANDSHAKE_MESSAGES):
        try:
            raw = await websocket.receive_text()
        except disconnected:
            raise ValueError("hung up before the stream started") from None
        except KeyError:                                       # a binary frame: not the start event
            continue
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(msg, dict) or msg.get("event") != "start":
            continue
        start = msg.get("start") if isinstance(msg.get("start"), dict) else {}
        call_id, stream_id = start.get("callId"), start.get("streamId")
        if not call_id or not stream_id:
            raise ValueError("the start event had no call or stream id")
        extra = msg.get("extra_headers") or start.get("extra_headers")
        return Handshake(str(call_id), str(stream_id), parse_extra_headers(extra).get("token"))
    raise ValueError("no start event arrived")

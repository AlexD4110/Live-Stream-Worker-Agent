"""python -m voice.check          settings only, no internet
python -m voice.check --live   also tries Groq, Deepgram, Modulate (hearing and the cloned-voice check), Tavily, Plivo and your public tunnel address"""
import asyncio
import base64
import json
import sys
import urllib.error
import urllib.request

from voice import brain, config, web


def _http_get(url, headers=None, timeout=10, body=None):
    """Returns the HTTP status. With a body it is a POST of that JSON, otherwise a GET."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"User-Agent": "gifting-pulse/1.0", **({"Content-Type": "application/json"} if data else {}),
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _modulate_probe(api_key, url_for=None):
    """Open a Modulate stream and end it at once. Returns the close code Modulate answered with."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    from voice import modulate_stt

    url = (url_for or modulate_stt.build_url)(api_key, 16000)

    async def go():
        try:
            async with connect(url, open_timeout=10) as ws:
                await ws.send("")
                try:
                    await asyncio.wait_for(ws.recv(), 10)
                except ConnectionClosed:
                    pass
                await ws.wait_closed()
                return ws.close_code
        except InvalidStatus as e:
            return modulate_stt._HTTP_AS_CLOSE.get(e.response.status_code, 1011)
    return asyncio.run(go())


def _voice_probe(api_key):
    from voice import modulate_svd
    return _modulate_probe(api_key, modulate_svd.build_url)


def run(cfg, live=False, http_get=_http_get, groq_post=brain._post, modulate_probe=_modulate_probe, tavily_post=web._post, voice_probe=_voice_probe):
    rows = []
    found = config.problems(cfg)
    rows += [("Settings", False, p) for p in found] or [("Settings", True, "all required settings are present")]
    if not cfg.plivo_auth_token:
        rows.append(("Settings", True, "PLIVO_AUTH_TOKEN is not set: requests from Plivo will not be verified (fine for a quick test)."))
    if not live:
        return rows

    if cfg.groq_api_key:
        try:
            brain.groq_chat(cfg.groq_api_key, cfg.groq_model, post=groq_post)([{"role": "user", "content": "Say ok."}], [])
            rows.append(("Groq", True, f"answered using {cfg.groq_model}"))
        except brain.BrainError as e:
            rows.append(("Groq", False, str(e)))
    if cfg.deepgram_api_key:
        # Test exactly what the agent needs, speaking, with two characters. Listing projects would need a permission
        # that an ordinary key may not have, and would report a good key as bad.
        try:
            code = http_get(f"https://api.deepgram.com/v1/speak?model={cfg.tts_voice}",
                            {"Authorization": f"Token {cfg.deepgram_api_key}"}, body={"text": "OK"})
            detail = {200: "key accepted for speaking",
                      401: "key rejected: check it was copied in full",
                      403: "key has no permission to speak: create it with the Member role or higher, in a project with text-to-speech",
                      402: "the account is out of credit",
                      429: "Deepgram's rate limit was hit; try again in a minute"}.get(code, f"unexpected reply {code}")
            rows.append(("Deepgram", code == 200, detail))
        except (urllib.error.URLError, TimeoutError):
            rows.append(("Deepgram", False, "could not reach Deepgram; check the internet connection"))
    if cfg.stt_provider == "modulate" and cfg.modulate_api_key:
        from voice import modulate_stt
        try:
            code = modulate_probe(cfg.modulate_api_key)
            good = code in (None, 1000)
            rows.append(("Modulate", good, "key accepted" if good else modulate_stt.close_reason(code)))
        except (OSError, TimeoutError, asyncio.TimeoutError):
            rows.append(("Modulate", False, "could not reach Modulate; check the internet connection"))
    if cfg.fraud_voice_check:
        from voice import modulate_stt
        try:
            code = voice_probe(cfg.modulate_api_key)
            good = code in (None, 1000)
            rows.append(("Modulate voice check", good, "key accepted for the cloned-voice detector" if good else modulate_stt.close_reason(code)))
        except (OSError, TimeoutError, asyncio.TimeoutError):
            rows.append(("Modulate voice check", False, "could not reach Modulate; check the internet connection"))
    if cfg.tavily_api_key:
        try:
            web.Tavily(cfg.tavily_api_key, post=tavily_post).search("live streaming gifts")
            rows.append(("Tavily", True, "key accepted (used 1 search credit)"))
        except web.WebSearchError as e:
            rows.append(("Tavily", False, str(e)))
    if cfg.plivo_auth_id and cfg.plivo_auth_token:
        login = base64.b64encode(f"{cfg.plivo_auth_id}:{cfg.plivo_auth_token}".encode()).decode()
        try:
            code = http_get(f"https://api.plivo.com/v1/Account/{cfg.plivo_auth_id}/", {"Authorization": f"Basic {login}"})
            rows.append(("Plivo", code == 200, "credentials accepted" if code == 200 else
                         "credentials rejected" if code in (401, 403) else f"unexpected reply {code}"))
        except (urllib.error.URLError, TimeoutError):
            rows.append(("Plivo", False, "could not reach Plivo; check the internet connection"))
    if cfg.public_host:
        try:
            code = http_get(f"https://{cfg.public_host}/health")
            rows.append(("Tunnel", code == 200, "reachable and the server answered" if code == 200 else
                         f"got {code}: is the tunnel running and is the voice server started (python -m voice)?"))
        except (urllib.error.URLError, TimeoutError):
            rows.append(("Tunnel", False, "could not reach it; start the tunnel and the voice server first"))
    return rows


def main(argv=None):
    live = "--live" in (argv if argv is not None else sys.argv[1:])
    rows = run(config.load(), live=live)
    for name, ok, detail in rows:
        print(f"{'OK  ' if ok else 'FAIL'} {name}: {detail}")
    if not live:
        print("\nAdd --live to also test Groq, Deepgram, Modulate, Tavily, Plivo and the tunnel.")
    return 0 if all(ok for _, ok, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main())

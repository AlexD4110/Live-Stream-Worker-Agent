"""Settings, the guards on the phone line, and the web server. No real call and no keys are needed."""
import json
import os
import tempfile
import unittest

from gifting import analysis, generate
from voice import config, plivo, security

try:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    HAVE_SERVER = True
except ImportError:                                      # only the third-party packages may be missing
    HAVE_SERVER = False
if HAVE_SERVER:
    from voice import server

ANALYST = "+15550100100"
ANALYST_FROM_PLIVO = "15550100100"          # Plivo sends numbers as digits with the country code, no plus
STRANGER_FROM_PLIVO = "15550199999"


class EnvFileTest(unittest.TestCase):
    def test_parse(self):
        text = '# comment\n\nGROQ_API_KEY=abc\nexport DEEPGRAM_API_KEY = "def" \nEMPTY=\nQUOTED=\'x y\'\nnot a line\n'
        self.assertEqual(config.parse_env(text),
                         {"GROQ_API_KEY": "abc", "DEEPGRAM_API_KEY": "def", "EMPTY": "", "QUOTED": "x y"})


class PhoneNumberTest(unittest.TestCase):
    def test_normalise(self):
        for raw, want in [("+1 (555) 010-0100", "+15550100100"), ("555-010-0100", "+15550100100"),
                          ("1 555 010 0100", "+15550100100"), ("+44 20 7946 0958", "+442079460958")]:
            self.assertEqual(config.normalise_number(raw), want, raw)

    def test_rejects_junk(self):
        for raw in ("", "call me", "12345", "+"):
            self.assertIsNone(config.normalise_number(raw), raw)

    def test_numbers_from_the_phone_company_may_lack_the_plus(self):
        self.assertEqual(config.normalise_number("442079460958", bare_e164=True), "+442079460958")
        self.assertEqual(config.normalise_number("15550100100", bare_e164=True), "+15550100100")
        self.assertIsNone(config.normalise_number("442079460958"))           # a person typing it must include the plus
        self.assertIsNone(config.normalise_number("123", bare_e164=True))


class ConfigTest(unittest.TestCase):
    ENV = {"GROQ_API_KEY": "g", "DEEPGRAM_API_KEY": "d", "PLIVO_AUTH_TOKEN": "t", "PLIVO_AUTH_ID": "i",
           "ALLOWED_CALLERS": "+1 555 010 0100, 555-010-0101 ,bogus", "PUBLIC_HOST": "https://demo.example.com/"}

    def test_load_normalises_values(self):
        c = config.load(self.ENV, env_file=None)
        self.assertEqual(c.allowed_callers, ("+15550100100", "+15550100101"))
        self.assertEqual(c.public_host, "demo.example.com")
        self.assertEqual(c.groq_model, "openai/gpt-oss-120b")
        self.assertEqual(c.port, 8001)
        self.assertEqual((c.plivo_auth_id, c.plivo_auth_token), ("i", "t"))

    def test_real_environment_beats_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            open(path, "w").write("GROQ_API_KEY=from_file\nDEEPGRAM_API_KEY=from_file\n")
            c = config.load({"GROQ_API_KEY": "from_env"}, env_file=path)
        self.assertEqual((c.groq_api_key, c.deepgram_api_key), ("from_env", "from_file"))

    def test_problems_list_what_is_missing(self):
        self.assertEqual(config.problems(config.load({**self.ENV, "ALLOWED_CALLERS": "+1 555 010 0100"}, env_file=None)), [])
        p = " ".join(config.problems(config.load({}, env_file=None)))
        for need in ("GROQ_API_KEY", "DEEPGRAM_API_KEY", "ALLOWED_CALLERS"):
            self.assertIn(need, p)

    def test_an_open_line_is_a_problem_and_a_bad_number_is_named(self):
        p = " ".join(config.problems(config.load({**self.ENV, "ALLOWED_CALLERS": "bogus"}, env_file=None)))
        self.assertIn("bogus", p)
        self.assertIn("anyone", p)

    def test_problems_never_contain_a_secret(self):
        p = " ".join(config.problems(config.load({**self.ENV, "GROQ_API_KEY": "SECRETVALUE", "ALLOWED_CALLERS": ""}, env_file=None)))
        self.assertNotIn("SECRETVALUE", p)

    def test_repr_hides_secrets(self):
        r = repr(config.load(self.ENV, env_file=None))
        for secret in ("'g'", "'d'", "'t'", "'i'"):
            self.assertNotIn(secret, r)


class SecurityTest(unittest.TestCase):
    def test_allow_list(self):
        self.assertTrue(security.caller_allowed("+1 555 010 0100", (ANALYST,)))
        self.assertTrue(security.caller_allowed(ANALYST_FROM_PLIVO, (ANALYST,)))
        self.assertTrue(security.caller_allowed("442079460958", ("+442079460958",)))
        self.assertFalse(security.caller_allowed(STRANGER_FROM_PLIVO, (ANALYST,)))
        self.assertFalse(security.caller_allowed("", (ANALYST,)))
        self.assertFalse(security.caller_allowed(ANALYST, ()))          # an empty list lets no one in

    def test_stream_token_lifecycle(self):
        t = security.make_stream_token("secret", now=1000)
        self.assertTrue(security.check_stream_token("secret", t, now=1060))
        self.assertFalse(security.check_stream_token("secret", t, now=1000 + security.TOKEN_SECONDS + 1))
        self.assertFalse(security.check_stream_token("other", t, now=1001))
        self.assertFalse(security.check_stream_token("secret", t + "x", now=1001))
        self.assertFalse(security.check_stream_token("secret", "", now=1001))
        self.assertFalse(security.check_stream_token("secret", None, now=1001))
        self.assertFalse(security.check_stream_token("secret", "no-dot", now=1001))

    def test_tokens_survive_travelling_in_plivos_extra_headers(self):
        token = security.make_stream_token("secret")
        got = plivo.parse_extra_headers(f"token={token}")["token"]
        self.assertTrue(security.check_stream_token("secret", got))

    def test_call_limiter(self):
        lim = security.CallLimiter(1)
        self.assertTrue(lim.acquire())
        self.assertFalse(lim.acquire())
        lim.release()
        self.assertTrue(lim.acquire())
        lim.release()
        lim.release()                                                   # extra releases never go below zero
        self.assertTrue(lim.acquire())
        self.assertFalse(lim.acquire())


@unittest.skipUnless(HAVE_SERVER, "install requirements.txt to run the server tests")
class ServerTest(unittest.TestCase):
    URL = "https://demo.example.com/plivo/answer"

    @classmethod
    def setUpClass(cls):
        t, _ = generate.build(seed=7)
        path = os.path.join(tempfile.mkdtemp(), "data.js")
        generate.write_dashboard_data(t, path)
        cls.D = analysis.load(path)

    def make(self, **env):
        base = {"GROQ_API_KEY": "g", "DEEPGRAM_API_KEY": "d", "ALLOWED_CALLERS": ANALYST,
                "PUBLIC_HOST": "demo.example.com", "PLIVO_AUTH_TOKEN": "tok"}
        base.update(env)
        self.calls = []

        async def fake_bot(websocket, call_data, cfg, analyst):
            self.calls.append(call_data.call_id)

        app = server.create_app(config.load(base, env_file=None), data=self.D, run_bot=fake_bot)
        return TestClient(app), app

    def post(self, client, params, token="tok", nonce="n1", url=URL):
        headers = {}
        if token:
            headers = {"X-Plivo-Signature-V3": plivo.signature_v3(token, "POST", url, params, nonce),
                       "X-Plivo-Signature-V3-Nonce": nonce}
        return client.post("/plivo/answer", data=params, headers=headers)

    # --- health ---------------------------------------------------------------------------------
    def test_health_is_ready_when_configured(self):
        c, _ = self.make()
        self.assertEqual(c.get("/health").json(), {"ok": True, "ready": True, "problems": []})

    def test_health_lists_problems_not_secrets(self):
        c, _ = self.make(GROQ_API_KEY="", ALLOWED_CALLERS="")
        j = c.get("/health").json()
        self.assertFalse(j["ready"])
        self.assertTrue(any("GROQ_API_KEY" in p for p in j["problems"]))

    # --- the answer webhook ---------------------------------------------------------------------
    def test_an_analyst_is_connected_to_a_stream(self):
        c, _ = self.make()
        r = self.post(c, {"From": ANALYST_FROM_PLIVO, "To": "15550100000", "CallUUID": "u1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("xml", r.headers["content-type"])
        self.assertIn('>wss://demo.example.com/plivo/stream</Stream>', r.text)
        self.assertIn('bidirectional="true"', r.text)
        self.assertIn('contentType="audio/x-mulaw;rate=8000"', r.text)
        self.assertIn('extraHeaders="token=', r.text)
        self.assertNotIn(ANALYST_FROM_PLIVO, r.text)                # the number is never passed along

    def test_the_stream_token_in_the_reply_is_valid(self):
        c, app = self.make()
        r = self.post(c, {"From": ANALYST_FROM_PLIVO, "CallUUID": "u1"})
        token = r.text.split('extraHeaders="token=')[1].split('"')[0]
        self.assertTrue(security.check_stream_token(app.state.secret, token))

    def test_a_stranger_is_turned_away(self):
        c, _ = self.make()
        r = self.post(c, {"From": STRANGER_FROM_PLIVO, "CallUUID": "u2"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("<Hangup/>", r.text)
        self.assertNotIn("<Stream", r.text)
        self.assertIn("private", r.text.lower())

    def test_a_forged_request_is_rejected(self):
        c, _ = self.make()
        self.assertEqual(self.post(c, {"From": ANALYST_FROM_PLIVO}, token="wrong").status_code, 403)
        self.assertEqual(c.post("/plivo/answer", data={"From": ANALYST_FROM_PLIVO}).status_code, 403)
        # signed for different fields than were sent
        sig = plivo.signature_v3("tok", "POST", self.URL, {"From": STRANGER_FROM_PLIVO}, "n1")
        r = c.post("/plivo/answer", data={"From": ANALYST_FROM_PLIVO},
                   headers={"X-Plivo-Signature-V3": sig, "X-Plivo-Signature-V3-Nonce": "n1"})
        self.assertEqual(r.status_code, 403)

    def test_without_an_auth_token_the_allow_list_still_applies(self):
        c, _ = self.make(PLIVO_AUTH_TOKEN="")
        self.assertIn("<Stream", c.post("/plivo/answer", data={"From": ANALYST_FROM_PLIVO}).text)
        self.assertIn("<Hangup/>", c.post("/plivo/answer", data={"From": STRANGER_FROM_PLIVO}).text)

    def test_host_comes_from_the_request_when_not_configured(self):
        c, _ = self.make(PUBLIC_HOST="", PLIVO_AUTH_TOKEN="")
        r = c.post("/plivo/answer", data={"From": ANALYST_FROM_PLIVO}, headers={"X-Forwarded-Host": "abc.trycloudflare.com"})
        self.assertIn('wss://abc.trycloudflare.com/plivo/stream', r.text)

    def test_odd_hosts_are_refused(self):
        c, _ = self.make(PUBLIC_HOST="", PLIVO_AUTH_TOKEN="")
        r = c.post("/plivo/answer", data={"From": ANALYST_FROM_PLIVO}, headers={"X-Forwarded-Host": 'a"b<c'})
        self.assertEqual(r.status_code, 400)

    # --- the audio stream -----------------------------------------------------------------------
    def stream(self, client, token, extra=True, first=None):
        start = {"event": "start", "sequenceNumber": 1,
                 "start": {"callId": "call-9", "streamId": "stream-9", "tracks": ["inbound"],
                           "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}}
        if token and extra:
            start["extra_headers"] = f"token={token}"
        with client.websocket_connect("/plivo/stream") as ws:
            ws.send_text(first if first is not None else json.dumps(start))
            try:
                ws.receive_text()
            except WebSocketDisconnect:
                pass

    def test_a_valid_token_starts_the_bot(self):
        c, app = self.make()
        self.stream(c, security.make_stream_token(app.state.secret))
        self.assertEqual(self.calls, ["call-9"])

    def test_missing_expired_or_forged_tokens_never_start_the_bot(self):
        c, app = self.make()
        self.stream(c, None)
        self.stream(c, "forged.token")
        self.stream(c, security.make_stream_token(app.state.secret, now=1))
        self.stream(c, security.make_stream_token("another secret"))
        self.stream(c, "x", first="this is not json at all")
        self.assertEqual(self.calls, [])

    def test_the_call_slot_is_freed_afterwards(self):
        c, app = self.make()
        for _ in range(3):
            self.stream(c, security.make_stream_token(app.state.secret))
        self.assertEqual(len(self.calls), 3)

    def test_only_one_call_at_a_time(self):
        c, app = self.make()
        app.state.limiter.acquire()                                 # someone is already on the line
        self.stream(c, security.make_stream_token(app.state.secret))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

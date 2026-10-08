"""The setup checker tells you what is missing before you dial. Tested with stand-ins for the internet."""
import unittest

from voice import brain, check, config

GOOD = {"GROQ_API_KEY": "SECRET1", "DEEPGRAM_API_KEY": "SECRET2", "ALLOWED_CALLERS": "+15550100100",
        "PUBLIC_HOST": "demo.example.com", "PLIVO_AUTH_TOKEN": "SECRET3", "PLIVO_AUTH_ID": "MAIDSECRET"}


def cfg(**over):
    return config.load({**GOOD, **over}, env_file=None)


def ok_post(url, key, body, timeout=30):
    return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def probe(code):
    return lambda key: code


def status(code):
    return lambda url, headers=None, timeout=10, body=None: code


class OfflineTest(unittest.TestCase):
    def test_good_settings_pass(self):
        rows = check.run(cfg())
        self.assertTrue(all(ok for _, ok, _ in rows), rows)

    def test_missing_settings_fail_and_say_which(self):
        rows = check.run(cfg(GROQ_API_KEY="", ALLOWED_CALLERS=""))
        failed = " ".join(d for _, ok, d in rows if not ok)
        self.assertIn("GROQ_API_KEY", failed)
        self.assertIn("ALLOWED_CALLERS", failed)

    def test_a_missing_plivo_token_is_a_warning_not_a_blocker(self):
        rows = check.run(cfg(PLIVO_AUTH_TOKEN=""))
        self.assertTrue(any("PLIVO_AUTH_TOKEN" in d for _, _, d in rows))
        self.assertTrue(all(ok for _, ok, _ in rows))

    def test_no_internet_is_used_unless_asked(self):
        def boom(*a, **k):
            raise AssertionError("went online")
        check.run(cfg(), live=False, http_get=boom, groq_post=boom)


MOD = {"MODULATE_API_KEY": "SECRET4"}


class ModulateCheckTest(unittest.TestCase):
    def rows(self, probe_fn, **over):
        return check.run(cfg(**{**MOD, **over}), live=True, http_get=status(200), groq_post=ok_post, modulate_probe=probe_fn)

    def test_an_accepted_key_passes(self):
        for code in (None, 1000):
            d = next(d for n, ok, d in self.rows(probe(code)) if n == "Modulate")
            self.assertIn("accepted", d)

    def test_a_rejected_key_says_so(self):
        ok, d = next((ok, d) for n, ok, d in self.rows(probe(4001)) if n == "Modulate")
        self.assertFalse(ok)
        self.assertIn("API key", d)

    def test_no_credits_is_reported(self):
        self.assertIn("credits", next(d for n, ok, d in self.rows(probe(4029)) if n == "Modulate"))

    def test_an_unreachable_service_is_reported(self):
        def down(key):
            raise OSError("unreachable")
        ok, d = next((ok, d) for n, ok, d in self.rows(down) if n == "Modulate")
        self.assertFalse(ok)
        self.assertIn("reach", d)

    def test_skipped_when_deepgram_is_the_listener(self):
        rows = self.rows(probe(4001), STT_PROVIDER="deepgram")
        self.assertNotIn("Modulate", [n for n, _, _ in rows])

    def test_never_probed_offline_and_the_key_is_never_shown(self):
        def boom(key):
            raise AssertionError("went online")
        check.run(cfg(**MOD), live=False, modulate_probe=boom)
        self.assertNotIn("SECRET4", str(self.rows(probe(4001))))


class DeepgramCheckTest(unittest.TestCase):
    def rows(self, code):
        seen = []

        def http(url, headers=None, timeout=10, body=None):
            seen.append((url, headers or {}, body))
            return code if "deepgram.com" in url else 200
        rows = check.run(cfg(), live=True, http_get=http, groq_post=ok_post, modulate_probe=probe(None))
        return rows, seen

    def row(self, code):
        return next((ok, d) for n, ok, d in self.rows(code)[0] if n == "Deepgram")

    def test_it_tests_speaking_not_project_listing(self):
        rows, seen = self.rows(200)
        url, headers, body = next(x for x in seen if "deepgram.com" in x[0])
        self.assertIn("/v1/speak", url)
        self.assertIn("aura-2", url)
        self.assertEqual(body, {"text": "OK"})                       # a couple of characters, not a real sentence
        self.assertTrue(headers["Authorization"].startswith("Token "))

    def test_a_working_key(self):
        ok, d = self.row(200)
        self.assertTrue(ok)
        self.assertIn("accepted", d)

    def test_a_wrong_key(self):
        ok, d = self.row(401)
        self.assertFalse(ok)
        self.assertIn("rejected", d)

    def test_a_key_without_permission_says_how_to_fix_it(self):
        ok, d = self.row(403)
        self.assertFalse(ok)
        self.assertIn("permission", d)
        self.assertIn("Member", d)

    def test_no_credit_and_rate_limits(self):
        self.assertIn("credit", self.row(402)[1])
        self.assertIn("limit", self.row(429)[1])

    def test_the_key_never_appears_in_the_output(self):
        for code in (200, 401, 403):
            self.assertNotIn("SECRET2", str(self.rows(code)[0]))


class VoiceCheckTest(unittest.TestCase):
    def rows(self, probe_fn, **over):
        return check.run(cfg(**{**MOD, **over}), live=True, http_get=status(200), groq_post=ok_post,
                         modulate_probe=probe(None), voice_probe=probe_fn)

    def test_the_fraud_voice_check_is_probed_when_enabled(self):
        d = next(d for n, ok, d in self.rows(probe(None)) if n == "Modulate voice check")
        self.assertIn("accepted", d)

    def test_a_key_without_access_to_the_detector_says_so(self):
        ok, d = next((ok, d) for n, ok, d in self.rows(probe(4004)) if n == "Modulate voice check")
        self.assertFalse(ok)
        self.assertIn("access", d)

    def test_skipped_when_switched_off(self):
        rows = self.rows(probe(4004), FRAUD_GUARD="off")
        self.assertNotIn("Modulate voice check", [n for n, _, _ in rows])

    def test_not_probed_offline(self):
        def boom(key):
            raise AssertionError("went online")
        check.run(cfg(**MOD), live=False, voice_probe=boom)


class PlivoCheckTest(unittest.TestCase):
    def rows(self, code, **over):
        seen = []

        def http(url, headers=None, timeout=10, body=None):
            seen.append((url, headers or {}))
            return 200 if "plivo.com" not in url else code
        rows = check.run(cfg(**over), live=True, http_get=http, groq_post=ok_post, modulate_probe=probe(None))
        return rows, seen

    def test_credentials_that_work(self):
        rows, seen = self.rows(200)
        self.assertIn("accepted", next(d for n, ok, d in rows if n == "Plivo"))
        url, headers = next((u, h) for u, h in seen if "plivo.com" in u)
        self.assertIn("MAIDSECRET", url)                                   # the account id is part of the address
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_rejected_credentials(self):
        ok, d = next((ok, d) for n, ok, d in self.rows(401)[0] if n == "Plivo")
        self.assertFalse(ok)
        self.assertIn("rejected", d)

    def test_skipped_without_credentials(self):
        rows, _ = self.rows(200, PLIVO_AUTH_TOKEN="", PLIVO_AUTH_ID="")
        self.assertNotIn("Plivo", [n for n, _, _ in rows])

    def test_the_token_is_never_shown(self):
        for code in (200, 401):
            self.assertNotIn("SECRET3", str(self.rows(code)[0]))


class LiveTest(unittest.TestCase):
    def test_everything_reachable(self):
        rows = check.run(cfg(), live=True, http_get=status(200), groq_post=ok_post)
        self.assertTrue(all(ok for _, ok, _ in rows), rows)
        self.assertEqual({n for n, _, _ in rows} >= {"Groq", "Deepgram", "Tunnel"}, True)

    def test_a_rejected_key_is_reported_plainly(self):
        rows = check.run(cfg(), live=True, http_get=status(401), groq_post=ok_post)
        d = next(d for n, ok, d in rows if n == "Deepgram")
        self.assertIn("rejected", d)

    def test_groq_rate_limit_is_reported(self):
        def limited(url, key, body, timeout=30):
            raise brain.BrainError("Groq returned 429: Groq's free-tier limit was hit")
        rows = check.run(cfg(), live=True, http_get=status(200), groq_post=limited)
        self.assertIn("429", next(d for n, ok, d in rows if n == "Groq"))

    def test_an_unreachable_tunnel_is_reported(self):
        rows = check.run(cfg(), live=True, http_get=status(530), groq_post=ok_post)
        ok, d = next((ok, d) for n, ok, d in rows if n == "Tunnel")
        self.assertFalse(ok)
        self.assertIn("tunnel", d.lower())

    def test_secrets_never_appear_in_the_output(self):
        for http in (status(200), status(401)):
            blob = str(check.run(cfg(), live=True, http_get=http, groq_post=ok_post))
            for secret in ("SECRET1", "SECRET2", "SECRET3"):
                self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()

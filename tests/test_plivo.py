"""Plivo specifics: checking that a request really came from Plivo, the XML we answer with, and the stream handshake.
The signature vectors are worked out by hand from Plivo's published V3 method, not by calling our own code."""
import asyncio
import base64
import hashlib
import hmac
import json
import unittest

from voice import plivo

URL = "https://demo.example.com/plivo/answer"
PARAMS = {"From": "14155550100", "To": "15550100000", "CallUUID": "abc-123"}


def sign(token, base, nonce):
    return base64.b64encode(hmac.new(token.encode(), f"{base}.{nonce}".encode(), hashlib.sha256).digest()).decode()


class SignatureTest(unittest.TestCase):
    def test_post_with_form_fields(self):
        # Plivo signs: URL + "?" + each field as name+value, sorted by name; then "." + nonce; HMAC-SHA256, base64
        base = URL + "?" + "CallUUIDabc-123" + "From14155550100" + "To15550100000"
        self.assertEqual(plivo.signature_v3("tok", "POST", URL, PARAMS, "n1"), sign("tok", base, "n1"))

    def test_post_with_no_fields_signs_the_bare_url(self):
        self.assertEqual(plivo.signature_v3("tok", "POST", URL, {}, "n1"), sign("tok", URL, "n1"))

    def test_post_with_a_query_string_adds_it_sorted_then_a_dot(self):
        base = "https://h.example.com/p" + "?" + "a=1&b=2" + "." + "FromX"
        self.assertEqual(plivo.signature_v3("tok", "POST", "https://h.example.com/p?b=2&a=1", {"From": "X"}, "n"),
                         sign("tok", base, "n"))

    def test_get_signs_url_and_sorted_query(self):
        base = "https://h.example.com/p?From=X&a=1"          # keys sort by character code: capitals first
        self.assertEqual(plivo.signature_v3("tok", "GET", "https://h.example.com/p?a=1", {"From": "X"}, "n"),
                         sign("tok", base, "n"))

    def test_valid_and_invalid(self):
        sig = plivo.signature_v3("tok", "POST", URL, PARAMS, "n1")
        self.assertTrue(plivo.valid_request("tok", "POST", URL, PARAMS, sig, "n1"))
        self.assertTrue(plivo.valid_request("tok", "POST", URL, PARAMS, "old," + sig, "n1"))      # several signatures allowed
        self.assertFalse(plivo.valid_request("tok", "POST", URL, {**PARAMS, "From": "19995550100"}, sig, "n1"))
        self.assertFalse(plivo.valid_request("tok", "POST", URL, PARAMS, sig, "other-nonce"))
        self.assertFalse(plivo.valid_request("wrong", "POST", URL, PARAMS, sig, "n1"))
        for missing in (None, ""):
            self.assertFalse(plivo.valid_request("tok", "POST", URL, PARAMS, missing, "n1"))
            self.assertFalse(plivo.valid_request("tok", "POST", URL, PARAMS, sig, missing))


class XmlTest(unittest.TestCase):
    def test_answer_opens_a_two_way_phone_quality_stream(self):
        xml = plivo.answer_xml("demo.example.com", "1.abc")
        self.assertIn('<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000" '
                      'extraHeaders="token=1.abc">wss://demo.example.com/plivo/stream</Stream>', xml)
        self.assertTrue(xml.startswith("<?xml"))
        self.assertIn("<Response>", xml)

    def test_special_characters_cannot_break_out_of_the_xml(self):
        xml = plivo.answer_xml('a"b<c&d', 'x"><evil/>')
        self.assertNotIn("<evil/>", xml)
        self.assertNotIn('a"b<c', xml)

    def test_private_line_message(self):
        xml = plivo.private_xml()
        self.assertIn("<Speak>", xml)
        self.assertIn("private", xml.lower())
        self.assertIn("<Hangup/>", xml)


class ExtraHeadersTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(plivo.parse_extra_headers("token=1700.abc;user=x"), {"token": "1700.abc", "user": "x"})
        self.assertEqual(plivo.parse_extra_headers("a=1,b=2"), {"a": "1", "b": "2"})

    def test_junk(self):
        for raw in (None, "", "no equals", ";;", 5, {"a": 1}):
            self.assertIsInstance(plivo.parse_extra_headers(raw), dict)
        self.assertEqual(plivo.parse_extra_headers("token=a=b"), {"token": "a=b"})        # only the first = splits


class FakeSocket:
    """Stands in for the websocket: hands back scripted text frames, then disconnects."""

    def __init__(self, *messages):
        self.messages = list(messages)

    async def receive_text(self):
        if not self.messages:
            raise plivo.Disconnected()
        m = self.messages.pop(0)
        if isinstance(m, Exception):
            raise m
        return m


START = {"event": "start", "sequenceNumber": 1,
         "start": {"callId": "call-1", "streamId": "stream-1", "accountId": "MAXXX", "tracks": ["inbound"],
                   "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}},
         "extra_headers": "token=1700.abc"}


class HandshakeTest(unittest.TestCase):
    def run_async(self, sock):
        return asyncio.run(plivo.read_handshake(sock))

    def test_reads_ids_and_the_token(self):
        h = self.run_async(FakeSocket(json.dumps(START)))
        self.assertEqual((h.call_id, h.stream_id, h.token), ("call-1", "stream-1", "1700.abc"))

    def test_skips_messages_before_the_start_event(self):
        h = self.run_async(FakeSocket(json.dumps({"event": "connected"}), "not json", json.dumps(START)))
        self.assertEqual(h.call_id, "call-1")

    def test_the_token_may_sit_inside_the_start_object(self):
        moved = {**START, "start": {**START["start"], "extra_headers": "token=zzz"}}
        del moved["extra_headers"]
        self.assertEqual(self.run_async(FakeSocket(json.dumps(moved))).token, "zzz")

    def test_no_token_gives_none(self):
        no_tok = {k: v for k, v in START.items() if k != "extra_headers"}
        self.assertIsNone(self.run_async(FakeSocket(json.dumps(no_tok))).token)

    def test_gives_up_without_a_start_event(self):
        with self.assertRaises(ValueError):
            self.run_async(FakeSocket(*[json.dumps({"event": "media"})] * 10))

    def test_a_binary_frame_before_start_is_skipped(self):
        h = self.run_async(FakeSocket(KeyError("text"), json.dumps(START)))
        self.assertEqual(h.call_id, "call-1")

    def test_a_hangup_before_start_is_an_error_not_a_hang(self):
        with self.assertRaises(ValueError):
            self.run_async(FakeSocket())

    def test_the_format_a_real_plivo_call_sends(self):
        self.assertEqual(plivo.parse_extra_headers("{X-PH-token: 123.abc}"), {"token": "123.abc"})

    def test_start_without_ids_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_async(FakeSocket(json.dumps({"event": "start", "start": {}})))


if __name__ == "__main__":
    unittest.main()

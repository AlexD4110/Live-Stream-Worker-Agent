"""Guards on the phone line: who may call, a short-lived pass for each audio stream, and one call at a time."""
import hashlib
import hmac
import threading
import time

from voice import config

TOKEN_SECONDS = 120


def caller_allowed(number, allowed):
    n = config.normalise_number(number, bare_e164=True)
    return bool(n) and n in allowed


def make_stream_token(secret, now=None):
    """A short-lived pass the webhook gives to the audio stream, so a stranger can't open the stream directly."""
    expiry = int(now if now is not None else time.time()) + TOKEN_SECONDS
    return f"{expiry}.{_mac(secret, expiry)}"


def check_stream_token(secret, token, now=None):
    try:
        expiry, mac = token.split(".")
        expiry = int(expiry)
    except (AttributeError, ValueError):
        return False
    if (now if now is not None else time.time()) > expiry:
        return False
    return hmac.compare_digest(_mac(secret, expiry), mac)


def _mac(secret, expiry):
    return hmac.new(secret.encode(), str(expiry).encode(), hashlib.sha256).hexdigest()[:32]


class CallLimiter:
    """Allows at most `limit` calls at once, which also protects the free-tier credits."""

    def __init__(self, limit=1):
        self.limit, self.active, self._lock = limit, 0, threading.Lock()

    def acquire(self):
        with self._lock:
            if self.active >= self.limit:
                return False
            self.active += 1
            return True

    def release(self):
        with self._lock:
            self.active = max(0, self.active - 1)

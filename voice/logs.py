"""Logging that records that a call happened, never what was said.

Pipecat's debug and info logs can include transcripts and the words being spoken, so only warnings and errors
from it are shown. Our own messages say only that a call started or ended.
"""
import logging
import re
import sys

from loguru import logger


MAX_CHARS = 200


def _scrub(record):
    """Pipecat warnings can quote the words being spoken in [brackets]. Hide those and keep messages short."""
    msg = re.sub(r"\[[^\]]*\]", "[hidden]", record["message"])
    record["message"] = msg[:MAX_CHARS] + ("..." if len(msg) > MAX_CHARS else "")
    return True


def configure(sink=None):
    logger.remove()
    logger.add(sink or sys.stderr, level="WARNING", format="{time:HH:mm:ss} {level} {message}", filter=_scrub)
    ours = logging.getLogger("voice")
    ours.setLevel(logging.INFO)
    if not ours.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        ours.addHandler(h)

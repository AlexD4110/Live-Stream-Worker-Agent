"""Settings, read from a .env file and the environment. Secrets are never printed or logged."""
import os
import re
from dataclasses import dataclass, field

from gifting import analysis

ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def normalise_number(raw, bare_e164=False):
    """A phone number as +E.164, or None. Ten digits are taken as US numbers.

    The phone company sends numbers as digits with the country code and no plus; pass bare_e164=True for those."""
    digits = re.sub(r"\D", "", raw or "")
    if (raw or "").strip().startswith("+"):
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if bare_e164 and 8 <= len(digits) <= 15:
        return "+" + digits
    return None


def _number(raw, default, low, high, cast):
    try:
        v = cast(raw)
    except (TypeError, ValueError):
        return default
    return v if low <= v <= high else default


def _host(raw):
    raw = re.sub(r"^\w+://", "", (raw or "").strip())
    return raw.split("/")[0]


@dataclass(frozen=True)
class Config:
    groq_api_key: str = field(default="", repr=False)
    groq_model: str = "openai/gpt-oss-120b"
    deepgram_api_key: str = field(default="", repr=False)
    modulate_api_key: str = field(default="", repr=False)
    tavily_api_key: str = field(default="", repr=False)
    stt_provider: str = "deepgram"
    fraud_guard: str = ""
    fraud_voice_check: bool = False
    synthetic_threshold: float = 0.9
    synthetic_windows: int = 2
    fraud_fail_closed: bool = False
    output_guard: bool = True
    stt_model: str = "nova-3"
    tts_voice: str = "aura-2-thalia-en"
    plivo_auth_id: str = field(default="", repr=False)
    plivo_auth_token: str = field(default="", repr=False)
    allowed_callers: tuple = ()
    bad_callers: tuple = ()
    public_host: str = ""
    port: int = 8001
    data_path: str = analysis.DATA_JS


def load(environ=None, env_file=ENV_FILE):
    values = {}
    if env_file and os.path.exists(env_file):
        with open(env_file) as f:
            values.update(parse_env(f.read()))
    values.update({k: v for k, v in (os.environ if environ is None else environ).items() if v != ""})
    good, bad = [], []
    for raw in re.split(r"[,;]", values.get("ALLOWED_CALLERS", "")):
        if raw.strip():
            n = normalise_number(raw)
            (good if n else bad).append(n or raw.strip())
    d = Config()
    guard = values.get("FRAUD_GUARD", "").strip().lower()
    modulate_key = values.get("MODULATE_API_KEY", "")
    chosen = values.get("STT_PROVIDER", "").strip().lower()
    provider = chosen if chosen in ("modulate", "deepgram") else ("modulate" if modulate_key else "deepgram")
    return Config(
        groq_api_key=values.get("GROQ_API_KEY", ""), groq_model=values.get("GROQ_MODEL", d.groq_model),
        deepgram_api_key=values.get("DEEPGRAM_API_KEY", ""), modulate_api_key=modulate_key, tavily_api_key=values.get("TAVILY_API_KEY", ""), stt_provider=provider,
        stt_model=values.get("DEEPGRAM_STT_MODEL", d.stt_model),
        tts_voice=values.get("DEEPGRAM_TTS_VOICE", d.tts_voice), plivo_auth_id=values.get("PLIVO_AUTH_ID", ""),
        plivo_auth_token=values.get("PLIVO_AUTH_TOKEN", ""), allowed_callers=tuple(good), bad_callers=tuple(bad),
        fraud_guard=guard, fraud_voice_check=bool(modulate_key) and guard != "off",
        synthetic_threshold=_number(values.get("FRAUD_SYNTHETIC_THRESHOLD"), d.synthetic_threshold, 0.01, 1.0, float),
        synthetic_windows=_number(values.get("FRAUD_SYNTHETIC_WINDOWS"), d.synthetic_windows, 1, 20, int),
        fraud_fail_closed=values.get("FRAUD_FAIL_CLOSED", "").strip().lower() in ("1", "true", "yes", "on"),
        output_guard=values.get("OUTPUT_GUARD", "").strip().lower() != "off",
        public_host=_host(values.get("PUBLIC_HOST", "")), port=int(values.get("PORT", d.port)),
    )


def problems(cfg):
    """What must be fixed before the phone line can work. Names settings, never shows their values."""
    out = []
    if not cfg.groq_api_key:
        out.append("GROQ_API_KEY is missing (free key: console.groq.com/keys).")
    if not cfg.deepgram_api_key:
        out.append("DEEPGRAM_API_KEY is missing (it speaks the replies; free credit: console.deepgram.com).")
    if cfg.stt_provider == "modulate" and not cfg.modulate_api_key:
        out.append("MODULATE_API_KEY is missing, but STT_PROVIDER is set to modulate.")
    if cfg.fraud_guard == "on" and not cfg.modulate_api_key:
        out.append("FRAUD_GUARD is on, but it needs MODULATE_API_KEY to check the caller's voice.")
    for b in cfg.bad_callers:
        out.append(f"ALLOWED_CALLERS has a number I can't read: '{b}'. Use a form like +15550100100.")
    if not cfg.allowed_callers:
        out.append("ALLOWED_CALLERS is empty. Set the analyst's phone number; otherwise the line is open to anyone who finds it.")
    return out

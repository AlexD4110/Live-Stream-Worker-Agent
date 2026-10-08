# Gifting Pulse

An interactive dashboard for the gifting side of a live-streaming platform, built on synthetic data.
It shows gifter health, creator health and trust-and-safety signals in one place, and a rules engine
turns the numbers into ranked findings with evidence, next steps and a guardrail.

Phase 2 adds a phone line: you call a number and an AI agent answers, gives a one-minute briefing, answers
questions, explains what the numbers mean and suggests next steps. See `docs/PHONE_SETUP.md`.

## Run it

Needs Python 3.10+ (no packages to install). Node is optional and only used by the tests.

```bash
python3 serve.py
```

It opens `http://localhost:8000`. The data is already generated. To regenerate it:

```bash
python3 -m gifting.generate
```

Run the tests (the dashboard tests need Node; the phone tests need `pip install -r requirements.txt`, and skip themselves if it's missing):

```bash
python3 -m unittest -v
```

## Talk to the agent (phase 2)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# keys go in .env (copy .env.example to .env if you don't have one)
python -m voice.chat          # try it by typing, no phone needed
python -m voice.talk          # talk to it with your voice in a browser, no phone needed (needs a Deepgram key)
python -m voice               # start the phone line (full steps: docs/PHONE_SETUP.md)
```

## Manual checkpoint

Open the dashboard. The first card under "What needs attention" must read exactly:

`Coin purchases are failing: Android US-East on app 34.2`

Set Platform to Android: the "Gift-send rate" tile shows `n/a`, and the "Why did gifting change?" panel
lists Gifters, Gifts per gifter and Coins per gift.

## What's in the data (all invented)

| Pattern | Where to see it |
|---|---|
| A bad Android release breaks app coin purchases in US-East for the last 7 days | First finding, segment table |
| A fraud ring: 14 new accounts on 3 shared devices, web-funded, about 43% chargebacks, gifting 3 new creators | Second finding, Trust and safety |
| Early-churn cliff: about 77% of new gifters never send a second gift | Retention panel |
| The top 1% of gifters send about 40% of coins; some have gone quiet | Top 1% tile, findings |
| A campaign whose headline lift (+55%) is well above its lift against holdout regions (+22%), followed by a dip | Campaign panel |
| Mid-tier creators stopping faster in the last 4 weeks | Creators panel |
| Flagged possible minors still gifting; prizes that failed, duplicated, or went to ring accounts | Trust and safety |

## Files

| Path | What it does |
|---|---|
| `gifting/generate.py` | Builds the synthetic data. Seeded, so the same every time. |
| `gifting/metrics.py` | Reference metric maths in Python. |
| `dashboard/metrics.js` | The same maths plus the findings rules, used by the page. |
| `dashboard/index.html` | The dashboard. |
| `dashboard/data.js`, `data/*.csv` | Generated data (the CSVs are the same data in a spreadsheet-friendly form). |
| `tests/` | Data patterns, metric maths, and a check that the JavaScript agrees with Python. |
| `serve.py` | Local web server for the dashboard. |
| `gifting/analysis.py` | The dashboard's maths and rules in Python, for the agent. Tests make it match the JavaScript word for word. |
| `voice/tools.py` | The only things the agent can look up. |
| `voice/briefing.py` | The spoken one-minute briefing (built by code, no AI). |
| `voice/brain.py` | The rules given to the AI model and the tool-calling loop. |
| `voice/pipeline.py` | One phone call as a pipeline: speech to text, model, text to speech. |
| `voice/server.py`, `voice/plivo.py`, `voice/security.py` | The web server Plivo calls, with the allow-list and the guards. |
| `voice/fraud.py`, `voice/input_guard.py` | Fraud rules and the guard that refuses social-engineering requests before the AI model sees them. |
| `voice/grounding.py`, `voice/compliance.py`, `voice/review.py`, `voice/output_guard.py` | The output guards: every number the AI says must trace to a tool result, and its wording must follow the rules. |
| `voice/modulate_svd.py` | Streams the caller's audio to Modulate to check for a cloned voice. |
| `voice/web.py` | Web search (Tavily) for outside background, labeled as external, with privacy and prompt-injection guards. |
| `voice/modulate_stt.py` | Hears the caller using Modulate's streaming speech-to-text. |
| `voice/chat.py`, `voice/talk.py`, `voice/check.py` | Type to the agent, talk to it in a browser, check your setup. |

The data is synthetic: no real users, creators or payments. Figures such as a roughly 50% platform take
are reported benchmarks in public sources, not official policy, and the dashboard does not state them.

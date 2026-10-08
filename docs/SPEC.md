# Gifting Pulse: spec

## Goal

Give a team clear insight into the gifting side of live streaming: who gifts, whether they stay, what
changed, why, and what to do next, with a small creator view and trust-and-safety guardrails.

Core rule: **code does the maths, the AI does the talking.** Every number comes from a deterministic
function. The voice agent (phase 2) explains results; it never calculates them.

## Phases

| Phase | What | Status |
|---|---|---|
| 1 | Synthetic data, metric functions, findings rules, interactive dashboard | Built |
| 2 | Voice agent: you call, it answers first, gives the briefing, diagnoses, recommends | Built; not yet tried on a real call (needs your keys) |
| 3 | Real-time guardrails on the call: fraud, hallucination, compliance | All three built (voice-behavior detection from Modulate still to add) |

## Phase 1: what exists

- **Data:** about 21,000 gifters, 300 creators and 85,000 gifts over 14 weeks (Jul 1 to Oct 6, 2026), with
  seven planted patterns (see README). Seeded generator; tests prove each pattern is present.
- **Metrics:** gifting coins, gifters, send rate, coins per gifter, 7- and 30-day return rate, top-1% share,
  chargeback rate, failed purchases, week-over-week change, campaign lift against holdout.
- **Diagnosis:** the change splits into viewers x send rate x gifts per gifter x coins per gift, and into
  platform x region segments. Nine rules produce findings, each with evidence, next steps and a guardrail.
- **Creator view:** tier counts, share earning, coins received, mid-tier churn by week.
- **Trust and safety:** device-sharing clusters, flagged minors, prize reconciliation.
- **Checked:** the JavaScript the page uses is tested against the Python reference.

## Phase 2: voice agent (built)

Decisions: the analyst calls in (inbound) and the agent speaks first; free tiers allowed; demo on your Mac; English only.

| Layer | Used | Why |
|---|---|---|
| Phone | Plivo (hackathon tool) | Streams call audio over a websocket; Pipecat has a built-in Plivo serializer. Replaced Twilio. |
| Public address | Cloudflare quick tunnel | Free, no account |
| Pipeline | Pipecat 1.12 (open source) | Handles phone audio, interruptions, and lets us add checks between stages |
| Hearing when you stop talking | Silero VAD | Open source, runs locally |
| Speech to text | Modulate Velma streaming (hackathon), Deepgram Nova-3 as fallback | Hackathon tool; Deepgram stays as the no-key fallback |
| Model | Groq, openai/gpt-oss-120b (set to think briefly) | Fast on the free tier; fallback openai/gpt-oss-20b. Replaced llama-3.3-70b-versatile, which Groq no longer offers. |
| Voice | Deepgram Aura-2 | Same key as above, fewer moving parts than a local voice |

Changed from the earlier plan: the voice is Deepgram Aura-2 instead of local Kokoro (one fewer thing to install); Kokoro or Piper
remain a later option if you want a fully offline voice.

How it works, in the order a call goes:

1. Plivo asks `/plivo/answer`. The server checks Plivo's signature and that the caller is on `ALLOWED_CALLERS`.
2. It replies with a short-lived pass and an instruction to open an audio stream. Only one call at a time.
3. The agent says a fixed greeting that states it is an AI.
4. Each thing you say becomes text, goes to the model along with ten tools, and the model calls the tool it needs.
5. The tool computes the figure from the same code the dashboard is tested against and returns it with a ready-to-say sentence.
6. Most tools return an exact, ready-to-speak sentence, and that sentence is spoken as it is, with no second model call (`tools.DIRECT`). This halves the tokens and delay, and nothing the model could get wrong sits between the number and the caller. Tools that need synthesis (findings, web search) go back to the model, which says it in at most two sentences.
7. (Unchanged)  "Brief me" reads a script built by code, so no number in it is model-made.

The ten tools: `get_metric`, `diagnose_change` (why it changed, where, and the top issue in one answer), `explain_change`, `get_segment_changes`, `get_findings`, `get_campaign_result`, `get_creator_health`,
`get_lapsed_gifters`, `search_web` (Tavily; outside background, always labeled external and unverified), plus `get_briefing`. No free-form SQL. If nothing fits, the agent says it can't answer.

Where phase 3 plugs in: `voice/pipeline.py` builds a list of stages. An input check goes between speech to text and the model;
a grounding and compliance check goes between the model and the voice. Every tool result already carries the exact figures
(and a `say` sentence), which is what a grounding check compares the spoken answer against.

## Phase 3a: fraud guards (built)

- **Social-engineering guard** (`voice/input_guard.py`, `voice/fraud.py`): between speech-to-text and the model. Requests for
  payouts, account or payment details, rule overrides and account actions get a fixed refusal and never reach the model; the
  third ends the call.
- **Cloned-voice guard** (`voice/modulate_svd.py`): streams the caller's audio to Modulate's synthetic voice detector. One
  confident synthetic window closes the sensitive tools; two end the call. Fails open by default, closed with `FRAUD_FAIL_CLOSED`.
- The agent has no tool that can pay, ban, refund or reveal an account, so a guard failure still cannot cause one.
- Not built: Velma behavior detection (`vishing`, `account_impersonation`, `coercion_manipulation`), pending a timing test with a real key.

## Phase 3b: output guards (built)

- **Hallucination guard** (`voice/grounding.py`, `voice/output_guard.py`): every number the model says must trace to a tool result from
  the same call, or the sentence is replaced. Tool lines spoken directly are exact by construction.
- **Compliance guard** (`voice/compliance.py`): promises, investment advice, pushing big spenders, verdicts, allegations as fact, and
  official-looking take rates are replaced with fixed lines.
- Both run in the call pipeline between the model and the voice, and in the typed chat. Switch off with `OUTPUT_GUARD=off` to troubleshoot.

## Phase 3: guardrails on the call (original plan)

Input guard (after speech to text): social-engineering and fraud attempts such as a caller claiming to be a
creator and asking for an early payout, and personal data masking. Grounding check: every number the agent
speaks must appear in a function result. Output guard (before text to speech): blocks promises, financial
advice and unapproved claims, adds the AI disclosure. Log metadata only, never call content.

The "do not overclaim" list for the agent: the roughly 50% platform take is a reported benchmark, not an
official rate; Utah and New Hampshire matters are allegations, not findings; never push heavy spenders to
spend more; escalate possible minors to humans.

## Open questions for phase 2

1. Do the hackathon rules allow hosted free tiers (Deepgram, Groq), or must everything run locally?
2. Do judges call the agent, or does it call them?
3. Demo machine and network, and whether the Plivo number is set up.
4. English only?

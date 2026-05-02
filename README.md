# AI Tutor

A personal AI research tutor built on **Anthropic Managed Agents** that delivers a daily ~1-hour learning session through Slack: curates readings from RSS / arXiv / HN / Reddit, asks one open-ended question per day, grades your reply against a hidden rubric, and tracks proficiency via **Leitner spaced repetition**.

The full product spec lives in [`Spec.MD`](./Spec.MD).

---

## Architecture

```
┌────────────────────┐        ┌──────────────────────────┐
│  GitHub Actions    │  cron  │   Python Orchestrator    │
│  (daily / missed)  ├───────▶│  runtime/orchestrator.py │
└────────────────────┘        │                          │
                              │  • creates session       │
┌────────────────────┐        │  • streams events        │
│  Slack Events API  │  HTTP  │  • executes Slack calls  │
│  (user replies)    ├───────▶│  • holds Slack token     │
└────────────────────┘        └──────────┬───────────────┘
                                         │ Anthropic SDK
                                         ▼
                              ┌──────────────────────────┐
                              │   Managed Agent Session  │
                              │   (ai-tutor.agent.yaml)  │
                              │                          │
                              │   ↕ memory store         │
                              │     concepts.json        │
                              │     profile.md           │
                              │     runs/YYYY-MM-DD.json │
                              │     lessons.md           │
                              │                          │
                              │   ↕ custom tool          │
                              │     post_slack_message   │
                              └──────────────────────────┘
```

**Key design choice — security boundary:** the Slack bot token never enters the agent sandbox. The agent declares `post_slack_message` as a custom tool; the orchestrator executes the actual Slack Web API call host-side and returns the result.

### Components

| Path | Role |
|------|------|
| `Spec.MD` | Product spec: learning model, scoring, Slack UX. |
| `ai-tutor.agent.yaml` | Agent definition: model, system prompt, mode dispatch, custom tool schema. |
| `ai-tutor.environment.yaml` | Cloud agent environment with unrestricted egress. |
| `runtime/orchestrator.py` | Session driver, Slack webhook server, identity gate. |
| `seed_concepts.json` | Initial concept list (20 AI fundamentals). |
| `seed_feeds.yaml` | Curated RSS / arXiv / buzz sources. |
| `setup.sh` | One-time provisioning via the `ant` CLI. |
| `Dockerfile` / `fly.toml` | Webhook deploy on Fly.io. |
| `.github/workflows/daily.yml` | Cron: missed-check (5:50 AM PT) + daily-run (6:00 AM PT). |

---

## How it works

### Daily run (cron-driven)

1. GitHub Actions cron fires at 6:00 AM PT.
2. Orchestrator runs `python runtime/orchestrator.py daily --kickoff "DAILY RUN"`.
3. A new managed-agent session is created with the memory store mounted.
4. The agent:
   - Loads shaky / due concepts and the want-to-learn list.
   - Scans curated feeds → arXiv → buzz sources.
   - Scores candidates (`0.5·relevance + 0.3·difficulty_fit + 0.2·quality_prior`) and picks ~45 minutes of reading.
   - Picks one core concept to test, generates the question **and** a hidden rubric together.
   - Writes `runs/YYYY-MM-DD.json` to the memory store.
   - Calls `post_slack_message` once (framing → reading list → question last).
5. Session ends.

### Reply (webhook-driven)

1. You reply in the Slack thread.
2. Slack posts to `/slack/events`.
3. Orchestrator verifies the Slack signature, dedups by `event_id`, and **drops anything not from `ALLOWED_SLACK_USER_ID`**.
4. `_classify_reply` heuristically routes to `essay` / `confirm` / `teaching_score` / `grade`.
5. The webhook returns 200 immediately; a background worker spawns a new session with the appropriate kickoff (e.g. `GRADE: <text>`).
6. The agent loads today's run record, judges against the rubric, posts grade + revealed rubric, and asks for a 0–5 teaching score.
7. On the teaching-score reply, the agent updates `concepts.json` (Leitner box, proficiency, shaky flag), `agent_score.md`, and `lessons.md`.

### Missed day

A separate cron at 5:50 AM PT runs `orchestrator.py missed <yesterday>`. The agent silently marks the tested concept shaky and demotes its Leitner box.

---

## Setup

### 1. Prereqs

- Python 3.12+
- [`ant` CLI](https://github.com/anthropics/anthropic-cli)
- `jq`
- A Slack app with a bot user (scopes: `chat:write`, `im:write`)
- `ANTHROPIC_API_KEY` exported

### 2. Provision agent + environment + memory store

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./setup.sh
```

This creates the environment, agent, and memory store; seeds `feeds.yaml` and `concepts.json`; and writes `ai-tutor.env` with the resulting IDs.

### 3. Configure runtime

```bash
cd runtime
cp .env.example .env
source ../ai-tutor.env   # populates AGENT_ID, ENV_ID, MEMORY_STORE_ID
# then fill in Slack values + ANTHROPIC_API_KEY in runtime/.env
```

Required Slack values:
- `SLACK_BOT_TOKEN` — `xoxb-...`
- `SLACK_SIGNING_SECRET`
- `ALLOWED_SLACK_USER_ID` — your `U…` member ID (everyone else is dropped)
- `DEFAULT_DM_CHANNEL` — `D…` channel ID for your bot DM

### 4. Onboard

Send yourself the onboarding essay request:

```bash
python runtime/orchestrator.py onboard
```

Reply in Slack with a 200–800 word essay on your AI background. The agent will propose a concept tree; reply `confirm` to commit it.

---

## Running

### Webhook server (handles Slack replies)

Local:

```bash
python runtime/orchestrator.py serve --port 8080
```

Production (Fly.io):

```bash
fly launch        # first time
fly secrets set ANTHROPIC_API_KEY=... AGENT_ID=... ENV_ID=... \
                MEMORY_STORE_ID=... SLACK_BOT_TOKEN=... \
                SLACK_SIGNING_SECRET=... ALLOWED_SLACK_USER_ID=... \
                DEFAULT_DM_CHANNEL=...
fly deploy
```

Point your Slack app's Events URL at `https://<your-app>.fly.dev/slack/events`.

### Daily / missed cron

GitHub Actions runs both automatically (`.github/workflows/daily.yml`). Add the same env vars as repo secrets.

Manual trigger:

```bash
python runtime/orchestrator.py daily --kickoff "DAILY RUN"
python runtime/orchestrator.py daily --kickoff "MAINTENANCE"   # 30-day pruning
python runtime/orchestrator.py missed 2026-04-30
```

---

## Memory layout

All durable state lives in the managed memory store under `/mnt/memory/ai-tutor/`:

```
concepts.json          # source of truth for tracked concepts
profile.md             # parsed onboarding essay + want-to-learn list
feeds.yaml             # curated sources
runs/YYYY-MM-DD.json   # daily run records (readings, question, rubric, grade)
agent_score.md         # rolling teaching feedback
lessons.md             # things the tutor has learned about teaching you
```

`concepts.json` schema (see `ai-tutor.agent.yaml`):

```json
{
  "name": "Self-attention",
  "proficiency": 0,
  "leitner_box": 1,
  "last_tested": null,
  "last_grade": null,
  "shaky": false,
  "source_refs": [],
  "notes": "..."
}
```

---

## Spaced repetition

Five Leitner boxes with intervals **1, 3, 7, 14, 30** days.

| Grade | Effect |
|-------|--------|
| ≥ 4 | Promote one box; clear shaky; `proficiency += 1` |
| = 3 | Stay; clear shaky |
| ≤ 2 | Demote to box 1; mark shaky; `proficiency -= 1` |

Shaky concepts re-surface within 24–48 h regardless of box. No reply within 24 h ⇒ tested concept becomes shaky.

---

## Security

- HMAC verification on every Slack webhook (5-minute replay window).
- Hard I/O filter — only messages from `ALLOWED_SLACK_USER_ID` are processed; everything else is logged and dropped.
- `event_id` dedup to absorb Slack retries.
- Slack bot token lives only in the orchestrator's env; the agent sandbox can request a Slack post but never sees credentials.
- `ai-tutor.env` and `runtime/.env` are gitignored.

---

## License

Personal project — no license granted.

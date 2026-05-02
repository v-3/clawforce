# AI Tutor

## Goal

The AI tutor keeps me current on AI research and discussion by curating a daily
one-hour learning session delivered through Slack.

## Success Criteria In 3 Months

- I can hold a substantive technical conversation about at least 20 recent AI concepts I did not know before.
- My proficiency on tracked concepts trends upward with measurable revision cadence.
- I have completed 80 sessions.
- My overall rating for the tutor is at least 4.5 out of 5.

## Learning Model

- Maintain a list of concepts. Start with a small seed list, then grow it over time.
- When the tutor encounters an important concept that does not fit the existing model, add it as a new concept.
- Every 30 days, the AI tutor revisits the list and prunes unnecessary concepts.

## Concept Fields

For each concept, store:

- `name`: string; human-readable concept name.
- `proficiency`: integer; 0 = unseen, 1 = introduced, 2 = shaky, 3 = solid, 4 = strong, 5 = teaching-level.
- `leitner_box`: integer from 1 to 5; maps to the surfacing interval.
- `last_tested`: datetime; the last time this concept was the focus of a daily question.
- `last_grade`: integer from 0 to 5; the grade from the most recent test.
- `shaky`: boolean; true if the last grade was 2 or lower, forcing earlier resurfacing.
- `source_refs`: list of URLs where I learned the concept.
- `notes`: string; agent-generated summary of what "knowing this" means at my current level.

## Storage

Store concepts in a text file in the memory store, preferably `concepts.json`.
Keep it easy for the agent to inspect and rewrite directly; avoid SQLite or
other binary/stateful formats for the core learning model.

## Onboarding

Onboarding is a one-time process before daily runs begin.

The agent posts a Slack DM asking me to write a free-form 200-800 word essay on
my AI background: what I have built, what I have read, what I have struggled
with, and what I want to be good at.

The agent parses the essay with an LLM and proposes:

- An initial concept tree skeleton with depth 1-2.
- Initial proficiency estimates for concepts I mentioned.
- A "want to learn" list for concepts I gestured at but do not yet know.

I review and edit the tree in Slack via a structured message. The agent confirms
the final tree and schedules the first daily run.

## Daily Run Pipeline

Daily runs are triggered at a fixed time, configurable with a default of 6:00 AM
Pacific.

The tutor scans three source classes in this order:

- Curated feeds, highest trust: blogs, Substacks, and newsletters I have added. Use RSS where available and HTML scraping otherwise. Scan the last 24-48 hours of new posts.
- Targeted arXiv: configured categories, defaulting to `cs.LG`, `cs.CL`, and `cs.AI`. Scan the last 24-48 hours and filter by simple LLM relevance against my "want to learn" list and weak concepts.
- Buzz scan: Twitter/X, Hacker News, and `/r/MachineLearning`. Look for posts or threads with high engagement and a substantive linked artifact, such as a paper, blog post, or repository. The buzz scan should catch important items the curated list missed, not feed timeline-brain.

The output is a candidate pool of 10-30 items with metadata: source, link,
title, abstract or excerpt, engagement signal, and detected concepts.

## Scoring And Selection

- Relevance, 0-1: overlap between detected concepts and my weak concepts plus "want to learn" list.
- Difficulty fit, 0-1: whether this is one step beyond my current proficiency on its core concept. Too easy is boring; too hard bounces off.
- Quality prior, 0-1: based on source trust score. Curated feeds start at 0.9, buzz starts at 0.5, and both are adjusted by my historical ratings.

The agent computes a composite score and picks 1-3 items totaling about 45
minutes of estimated reading time, using word count / 200 wpm plus a per-source
modifier for paper density.

Selected content should enable learning of a core concept.

## Question Generation

The agent picks one core concept from the day's readings to test.

It generates:

- The question, shown to me: open-ended, about 10-15 minutes of thinking and writing. It tests understanding and stretches one step beyond what is stated in the readings, such as applying the idea to a case the paper did not discuss, relaxing an assumption, or comparing the approach to a prior reading.
- A hidden rubric, not shown until after I answer: 3-5 bullet points naming what a strong answer covers. Store it in the run record.

The rubric must be authored by the question generator at the same time as the
question, not by the grader after the fact. The grader needs a fixed standard so
it grades against intended depth rather than the answer's vibe.

## Slack Delivery

The agent sends one message in a new thread at the scheduled time.

The question is the last thing in the message.

When I reply in the thread:

- The agent grades my answer with an LLM judge using the hidden rubric. Output: integer 0-5 plus 1-2 lines of feedback.
- The agent posts the grade, the feedback, and the revealed rubric.
- The agent asks for final feedback on its teaching on a scale of 0-5.
- The agent does not elaborate further or answer follow-up questions in that grading response.

## State Updates

For the tested concept:

- Update `last_tested`, `last_grade`, and proficiency.
- Add new concept ideas surfaced by the session.

Update the overall agent score on a 0-5 scale.

On a missed day or low-effort reply:

- Mark the tested concept as shaky and demote it one box.

If I do not reply within 24 hours, the tested concept enters the shaky state.

## Spaced Repetition (Leitner)

Use five boxes with intervals of 1, 3, 7, 14, and 30 days.

- New concepts enter at box 1.
- Pass, grade >= 4: promote one box.
- Hold, grade = 3: stay in the current box.
- Fail, grade <= 2: demote to box 1 and mark shaky.
- Shaky concepts surface within 24-48 hours regardless of box.

Daily question selection priority:

1. Any shaky concept due today.
2. The highest-priority concept due today by box interval.
3. If nothing is due, the weakest concept by lowest proficiency and oldest `last_tested`.

## General Requirements

- Try new teaching approaches when needed to improve learning outcomes.
- Run as a Claude managed agent deployed in the cloud.
- Allow Claude model selection.
- Keep all communication in Slack.
- Apply I/O filtering so messages are accepted only from a fixed Slack user.
- Update instructions and teaching strategies as the tutor learns what works.

# LinkedIn Growth Engine setup

This repository imports the 12-skill bundle from [sergebulaev/linkedin-skills](https://github.com/sergebulaev/linkedin-skills) at commit `bfa41ff5d2b137f6023f88d9c9fda68d75b4ed5f`. The original MIT license and attribution are retained.

## Install and validate

Run from this repository's root with Python 3.10 or newer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r automation/requirements.txt 'PyYAML==6.0.3'
.venv/bin/python -m pip check
.venv/bin/python scripts/check_frontmatter.py
.venv/bin/python scripts/check_markdown_references.py
.venv/bin/python scripts/check_no_secrets.py
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/check_config.py --offline
```

For Codex cloud, use the existing checkout; tasks are already isolated, so do not create Git worktrees unless requested. A saved cloud environment draft must be published in environment settings to apply to future cloud sessions. Publishing that draft is separate from activating the persistent GitHub workflow.

## Persistent automation

The [GitHub Actions workflow](../.github/workflows/digital-twin.yml) runs every five minutes, subject to GitHub's scheduling delays. It remains independent of an open cloud session. The requested daily slots are 09:00 and 18:00 IST, with a five-minute publishing lead; an on-time run schedules approximately 09:05 and 18:05.

The [30-day growth plan](30-DAY-GROWTH-PLAN.md) and [machine-readable calendar](../automation/content-plan.json) contain 60 angles for October 10–November 8, 2026. Public engagement aims for 30–40 total daily likes/comments, capped at 20 meaningful comments and 20 likes, paced between 08:00 and 22:00 IST. Replies use a separate durable queue and quota. See [DIGITAL-TWIN.md](DIGITAL-TWIN.md) for exact limits, monitoring delays, model routing, activity receipts, and pause controls.

Routine posting and contextual public interaction use the owner's standing authorization in [policy.json](../automation/policy.json). The separately invoked interactive skills retain their original draft flow. The original October 9, 13, and 14 morning seed posts are already recorded and must not be resubmitted.

## Connected accounts and cost controls

The active connections require `PUBLORA_API_KEY`, `APIFY_TOKEN`, and the correct `LINKEDIN_PLATFORM_ID`. The existing Publora account was verified as Pro with API publishing and analytics enabled, 5,000 monthly posts per connection, and unrestricted queue/horizon fields. Capacity is checked again before scheduling rather than assumed from that observation.

Apify supplies public reads and the private GPT-4.1-mini writing bridge. Its model and read calls share the existing $5 monthly Free-plan credit pool. `APIFY_MONTHLY_BUDGET_USD` defaults to **4**; the lower of that guard and the account limit applies. Unknown usage pauses paid calls. This pre-call check does not change provider billing or guarantee a hard invoice cap. The workflow does not enable pay-as-you-go, top-ups, or a higher budget.

`TWIN_LOW_COST_MODE` defaults to **true**. Posts and weekly analysis use the private Apify route, while public comments and replies use a verified local Qwen3 4B CPU model. Posts can use that local model when the paid route is blocked by the budget guard. The workflow caches and starts its own local model; for a cloud-local preview, run [start_local_model.sh](../automation/start_local_model.sh).

The owner's total budget target is $5–7 per month. Publora's published one-account Pro price is $5.99 monthly; the connection is verified as Pro, while the actual invoice has not been inspected. Apify's $4 guard stays within the existing $5 free credit pool. No additional paid DM service is activated. `MODEL_API_KEY` is optional for direct OpenAI with separate billing; it is not needed by the current hybrid setup. `PIXFARO_TOKEN` is optional for images and is not required for the text calendar.

Cloud environment bindings and GitHub Actions secrets are separate. Working credentials have been verified in a hosted live run; repository secrets/settings inspection is denied with HTTP 403. For replacement credentials, use secure cloud settings and [GitHub Actions secrets](https://github.com/designwithrupesh/linkedingrowthengine/settings/secrets/actions) as appropriate. Never commit keys or copy their values through workflow inputs. An ignored local `.env` may be used for local development.

## Replies, inbox, and profile

Own-post monitoring rotates across up to 60 verified published posts, batches up to five, and follows available pagination. Publora comment-count signals reduce paid Apify reads: changed counts or a daily reconciliation trigger a read, with a six-hour fallback when analytics cannot be verified. Publora's two-hour analytics cache and LinkedIn's possible 24-hour reporting lag mean this is not instant comment delivery. Eligible detected comments stay in a durable queue for contextual replies.

The active Publora connection does not expose LinkedIn DMs or profile editing. An optional Unipile adapter is prepared but disconnected because the owner rejected its cost. `dm_replies_enabled` and `profile_edits_enabled` remain false. No automatic DM/profile integration within the stated budget has been verified. [PROFILE-DRAFT.md](PROFILE-DRAFT.md) supplies ready headline/About copy, with the exact payload in [profile-update.json](../automation/profile-update.json); preparing that payload does not edit the profile.

## Skill context and private material

Read root `SKILL.md` and the relevant `skills/<name>/SKILL.md` when using the skills interactively. A checkout alone does not guarantee plugin activation. The upstream README describes installation.

The interviewer can collect optional true stories; the profile optimizer and planner align the position and calendar. The post writer, repurposer, and humanizer prepare content; the comment drafter, reply handler, thread monitor, and engager analytics support interaction. Hook extraction studies relevant examples. Employee advocacy applies only to an actual participating team, which has not been supplied.

Keep confidential stories, private voice samples, draft queues, and reports in ignored local storage such as `testing/`. Public action receipts remain in [state.json](../automation/state.json). Ambiguous writes, including the historical uncertain reaction, stay quarantined until verified evidence resolves them. Independent work can continue; uncertain intents must not be cleared simply to retry.

Use timezone-aware timestamps for any explicit schedule. `scripts/schedule_post.py` defaults to today's 10:00 in the host timezone or five minutes ahead; it is not the runner's daily calendar.

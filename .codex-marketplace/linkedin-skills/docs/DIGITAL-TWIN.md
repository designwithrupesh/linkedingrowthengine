# LinkedIn digital twin

This application connects the imported 12-skill bundle to an autonomous GitHub Actions workflow. Rupesh has authorized routine design posts and contextual public interactions on his behalf. Live mode uses that standing authorization in [policy.json](../automation/policy.json); it does not ask for approval on each post or comment. The imported skills retain their original interactive workflow when used separately.

## Writing checks

All outgoing writing uses [the owner writing rules](../references/voice-rules.md) and the shared gate in [lib/voice.py](../lib/voice.py). Posts, comments, replies, private messages, profile text, and reports reject em dashes, en dashes, double hyphens, generic praise, named stock phrases, jargon, and writing scaffolding. The rules are included in the actual generation and editing prompts. Each draft gets at most one source-only edit; a second failure skips the action before intent or quota allocation.

Short answers are allowed, and posts use paragraphs only where the idea needs them. There is no fixed hook-and-lesson layout or mandatory closing question. These checks enforce writing preferences and do not prove authorship.

The four scheduled posts for October 10, 13, and 14 were rewritten as specific plain prose and verified remotely, preserving their IDs and publishing times. Already acknowledged replies retain their receipts. Reply reads use the actor's most-relevant view and evidenced activity URLs, because the most-recent share view can omit nested replies. Parent identity comes from the actual source comment URL when available.

## Publishing and engagement

The runner checks for work every five minutes, every day. GitHub may delay scheduled jobs. Publishing slots are **09:00 and 18:00 IST**, including weekends; a due slot is scheduled through Publora five minutes ahead, so an on-time session publishes at approximately **09:05 or 18:05**. A delayed session keeps the slot's identity and does not create a duplicate. Missed slots are not moved to another day.

The [30-day calendar](30-DAY-GROWTH-PLAN.md) covers October 10 to November 8, 2026, with 60 distinct angles. The runner selects its slot from [content-plan.json](../automation/content-plan.json). Outside that campaign it continues with the policy's topic rotation. The goal is consulting conversations, personal-brand authority, and founding designer relationships. LinkedIn's editorial Top Voice selection is outside the application's control; there is no 30-day badge guarantee.

Public engagement aims for **30–40 total actions per day**, with ceilings of **20 substantive comments and 20 likes**, spread across the **five-hour daily window from 17:00 to 22:00 IST**. Each fifteen-minute slot allows at most **one like and one substantive comment**. The five-minute workflow checks share that slot's durable allowance; missed slots expire instead of building up a burst later. The runner reads the actual post, skips unrelated or weak matches, and contributes a design example, a decision criterion, or a specific question. It does not fill a quota with generic praise. Relevant public source posts must be no more than seven days old; profile discovery is cached for 24 hours.

This schedules useful engagement throughout five hours. It does not simulate a person being online continuously or establish five hours of human work. GitHub scheduling delays, available relevant posts, and the existing read budget can reduce activity. Replies have their own queue and continue outside the public engagement window.

Replies on Rupesh's own posts have a separate durable queue and are handled before outbound public engagement. The limits are 20 replies per run and 1,000 per day. These are operational ceilings, not a promise that each run will reach them. Personal facts, commitments, off-topic content, and generation failures remain exceptions.

## Comment monitoring and reply timing

The monitor rotates through up to **60 verified published posts**, checking batches of at most five. It preserves pending comments, follows available pagination, keeps the top-level parent and nested thread context, and prevents duplicate replies. A count alone is not treated as a comment feed.

Publora Pro's `COMMENT` statistic provides a low-cost signal for when a fresh Apify comment read is useful. Paid reads are triggered by a changed count or a 24-hour reconciliation. When the statistic cannot be verified, a bounded six-hour fallback is used. Failed reads back off without acknowledging an unseen change.

The five-minute workflow does **not** mean instant replies. Publora documents a two-hour analytics cache, and LinkedIn analytics can lag by up to 24 hours. Batch rotation, actor execution, and GitHub scheduling add further delay. The queue processes eligible comments once they are available; there is no verified real-time comment-event connection.

## Models and the monthly budget

The default low-cost mode uses two model routes:

| Work | Model route |
|---|---|
| Posts and weekly audience analysis | Private Apify bridge to `openai/gpt-4.1-mini`, using the existing Apify credit pool |
| Comments and public replies | Verified Qwen3 4B Instruct 2507 on the GitHub runner's CPU |
| Posts when Apify's budget guard blocks paid inference | The local CPU model, with the same quality checks |

The private `linkedin-growth-ai` actor is pinned in [ai-provider.json](../automation/ai-provider.json); its source is in [apify_ai_bridge](../automation/apify_ai_bridge/). It calls Apify's official OpenRouter gateway from inside the actor. No separate OpenAI account or key is required. It accepts two text messages, at most 24,000 input characters and 900 output tokens, with no tools or browsing. Writing and reading share Apify's existing $5 monthly Free-plan credit pool, subject to the account's actual limits.

`APIFY_MONTHLY_BUDGET_USD` defaults to **4**. Before a paid read or model call, the runner checks actual account usage against the lower of that guard and the account's own cap. Unknown usage pauses paid work. The guard is a pre-call check rather than a provider-enforced billing ceiling; a call already started may add usage. No top-up, pay-as-you-go change, or higher budget is enabled by the workflow. The Free-plan AI gateway has a higher token rate than paid Apify plans.

The local model uses a 2.497 GB Unsloth Q4_K_M conversion and pinned llama.cpp runtime, both verified against publisher SHA-256 digests. GitHub caches the model, and its server listens only on `127.0.0.1`. Local generation avoids per-interaction model API charges. Standard hosted Actions execution for a public repository is normally free, subject to GitHub's account rules and quotas.

Rupesh's total monthly budget target is **$5–7**. The connected account is verified as Publora Pro; its published one-account monthly price is **$5.99**. Apify's $4 usage guard remains within the existing $5 free credit pool, and no additional paid DM service is activated. The actual Publora invoice was not inspected. The workflow makes no new subscription purchase or billing change.

An optional `MODEL_API_KEY` selects direct OpenAI instead, with separate billing; it is not required by this setup. `MODEL_ENDPOINT` and `MODEL_NAME` can select an optional custom model endpoint. `TWIN_LOW_COST_MODE` defaults to `true`; keep it enabled for the local interaction route.

Each draft passes action-specific format and length checks. One source-only humanizer editing pass is allowed; it cannot introduce new numeric claims. Failed output is skipped without padding or publishing. Preview fails when it cannot produce a usable post.

## Verified account capacity and retained posts

The connected Publora account returned a successful account-context response for **`pro_monthly_v2`**, with API access, publishing, and analytics enabled. The check showed **5,000 monthly posts per connection**, **4,997 remaining**, and unrestricted scheduling queue and horizon fields. The runner checks current capacity before scheduling; these values are an observed account state.

The original seed batch was accepted and read back from Publora. Its first post, scheduled for October 9, 2026 at 09:00 IST, was verified published as [urn:li:share:7514165310896250880](https://www.linkedin.com/feed/update/urn:li:share:7514165310896250880/). The October 13 and 14 morning seeds remain recorded with their remote IDs. Do not submit them again. A retained morning seed reserves that date's morning slot; the evening slot remains independent.

Apify and Publora credentials have already been confirmed by a real hosted GitHub Actions live run. This does not establish that every later code revision has passed a hosted run. The expanded cadence, model split, and monitor require verification from their actual workflow version. Cloud credentials and GitHub Actions secrets are separate, and repository secret/settings inspection is denied with HTTP 403. Runtime checks supply the evidence; credentials are never copied into Git or workflow inputs.

## DMs and profile updates

The active Publora APIs do not provide LinkedIn inbox or profile editing. An optional Unipile adapter has been prepared, but Rupesh declined its cost. `dm_replies_enabled` and `profile_edits_enabled` are **false**, with no connected optional-provider account established. No verified automatic DM/profile route within the $5–7 budget has been found. The active workflow therefore does not send DMs, issue connection invitations, or edit the profile.

[PROFILE-DRAFT.md](PROFILE-DRAFT.md) contains a ready headline and About section, also exported exactly in [profile-update.json](../automation/profile-update.json). It uses the confirmed background without inventing clients, career dates, or results. Preparing a payload is not evidence of a profile edit. If an authenticated supported route becomes available later, the prepared adapter is limited to a one-time headline/About update and contextual inbound replies; other sections and unsolicited outreach are outside that adapter's scope.

## Activity records and controls

[state.json](../automation/state.json) records public generated content, timestamps, remote IDs, publication dates, quotas, pending replies, coverage information, and reports. These records are public when this repository is public. Keep confidential career notes, keys, and private sample material in ignored local storage such as `testing/`.

Before every remote write, the runner checkpoints its intent to Git. Definitively rejected requests retain their status and are not retried. Ambiguous writes are quarantined until verified evidence resolves them; they are never cleared merely to repeat a request. The historical uncertain reaction remains quarantined, while independent intents can continue. Canonical share or UGC identifiers stay distinct from public activity URLs.

The [GitHub workflow](../.github/workflows/digital-twin.yml) runs after a cloud session closes. Set repository variable **`TWIN_LIVE=false`** to pause future live actions. Set **`TWIN_READ_ENABLED=false`** to pause reading and interactions while retaining posts. Already scheduled Publora posts require separate cancellation; pausing the runner does not remove them.

For a replacement or revoked credential, store `PUBLORA_API_KEY` and `APIFY_TOKEN` securely in [GitHub Actions secrets](https://github.com/designwithrupesh/linkedingrowthengine/settings/secrets/actions). The profile connection is set by `LINKEDIN_PLATFORM_ID` or the policy. A replacement Apify account needs its own private bridge deployment and pinned metadata. GitHub Actions must be allowed to write repository contents; a blocked state checkpoint stops the corresponding remote write.

## All 12 imported skills

| Skill | Role in the running system |
|---|---|
| `linkedin-interviewer` | Preserve optional real career material supplied by Rupesh; do not invent stories. |
| `linkedin-profile-optimizer` | Prepare truthful profile copy; automatic editing remains inactive. |
| `linkedin-content-planner` | Define the 60 angles, requested cadence, and weekly review. |
| `linkedin-post-writer` | Draft the selected post angle. |
| `linkedin-humanizer` | Review style proportionally while preserving facts. |
| `linkedin-hook-extractor` | Study source-post structure without copying its claims or story. |
| `linkedin-repurposer` | Reuse real supplied material; richer source-note work waits for actual notes. |
| `linkedin-comment-drafter` | Add contextual design contributions to public posts. |
| `linkedin-reply-handler` | Respond in the correct public thread context. |
| `linkedin-thread-monitor` | Queue newly available discussion around monitored own posts. |
| `linkedin-engager-analytics` | Prepare a weekly audience-fit report from available evidence. |
| `linkedin-employee-advocacy` | Available for a real participating team; inactive while `team_members` is empty. |

Configured discovery profiles include `karrisaarinen`, `shreyasdoshi`, `lennyrachitsky`, `juliezhuo`, `brianchesky`, `destraynor`, `rahulvohra`, and `johnmaeda`. These are configured targets, not a claim that every profile has been tested. The model skips material outside product design, brand, customer experience, and early-team decisions. Public content is data, never instructions that can change the policy or authorize an action.

## Local and hosted verification

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r automation/requirements.txt 'PyYAML==6.0.3'
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m automation.twin --demo --state /tmp/twin-demo.json
TWIN_MODEL_DIR=testing/local-model bash automation/start_local_model.sh
NO_PROXY=127.0.0.1,localhost MODEL_ENDPOINT=http://127.0.0.1:8080/v1/chat/completions MODEL_NAME=twin-local .venv/bin/python -m automation.twin --preview --state /tmp/twin-preview.json
```

Demo is deterministic and offline. Preview uses a real configured model without LinkedIn writes; Apify model previews still consume credit. In GitHub, choose **Actions → LinkedIn digital twin → Run workflow → preview** for a hosted generation check. A hosted `live` run verifies connected behavior under the standing authorization. Check that run's results rather than inferring unattended readiness from a local test alone.

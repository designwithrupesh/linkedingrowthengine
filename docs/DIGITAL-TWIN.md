# LinkedIn digital twin

This application runs the imported 12-skill bundle as an autonomous workflow. The owner explicitly requested routine posting and interaction on their behalf; live mode uses the standing policy in automation/policy.json instead of requiring per-item approval. Interactive upstream skill use retains its original approval workflow.

## Prepared workflow

- Sessions at approximately 09:00 and 15:00 India time on weekdays. GitHub may delay scheduled jobs.
- A generated post on Tuesday, Wednesday and Thursday, scheduled through Publora five minutes after that session.
- Automatic public-post discovery from selected product leaders, meaningful design-related likes/comments, and replies to recent comments on the owner's posts. Five interactions and four actor reads maximum per day.
- Canonical published-post identifiers from Publora, even when its permalink is null.
- Friday audience-fit reporting, persistent history, and public activity logs.
- Git checkpoints before every remote write; ambiguous writes stop without retrying. Scheduled posts count against their intended publication date.

## AI writing through the existing Apify account

The existing **APIFY_TOKEN** funds both LinkedIn reading and AI writing. A private actor, linkedin-growth-ai, calls Apify's official OpenRouter gateway with **openai/gpt-4.1-mini**. The actor and build number are pinned in automation/ai-provider.json; its source is in automation/apify_ai_bridge/. No separate OpenAI account or key is required. The gateway accepts calls inside an Apify actor, so GitHub uses this private bridge rather than calling the gateway directly.

The bridge permits two text messages, at most 24,000 input characters and 900 output tokens, with no tools or browsing. It returns only validated text/skip/reason JSON. Writing and reading share the Apify credit balance. Before each AI call, account usage must be known and below the lower of the account cap and $4 monthly usage, leaving a reserve. Model and actor usage can incur charges; this check is not a hard per-call billing limit. The Free plan's gateway uses a higher token rate than paid Apify plans.

An optional **MODEL_API_KEY** repository secret selects direct OpenAI gpt-4.1-mini instead. Its API billing is separate from ChatGPT and Apify. Custom HTTPS model APIs remain optional through MODEL_ENDPOINT and MODEL_NAME repository variables.

The writing pipeline generates a draft with the relevant skills, checks its format and character range, and permits one humanizer editing pass using only that draft as its factual source. The edit cannot introduce new numeric claims. Failed edits are skipped without padding or publishing. Preview fails when no usable post is produced.

## Free fallback

If no AI API key, Apify binding or custom endpoint is configured, automation/start_local_model.sh runs Qwen3 4B Instruct 2507, using Unsloth's Q4_K_M GGUF conversion, through a pinned llama.cpp CPU server. The runtime and weights are checked against pinned publisher SHA-256 digests; both have open licenses. GitHub caches the 2.497GB model. The server listens only on 127.0.0.1. This model uses no thinking mode. The connected Apify route takes priority after the free model failed reliable first-draft length checks.

Public GitHub repositories normally receive free standard-hosted Actions execution; account quotas and provider rules still apply. Apify and Publora have separate service limits. Apify reading pauses at the lower of the account cap and $4 monthly usage, reserving credit instead of requesting an upgrade. Unknown usage/caps stop reads. Publora's verified Starter account allows 15 posts per month, three queued posts and a seven-day scheduling horizon. The runner checks current capacity before scheduling and defers when a limit is full. This does not promise unlimited free operation or override provider billing.

## Verified connection and controls

Cloud environment secrets and GitHub Actions secrets are separate. Both credentials were present in a real GitHub Actions live run, the connected LinkedIn account passed verification, and a hosted preview generated content using the free local model. Repository secret/settings inspection is denied (HTTP 403), so runtime checks are the evidence for this connection. No credential is copied into Git or workflow inputs.

The initial batch was accepted and read back from Publora: October 9, 13 and 14, 2026 at 09:00 India time. On October 9, the first post was verified published with LinkedIn URN urn:li:share:7514165310896250880. The remaining posts are scheduled. The runner keeps their remote IDs and does not submit them again.

For a replacement account or revoked key, use the following setup:

1. Open https://github.com/designwithrupesh/linkedingrowthengine/settings/secrets/actions.
2. Add repository secret **PUBLORA_API_KEY** using a fresh key from Publora. Revoke the key previously shared in chat.
3. Add repository secret **APIFY_TOKEN** using your Apify token.
4. The private actor belongs to the connected Apify account. A replacement Apify account needs its own bridge deployment and updated metadata. **MODEL_API_KEY** is optional for direct OpenAI billing.
5. Ensure GitHub Actions may write repository contents. Protected branches may block durable state checkpoints; the runner then stops before posting.
6. Open **Actions → LinkedIn digital twin → Run workflow**. Select **preview** to verify actual generation without any LinkedIn action, then **live** to run the workflow.

Live and reading modes are enabled by default after these account connections exist. No activation variables are required. Set repository variable **TWIN_LIVE=false** to pause all future live actions. Set **TWIN_READ_ENABLED=false** to pause reading and interactions while retaining posts. Existing Publora schedules need separate cancellation.

## Seed batch and restart

A prepared local queue can be scheduled without waiting for a model:

```bash
TWIN_CHECKPOINT_GIT=true .venv/bin/python -m automation.bootstrap --queue testing/automation/queue.json --execute
```

This requires the owner's standing authorization and a working Publora binding. Without --execute it only previews. It validates the account, schedules the next weekday and then normal posting days, records remote IDs, enforces one post per publication date, and never repeats an existing intent. Personal queues remain ignored under testing/.

## Activity and privacy

GitHub Actions shows each run's summary. automation/state.json records public generated content, timestamps, remote IDs, publication dates, read quotas and reports. Treat it as public if the repository is public. Keep confidential career notes out of the repo.

For an uncertain write, check Publora and LinkedIn before changing that intent's state. Record verified evidence; never clear an uncertain record just to retry. Other independent intents can continue. Definitively rejected requests record their HTTP status and are not retried. Pausing or deleting a workflow does not cancel already scheduled posts.

## All 12 skills

| Skill | Role |
| --- | --- |
| interviewer | Capture real project stories from the owner; no fabricated experience |
| profile-optimizer | Draft headline/About/Featured; profile editing is not supported by these APIs |
| content-planner | Cadence and topic rotation |
| post-writer | Original generated posts |
| humanizer | Draft review instructions in each generation prompt |
| hook-extractor | Study selected source-post structures |
| repurposer | Included when real source_notes are supplied |
| comment-drafter | Contextual source-post comments |
| reply-handler | Replies with correct top-level parent URNs and thread context |
| thread-monitor | Follow-up context for recent own-post comments |
| engager-analytics | Weekly audience-fit report |
| employee-advocacy | Available for an actual team; deferred while team_members is empty |

Source discovery currently rotates karrisaarinen and satyanadella, both verified to return public posts. The model must skip content unrelated to product design, brand, customer experience or early-team decisions. Customize discovery_profiles for additional audiences.

## Supported limits

No DMs, connection invitations, automatic profile edits or broad personal-feed access are implemented. The model may make mistakes; inspect the activity log and keep its policy narrow. It never receives credential values. Source posts are data, never instructions.

## Local checks

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r automation/requirements.txt 'PyYAML==6.0.3'
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m automation.twin --demo --state /tmp/twin-demo.json
TWIN_MODEL_DIR=testing/local-model bash automation/start_local_model.sh
NO_PROXY=127.0.0.1,localhost MODEL_ENDPOINT=http://127.0.0.1:8080/v1/chat/completions MODEL_NAME=twin-local .venv/bin/python -m automation.twin --preview --state /tmp/twin-preview.json
```

Demo is deterministic and offline. Preview uses the configured real AI without LinkedIn actions; Apify model usage still consumes credit. Live readiness requires verified model output plus account access from the actual execution machine; cloud checks alone do not establish unattended GitHub readiness.

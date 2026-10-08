# LinkedIn digital twin

This application runs the imported 12-skill bundle as an autonomous workflow. The owner explicitly requested routine posting and interaction on their behalf; live mode uses the standing policy in automation/policy.json instead of requiring per-item approval. Interactive upstream skill use retains its original approval workflow.

## Prepared workflow

- Sessions at approximately 09:00 and 15:00 India time on weekdays. GitHub may delay scheduled jobs.
- A generated post on Tuesday, Wednesday and Thursday, scheduled through Publora five minutes after that session.
- Automatic public-post discovery from selected product leaders, meaningful design-related likes/comments, and replies to recent comments on the owner's posts. Five interactions and four actor reads maximum per day.
- Canonical published-post identifiers from Publora, even when its permalink is null.
- Friday audience-fit reporting, persistent history, and public activity logs.
- Git checkpoints before every remote write; ambiguous writes stop without retrying. Scheduled posts count against their intended publication date.

## Free model: no AI API key

automation/start_local_model.sh runs official Qwen3 1.7B Q8 weights through a pinned llama.cpp CPU server. The runtime and weights are checked against their publisher SHA-256 digests. GitHub caches the 1.834GB model. The server listens only on 127.0.0.1; thinking is disabled to keep runtime and output manageable. Custom HTTPS model APIs remain optional.

Public GitHub repositories normally receive free standard-hosted Actions execution; account quotas and provider rules still apply. Apify and Publora have separate service limits. Apify reading pauses at the lower of the account cap and $4 monthly usage, reserving credit instead of requesting an upgrade. Unknown usage/caps stop reads. This does not promise unlimited free operation or override provider billing.

## One-time GitHub connection

Cloud environment secrets and GitHub Actions secrets are separate. The cloud Publora and Apify account checks pass, but this chat is denied repository secret/settings access (HTTP 403). No credential is copied into Git or workflow inputs.

1. Open https://github.com/designwithrupesh/linkedingrowthengine/settings/secrets/actions.
2. Add repository secret **PUBLORA_API_KEY** using a fresh key from Publora. Revoke the key previously shared in chat.
3. Add repository secret **APIFY_TOKEN** using your Apify token.
4. Ensure GitHub Actions may write repository contents. Protected branches may block durable state checkpoints; the runner then stops before posting.
5. Open **Actions → LinkedIn digital twin → Run workflow**. Select **preview** to verify actual free-model generation without any LinkedIn action, then **live** to run the workflow.

Live and reading modes are enabled by default in the workflow after these account connections exist. No model key or activation variables are required. Set repository variable **TWIN_LIVE=false** to pause all future live actions. Set **TWIN_READ_ENABLED=false** to pause reading and interactions while retaining posts. Existing Publora schedules need separate cancellation.

## Seed batch and restart

A prepared local queue can be scheduled without waiting for a model:

```bash
TWIN_CHECKPOINT_GIT=true .venv/bin/python -m automation.bootstrap --queue testing/automation/queue.json --execute
```

This requires the owner's standing authorization and a working Publora binding. Without --execute it only previews. It validates the account, schedules the next weekday and then normal posting days, records remote IDs, enforces one post per publication date, and never repeats an existing intent. Personal queues remain ignored under testing/.

## Activity and privacy

GitHub Actions shows each run's summary. automation/state.json records public generated content, timestamps, remote IDs, publication dates, read quotas and reports. Treat it as public if the repository is public. Keep confidential career notes out of the repo.

For an uncertain write, check Publora and LinkedIn before changing the state. Record the verified remote ID/status and reconcile before resuming. Never clear an uncertain record just to retry. Pausing or deleting a workflow does not cancel already scheduled posts.

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

Demo is deterministic and offline. Preview uses real local inference without LinkedIn calls. Live readiness requires verified model output plus account access from the actual execution machine; cloud checks alone do not establish unattended GitHub readiness.

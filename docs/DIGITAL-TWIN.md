# Autonomous LinkedIn digital twin

The application in automation/twin.py runs the imported skill bundle with a model and persistent state. The owner has requested autonomous routine posts and interactions. Live mode therefore uses the standing policy in automation/policy.json rather than requiring approval for every item. Interactive use of the upstream skills retains its original approval flow.

## What is implemented

- GitHub Actions sessions at approximately 09:00 and 15:00 India time on weekdays. GitHub may delay or skip scheduled runs; these are not exact delivery guarantees.
- One generated post on Tuesday, Wednesday, and Thursday, scheduled through Publora for five minutes after that session.
- Likes and comments on configured target post URLs and replies to recent comments on the twin's published posts, with at most five interactions per day.
- Own-post URL discovery from Publora and Friday audience-fit reporting through Apify.
- A policy containing confirmed background, goals, topics and boundaries; persistent post history informs new drafts.
- Git checkpoints before remote writes. Timeouts or ambiguous responses stop the run; uncertain actions are not retried automatically.
- Offline preview and tests. No LinkedIn passwords, session cookies or unofficial browser login required.

## One-time activation: GitHub settings

These are GitHub Actions settings, separate from the Codex cloud environment settings used earlier. Cloud secret bindings do not automatically become GitHub Actions secrets.

1. Open https://github.com/designwithrupesh/linkedingrowthengine/settings/secrets/actions.
2. Click **New repository secret**. Add a fresh **PUBLORA_API_KEY** from Publora. Revoke the key shared in chat.
3. For automatic reading and interaction, add **APIFY_TOKEN**. Without it, leave reading disabled; scheduled posting works separately. Provider free credits are limited and reading can incur charges.
4. Enable GitHub Models access for this repository/account if available. The workflow requests `models: read` and uses its built-in GitHub token; availability and free rate limits depend on GitHub. If that route is unavailable, supply a compatible model API's **MODEL_API_KEY**, and repository variables **MODEL_ENDPOINT** (HTTPS chat-completions URL) and **MODEL_NAME**. No paid model subscription is provisioned automatically.
5. In **Settings → Secrets and variables → Actions → Variables**, add **TWIN_LIVE** with value **true**. Add **TWIN_READ_ENABLED=true** only when you want Apify reading enabled and understand your provider usage limits. Remove/set TWIN_LIVE=false to stop all future live actions; existing Publora schedules need cancellation separately.
6. Ensure GitHub Actions is enabled and may write repository contents. Branch protection may prevent checkpoints; the runner stops before posting if a checkpoint cannot be saved.
7. Open **Actions → LinkedIn digital twin → Run workflow**. Select **demo** first to verify the GitHub runner. Then select **live** for account verification and a first session. If it is not a posting day/window, that session will not create a post.

The LinkedIn platform ID and India timezone are already configured. Secrets belong in GitHub settings, never in policy.json or chat.

## Review activity

Actions shows each run's summary. automation/state.json records generated public content, attempt status, remote IDs, daily limits and weekly reports. Treat this file as public if the repository is public. Keep confidential source notes out of the repo; this setup does not require any private project details.

For an uncertain write, check Publora and LinkedIn first. Record a verified remote ID/status in the state before resuming. Never clear an uncertain record simply to rerun it. Disabling the twin does not cancel posts already scheduled in Publora.

## All 12 skills and their roles

| Skill | Autonomous application |
| --- | --- |
| interviewer | One-time capture of real stories from the owner; never manufactures experience |
| profile-optimizer | Profile draft prepared during onboarding; profile edits require supported API access or owner action |
| content-planner | Provides cadence and topic rotation instructions to generation |
| post-writer | Generates the scheduled post |
| humanizer | Included in every generation and review prompt |
| hook-extractor | Included when studying selected target posts before commenting |
| repurposer | Included when source_notes contains real source material |
| comment-drafter | Generates comments on selected target post URLs |
| reply-handler | Generates contextual replies with correct top-level parent URNs |
| thread-monitor | Provides context rules for recent own-post reply threads |
| engager-analytics | Friday own-post audience-fit report |
| employee-advocacy | Available for an actual team; deferred while team_members is empty |

## Limits that remain

The repository's providers do not offer a broad LinkedIn feed-discovery method, DMs, automatic profile editing, or connection invitations. target_post_urls starts empty: the runner can reply to its own posts but needs real target URLs to comment on others. It does not fabricate URLs. Populate that list with relevant public post links, or implement a supported discovery provider separately.

No provider or GitHub Models connection has been verified in this onboarding session. No post or interaction has been sent. GitHub API access for remote dispatch/secret management is blocked from this cloud session, although Git pushes work. The model can still make mistakes; use policy boundaries and inspect the public activity log. API free tiers and GitHub Actions free allowances are not unlimited; this is not a guarantee of zero-cost end-to-end operation.

## Local verification

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r automation/requirements.txt 'PyYAML==6.0.3'
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m automation.twin --demo --state /tmp/twin-preview.json
```

The demo uses a fixed illustrative draft, not a real model or LinkedIn API. It verifies offline execution and state deduplication, not live behavior.

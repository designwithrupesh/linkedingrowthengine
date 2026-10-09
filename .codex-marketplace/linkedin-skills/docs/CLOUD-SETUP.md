# LinkedIn Growth Engine setup

This repository imports the 12-skill linkedin-skills bundle from https://github.com/sergebulaev/linkedin-skills at commit bfa41ff5d2b137f6023f88d9c9fda68d75b4ed5f. The original MIT license and attribution are retained. The upstream README describes the bundle and its integrations.

## Install and validate

Run from this repository's root with Python 3.10 or newer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r automation/requirements.txt 'PyYAML==6.0.3'
.venv/bin/python -m pip check
.venv/bin/python scripts/check_frontmatter.py
.venv/bin/python scripts/check_markdown_references.py
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/check_config.py --offline
```

## Use the skills

Read SKILL.md and the relevant skills/<name>/SKILL.md in your AI session. A checkout alone does not guarantee automatic plugin activation. The upstream README also describes Codex plugin installation.

Start with linkedin-interviewer to collect real stories, then linkedin-profile-optimizer and linkedin-content-planner. Use linkedin-post-writer or linkedin-repurposer to draft, and linkedin-humanizer to review. linkedin-comment-drafter and linkedin-reply-handler prepare interactions. linkedin-hook-extractor studies selected reference posts. linkedin-thread-monitor and linkedin-engager-analytics support follow-up and audience review. linkedin-employee-advocacy applies when an actual team participates.

Keep personal voice profiles, stories, draft queues, and reports in the ignored testing/ directory. Pass these working files as context instead of publishing personal material in the upstream reference templates.

## Autonomous runner

The imported skills are connected to the persistent GitHub Actions workflow in .github/workflows/digital-twin.yml. See [DIGITAL-TWIN.md](DIGITAL-TWIN.md) for the cadence, free local model, verified connection, activity records and pause controls. The owner has authorized routine autonomous posts and interactions within automation/policy.json. The upstream interactive skills still use their original draft approval flow when invoked separately.

## Account connections

Securely configure PUBLORA_API_KEY and LINKEDIN_PLATFORM_ID for publishing, and APIFY_TOKEN for reading posts, comments, engagers and the private AI-writing bridge. The bridge uses the connected Apify account's existing credits; MODEL_API_KEY is optional for direct OpenAI billing. PIXFARO_TOKEN is optional for images. Never commit credentials or paste them into public issues. Use secure cloud environment settings or a local ignored .env file.

Publora executes posts scheduled through its API. The GitHub runner handles drafting, discovery and supported interactions even after a cloud session closes. It does not send DMs or automatically edit a LinkedIn profile.

Use explicit timezone-aware timestamps for a weekly schedule. scripts/schedule_post.py defaults to today's 10:00 in the host timezone, or five minutes from now; it is not a future weekly calendar.

For Codex cloud, use the existing checkout: tasks are already isolated, so do not create Git worktrees unless requested. Local model previews need automation/start_local_model.sh; persistent automation starts its own model in GitHub Actions.

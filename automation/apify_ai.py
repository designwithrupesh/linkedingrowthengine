"""Private, build-pinned AI bridge using an existing secure Apify binding.

The metadata contains public identifiers only. A caller cannot redirect its
credential to another actor, build, host, path or query. Account usage is read
before each inference; no dataset or actor discovery request is made here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

import requests

from automation.discovery import read_budget_available
from lib.apify_client import ApifyClient


CONFIG_PATH = Path(__file__).resolve().parent / 'ai-provider.json'
MODEL = 'openai/gpt-4.1-mini'
_IDENTIFIER = re.compile(r'[A-Za-z0-9]{17}')
_BUILD_NUMBER = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+')


class ApifyModelError(RuntimeError):
    """A fixed, credential-free diagnostic for this bounded AI backend."""


class ApifyBudgetError(ApifyModelError):
    """Account usage or the selected spending limit prevents inference."""


def model_info(config_path: Path | str | None = None) -> tuple[str, str] | None:
    """Return the pinned destination and fixed model, or None for invalid config."""
    path = Path(config_path) if config_path is not None else CONFIG_PATH
    try:
        metadata = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(metadata, dict):
        return None
    actor, build = metadata.get('actor_id'), metadata.get('build_id')
    if not all(isinstance(value, str) and _IDENTIFIER.fullmatch(value) for value in (actor, build)):
        return None
    number = metadata.get('build_number')
    if not isinstance(number, str) or len(number) > 30 or not _BUILD_NUMBER.fullmatch(number):
        return None
    if metadata.get('model', MODEL) != MODEL:
        return None
    name = metadata.get('name')
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 120):
        return None
    # Do not accept a configurable host, path, actor name, query, or timeout.
    # Apify run-sync accepts an immutable build number here, not its opaque
    # build ID. The verified ID remains in metadata for deployment review.
    endpoint = f'https://api.apify.com/v2/acts/{actor}/run-sync?build={number}&timeout=120&memory=256'
    return endpoint, MODEL


def is_apify_model_endpoint(endpoint: Any, config_path: Path | str | None = None) -> bool:
    """Only the exact canonical URL in the locally pinned config is authorized."""
    info = model_info(config_path)
    return bool(info is not None and isinstance(endpoint, str) and endpoint == info[0])


def authorization_headers(endpoint: str, config_path: Path | str | None = None) -> dict[str, str]:
    """Check identity and credit reserve before returning a secure header.

    The existing budget guard fails closed when usage is unknown and permits
    spending only below the smaller of the account cap and four USD monthly.
    This reserve check is not a guarantee of provider billing or a hard per-run
    model-spend limit. Actor and model bounds belong to the deployed build.
    """
    if not is_apify_model_endpoint(endpoint, config_path):
        raise ApifyModelError('Apify AI endpoint does not match the pinned actor build')
    token = os.getenv('APIFY_TOKEN')
    if not token:
        raise ApifyModelError('APIFY_TOKEN secure binding is unavailable')
    try:
        permitted = read_budget_available(ApifyClient(token=token))
    except Exception:
        permitted = False
    if not permitted:
        raise ApifyBudgetError('Apify AI paused because the configured budget or account limits cannot be verified')
    return {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}


def complete(endpoint: str, payload: dict, *, config_path: Path | str | None = None) -> dict:
    """Send one standard chat-completions request and return native choices.

    No inference retry is performed. Redirects are disabled, and provider
    bodies, prompts and credential values are never included in diagnostics.
    """
    if not isinstance(payload, dict) or payload.get('model') != MODEL:
        raise ApifyModelError('Apify AI request must use the configured model')
    headers = authorization_headers(endpoint, config_path)
    try:
        response = requests.post(endpoint, headers=headers, json=payload,
                                 timeout=150, allow_redirects=False)
    except requests.RequestException:
        raise ApifyModelError('Apify AI request failed before a usable response') from None
    if not 200 <= response.status_code < 300:
        raise ApifyModelError(f'Apify AI request failed (HTTP {response.status_code})')
    try:
        result = response.json()
    except (ValueError, TypeError):
        raise ApifyModelError('Apify AI response was not valid JSON') from None
    if not isinstance(result, dict) or not isinstance(result.get('choices'), list) or not result['choices']:
        raise ApifyModelError('Apify AI response did not contain chat-completion choices')
    return result

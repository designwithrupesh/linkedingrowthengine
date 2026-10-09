import assert from 'node:assert/strict';
import test from 'node:test';
import { GATEWAY, MODEL, runBridge, sanitizeCompletion, validateInput } from './main.mjs';

const input = () => ({ model: MODEL, messages: [
  { role: 'system', content: 'Follow the supplied design policy.' },
  { role: 'user', content: 'Write one concrete design observation.' },
], max_tokens: 900, temperature: 0.5, response_format: { type: 'json_object' } });

test('fixed provider, strict result schema and no tools or streaming', () => {
  const request = validateInput(input());
  assert.equal(request.model, MODEL);
  assert.equal(request.max_tokens, 900);
  assert.equal(request.stream, false);
  assert.equal(request.response_format.json_schema.strict, true);
  assert.equal(request.response_format.json_schema.schema.additionalProperties, false);
  assert.equal(request.tools, undefined);
});

test('expensive models, extra messages, oversized prompts, tools and output budgets are rejected', () => {
  for (const change of [
    { model: 'openai/gpt-4.1' }, { max_tokens: 901 }, { max_tokens: 0 }, { stream: true },
    { tools: [{ type: 'web_search' }] },
    { messages: [...input().messages, { role: 'user', content: 'Extra request' }] },
    { messages: [{ role: 'system', content: 'x'.repeat(24000) }, { role: 'user', content: 'x' }] },
    { messages: [{ role: 'user', content: 'Wrong role' }, input().messages[1]] },
  ]) assert.throws(() => validateInput({ ...input(), ...change }));
});

test('completion strips provider metadata and rejects truncated or malformed content', () => {
  const content = JSON.stringify({ text: 'A concrete design observation.', skip: false, reason: '' });
  const response = { secret: 'never copied', usage: { prompt_tokens: 23 }, choices: [
    { message: { role: 'assistant', content, reasoning: 'never copied' }, finish_reason: 'stop' },
  ] };
  assert.deepEqual(Object.keys(sanitizeCompletion(response)), ['choices']);
  assert.equal(JSON.stringify(sanitizeCompletion(response)).includes('never copied'), false);
  assert.throws(() => sanitizeCompletion({ choices: [{ message: { content }, finish_reason: 'length' }] }));
  assert.throws(() => sanitizeCompletion({ choices: [{ message: { content: '{}' }, finish_reason: 'stop' }] }));
});

test('input, official gateway and safe OUTPUT use injected authentication without storing the key', async () => {
  const calls = [];
  const response = { choices: [{ message: { content: JSON.stringify({ text: 'Design has trade-offs.', skip: false, reason: '' }) }, finish_reason: 'stop' }] };
  const fetcher = async (url, options) => {
    calls.push({ url, options });
    return { ok: true, status: 200, json: async () => calls.length === 1 ? input() : response };
  };
  await runBridge({ APIFY_TOKEN: 'test-credential', APIFY_DEFAULT_KEY_VALUE_STORE_ID: 'testStore' }, fetcher);
  assert.equal(calls.length, 3);
  assert.equal(calls[1].url, GATEWAY);
  for (const call of calls) {
    assert.equal(call.options.redirect, 'error');
    assert.equal(call.options.headers.Authorization, 'Bearer test-credential');
    assert.equal((call.options.body || '').includes('test-credential'), false);
  }
  assert.deepEqual(Object.keys(JSON.parse(calls[2].options.body)), ['choices']);
});

test('provider errors persist only fixed error and HTTP status, and make no retry', async () => {
  const calls = [];
  const fetcher = async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1) return { ok: true, json: async () => input() };
    if (calls.length === 2) return { ok: false, status: 403, json: async () => { throw new Error('Provider secret'); } };
    return { ok: true };
  };
  await assert.rejects(runBridge({ APIFY_TOKEN: 'test-credential', APIFY_DEFAULT_KEY_VALUE_STORE_ID: 'testStore' }, fetcher));
  assert.equal(calls.filter(call => call.url === GATEWAY).length, 1);
  assert.deepEqual(JSON.parse(calls[2].options.body), { error: 'AI bridge generation unavailable', status: 403 });
});

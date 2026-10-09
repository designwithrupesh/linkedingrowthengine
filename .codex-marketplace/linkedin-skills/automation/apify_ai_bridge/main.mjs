import { fileURLToPath } from 'node:url';

export const MODEL = 'openai/gpt-4.1-mini';
export const GATEWAY = 'https://openrouter.apify.actor/api/v1/chat/completions';
const RESULT_SCHEMA = {
  type: 'object',
  properties: { text: { type: 'string' }, skip: { type: 'boolean' }, reason: { type: 'string' } },
  required: ['text', 'skip', 'reason'],
  additionalProperties: false,
};

export function validateInput(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('invalid_input');
  const allowed = new Set(['model', 'messages', 'temperature', 'max_tokens', 'response_format', 'stream']);
  if (Object.keys(input).some(key => !allowed.has(key))) throw new Error('invalid_input');
  if (input.model && ![MODEL, 'gpt-4.1-mini'].includes(input.model)) throw new Error('invalid_input');
  if (!Array.isArray(input.messages) || input.messages.length !== 2) throw new Error('invalid_input');
  const messages = input.messages.map((message, index) => {
    if (!message || typeof message !== 'object' || message.role !== ['system', 'user'][index]
        || typeof message.content !== 'string' || !message.content.trim()
        || Object.keys(message).some(key => !['role', 'content'].includes(key))) throw new Error('invalid_input');
    return { role: message.role, content: message.content };
  });
  if (messages.reduce((sum, message) => sum + message.content.length, 0) > 24000) throw new Error('invalid_input');
  const maxTokens = input.max_tokens ?? 900;
  if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 900) throw new Error('invalid_input');
  const temperature = input.temperature ?? 0.5;
  if (!Number.isFinite(temperature) || temperature < 0 || temperature > 2) throw new Error('invalid_input');
  if (input.stream !== undefined && input.stream !== false) throw new Error('invalid_input');
  return {
    model: MODEL, messages, max_tokens: maxTokens, temperature, stream: false,
    response_format: {
      type: 'json_schema',
      json_schema: { name: 'linkedin_twin_result', strict: true, schema: RESULT_SCHEMA },
    },
  };
}

export function sanitizeCompletion(payload) {
  const choice = payload?.choices?.[0];
  const content = choice?.message?.content;
  if (typeof content !== 'string' || !content || content.length > 12000
      || choice.finish_reason !== 'stop') throw new Error('invalid_response');
  const result = JSON.parse(content);
  if (!result || typeof result !== 'object' || Array.isArray(result)
      || typeof result.text !== 'string' || typeof result.skip !== 'boolean' || typeof result.reason !== 'string'
      || Object.keys(result).some(key => !['text', 'skip', 'reason'].includes(key))) throw new Error('invalid_response');
  return { choices: [{ message: { role: 'assistant', content: JSON.stringify(result) }, finish_reason: 'stop' }] };
}

export async function runBridge(env = process.env, fetchImpl = fetch) {
  const token = env.APIFY_TOKEN;
  const store = env.APIFY_DEFAULT_KEY_VALUE_STORE_ID;
  if (!token || !store || !/^[A-Za-z0-9]+$/.test(store)) throw new Error('runtime_unavailable');
  const storage = `https://api.apify.com/v2/key-value-stores/${encodeURIComponent(store)}/records/`;
  const authorization = `Bearer ${token}`;
  let output;
  let failureStatus = 0;
  try {
    const inputResponse = await fetchImpl(storage + 'INPUT', {
      headers: { Authorization: authorization }, redirect: 'error', signal: AbortSignal.timeout(10000),
    });
    if (!inputResponse.ok) throw new Error('input_unavailable');
    const request = validateInput(await inputResponse.json());
    const response = await fetchImpl(GATEWAY, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(80000),
      headers: { Authorization: authorization, 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    if (!response.ok) {
      failureStatus = response.status;
      throw new Error('generation_unavailable');
    }
    output = sanitizeCompletion(await response.json());
  } catch {
    // Provider bodies, exception messages, input and credentials are never logged.
    output = { error: 'AI bridge generation unavailable', status: failureStatus || 400 };
  }
  const saved = await fetchImpl(storage + 'OUTPUT', {
    method: 'PUT', redirect: 'error', signal: AbortSignal.timeout(10000),
    headers: { Authorization: authorization, 'Content-Type': 'application/json' },
    body: JSON.stringify(output),
  });
  if (!saved.ok) throw new Error('storage_unavailable');
  if (output.error) throw new Error('generation_unavailable');
  return output;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  runBridge().then(() => {
    console.log('AI bridge completed.');
  }).catch(() => {
    console.error('AI bridge failed; inspect the safe OUTPUT status.');
    process.exitCode = 1;
  });
}

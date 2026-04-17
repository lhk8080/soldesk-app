const READ_API_BASE = '/api/read';

function resolveReadTarget(path) {
  const rel = `${READ_API_BASE}${path}`;
  const runtime = window.APP_RUNTIME;
  if (runtime && typeof runtime.resolveTicketingApiUrl === 'function') {
    return runtime.resolveTicketingApiUrl(rel);
  }
  return rel;
}

async function readApi(path, options = {}) {
  const runtime = window.APP_RUNTIME;
  const targetPath = resolveReadTarget(path);

  if (runtime && typeof runtime.getJson === 'function') {
    return runtime.getJson(targetPath, options);
  }

  const extraAuth = {};
  try {
    const tok = typeof window.__TICKETING_AUTH_BEARER_TOKEN__ === 'string'
      ? window.__TICKETING_AUTH_BEARER_TOKEN__.trim()
      : '';
    if (tok) extraAuth.headers = { Authorization: `Bearer ${tok}` };
  } catch (e) { /* ignore */ }

  const response = await fetch(targetPath, {
    method: 'GET',
    credentials: 'omit',
    cache: options.cache || 'default',
    ...extraAuth
  });

  if (!response.ok) {
    throw new Error(`READ API 오류: ${response.status}`);
  }

  return await response.json();
}

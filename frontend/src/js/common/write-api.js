const WRITE_API_BASE = '/api/write';

function resolveWriteTarget(path) {
  const rel = `${WRITE_API_BASE}${path}`;
  const runtime = window.APP_RUNTIME;
  if (runtime && typeof runtime.resolveTicketingApiUrl === 'function') {
    return runtime.resolveTicketingApiUrl(rel);
  }
  return rel;
}

async function writeApi(path, method = 'POST', data = null, options = {}) {
  const runtime = window.APP_RUNTIME;
  const targetPath = resolveWriteTarget(path);

  if (runtime && typeof runtime.requestJson === 'function') {
    const result = await runtime.requestJson(targetPath, {
      ...(options || {}),
      method,
      body: data
    });
    const m = String(method || 'POST').toUpperCase();
    if (runtime && typeof runtime.notifyReadCacheRebuilt === 'function' && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(m)) {
      runtime.notifyReadCacheRebuilt();
    }
    return result;
  }

  const headers = { 'Content-Type': 'application/json' };
  try {
    const tok = typeof window.__TICKETING_AUTH_BEARER_TOKEN__ === 'string'
      ? window.__TICKETING_AUTH_BEARER_TOKEN__.trim()
      : '';
    if (tok) headers.Authorization = `Bearer ${tok}`;
  } catch (e) { /* ignore */ }

  const fetchOptions = {
    method,
    credentials: 'omit',
    headers
  };

  if (data !== null) {
    fetchOptions.body = JSON.stringify(data);
  }

  if (options && options.cache) {
    fetchOptions.cache = options.cache;
  }

  const response = await fetch(targetPath, fetchOptions);

  if (!response.ok) {
    let errorMessage = `WRITE API 오류: ${response.status}`;
    const httpError = new Error(errorMessage);
    httpError.status = response.status;
    httpError.data = null;

    try {
      const errorData = await response.json();
      httpError.data = errorData;
      if (errorData && errorData.message) {
        errorMessage = errorData.message;
      }
    } catch (error) {
      console.error(error);
    }

    httpError.message = errorMessage;
    throw httpError;
  }

  const result = await response.json();
  const m = String(method || 'POST').toUpperCase();
  if (runtime && typeof runtime.notifyReadCacheRebuilt === 'function' && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(m)) {
    runtime.notifyReadCacheRebuilt();
  }
  return result;
}

/** SQS 비동기 예매: GET status 응답이 처리 완료·실패·만료인지 */
function isTerminalAsyncBookingStatus(j) {
  if (!j || typeof j !== 'object') return true;
  if (j.status === 'PROCESSING') return false;
  if (j.status === 'UNKNOWN_OR_EXPIRED' || j.status === 'INVALID_REF') return true;
  return Object.prototype.hasOwnProperty.call(j, 'ok');
}

/**
 * /concerts/booking/status/{ref} 또는 /booking/status/{ref} 폴링.
 * @param {string} statusPath - writeApi 기준 경로 (예: /concerts/booking/status/uuid)
 */
async function pollAsyncBookingStatus(statusPath, options) {
  const timeoutSec = options && Number(options.timeoutSec) > 0 ? Number(options.timeoutSec) : 120;
  const intervalMs = options && Number(options.intervalMs) > 0 ? Number(options.intervalMs) : 400;
  const onProgress = options && typeof options.onProgress === 'function' ? options.onProgress : null;
  const deadline = Date.now() + timeoutSec * 1000;
  let last = {};
  while (Date.now() < deadline) {
    last = await writeApi(statusPath, 'GET', null, { cache: 'no-store' });
    if (onProgress && last && last.status === 'PROCESSING') {
      try { onProgress(last); } catch (e) { /* ignore */ }
    }
    if (isTerminalAsyncBookingStatus(last)) {
      return last;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return {
    ok: false,
    code: 'TIMEOUT',
    status: 'TIMEOUT',
    message: '예매 처리 시간이 초과되었습니다. 마이페이지에서 예매 내역을 확인해 주세요.'
  };
}

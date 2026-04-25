(function () {
  window.APP_CONFIG = window.APP_CONFIG || {};
  window.APP_RUNTIME = window.APP_RUNTIME || {};

  const runtime = window.APP_RUNTIME;
  const currentImage = window.APP_CONFIG.image || {};

  const stylePromiseMap = runtime.__stylePromiseMap || new Map();
  const scriptPromiseMap = runtime.__scriptPromiseMap || new Map();
  const prefetchedScriptSet = runtime.__prefetchedScriptSet || new Set();
  const modalState = runtime.__modalState || { savedScrollY: 0, locked: false };

  runtime.__stylePromiseMap = stylePromiseMap;
  runtime.__scriptPromiseMap = scriptPromiseMap;
  runtime.__prefetchedScriptSet = prefetchedScriptSet;
  runtime.__modalState = modalState;

  /** 배포 직후 S3·ALB·브라우저 캐시 불일치 시 /api/* 가 잘못된 오리진으로 나가는 레이스 완화 */
  let ticketingEndpointsLoadPromise = null;

  runtime.constants = {
    loginStorageKey: 'loginUser',
    rememberedLoginIdKey: 'rememberedLoginId',
    defaultLoginExpireMinutes: 120
  };

  window.APP_CONFIG.image = {
    baseUrl: currentImage.baseUrl || '/images/posters/',
    rewriteRootRelativePaths:
      typeof currentImage.rewriteRootRelativePaths === 'boolean'
        ? currentImage.rewriteRootRelativePaths
        : false,
    localPrefixMode: currentImage.localPrefixMode || 'filename',
    localPrefixes:
      Array.isArray(currentImage.localPrefixes) && currentImage.localPrefixes.length
        ? currentImage.localPrefixes
        : [],
    manifest: currentImage.manifest || {},
    keepAbsoluteUrls:
      typeof currentImage.keepAbsoluteUrls === 'boolean'
        ? currentImage.keepAbsoluteUrls
        : true,
    fallbackImageUrl: currentImage.fallbackImageUrl || '/images/posters/no-image.png'
  };

  function buildUrl(baseUrl, query) {
    const url = new URL(baseUrl, window.location.origin);
    const params = query || {};

    Object.entries(params).forEach(([key, value]) => {
      if (value === undefined || value === null || value === '') return;
      url.searchParams.set(key, String(value));
    });

    return url.toString();
  }

  function ensureStyle(href) {
    if (!href) return Promise.resolve(null);
    if (stylePromiseMap.has(href)) return stylePromiseMap.get(href);

    const promise = new Promise((resolve, reject) => {
      const existing = document.querySelector(`link[href="${href}"]`);
      if (existing) {
        if (existing.dataset.loaded === 'true' || existing.sheet) {
          existing.dataset.loaded = 'true';
          resolve(existing);
          return;
        }

        existing.addEventListener('load', () => {
          existing.dataset.loaded = 'true';
          resolve(existing);
        }, { once: true });

        existing.addEventListener('error', () => {
          stylePromiseMap.delete(href);
          reject(new Error(`style load fail: ${href}`));
        }, { once: true });
        return;
      }

      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = href;

      link.addEventListener('load', () => {
        link.dataset.loaded = 'true';
        resolve(link);
      }, { once: true });

      link.addEventListener('error', () => {
        stylePromiseMap.delete(href);
        reject(new Error(`style load fail: ${href}`));
      }, { once: true });

      document.head.appendChild(link);
    });

    stylePromiseMap.set(href, promise);
    return promise;
  }

  function ensureScript(src) {
    if (!src) return Promise.resolve(null);
    if (scriptPromiseMap.has(src)) return scriptPromiseMap.get(src);

    const promise = new Promise((resolve, reject) => {
      const existing = document.querySelector(`script[src="${src}"]`);
      if (existing) {
        if (existing.dataset.loaded === 'true') {
          resolve(existing);
          return;
        }

        existing.addEventListener('load', () => {
          existing.dataset.loaded = 'true';
          resolve(existing);
        }, { once: true });

        existing.addEventListener('error', () => {
          scriptPromiseMap.delete(src);
          reject(new Error(`script load fail: ${src}`));
        }, { once: true });
        return;
      }

      const script = document.createElement('script');
      script.src = src;
      script.defer = true;

      script.addEventListener('load', () => {
        script.dataset.loaded = 'true';
        resolve(script);
      }, { once: true });

      script.addEventListener('error', () => {
        scriptPromiseMap.delete(src);
        reject(new Error(`script load fail: ${src}`));
      }, { once: true });

      document.body.appendChild(script);
    });

    scriptPromiseMap.set(src, promise);
    return promise;
  }

  function prefetchScript(src) {
    if (!src) return;
    if (prefetchedScriptSet.has(src)) return;
    if (document.querySelector(`script[src="${src}"]`)) {
      prefetchedScriptSet.add(src);
      return;
    }
    if (document.querySelector(`link[rel="prefetch"][href="${src}"]`)) {
      prefetchedScriptSet.add(src);
      return;
    }

    const link = document.createElement('link');
    link.rel = 'prefetch';
    link.href = src;
    link.as = 'script';
    link.addEventListener('error', () => link.remove(), { once: true });
    document.head.appendChild(link);
    prefetchedScriptSet.add(src);
  }

  function prefetchScripts(sources) {
    if (!Array.isArray(sources)) return;
    sources.forEach(prefetchScript);
  }

  function readStorageJson(key) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (error) {
      console.error(`[storage] parse error: ${key}`, error);
      localStorage.removeItem(key);
      return null;
    }
  }

  function getLoginUser() {
    const key = runtime.constants.loginStorageKey;
    const parsed = readStorageJson(key);
    if (parsed && typeof parsed === 'object') {
      if (!parsed.expiresAt || Date.now() <= Number(parsed.expiresAt)) {
        return parsed;
      }
      localStorage.removeItem(key);
    } else if (parsed !== null) {
      localStorage.removeItem(key);
    }

    // Fallback: check Cognito token
    if (window.CognitoAuth && window.CognitoAuth.isLoggedIn()) {
      const cogUser = window.CognitoAuth.getCurrentUser();
      if (cogUser) {
        return {
          user_id: cogUser.sub,
          name: cogUser.name,
          email: cogUser.email,
          phone: cogUser.phone
        };
      }
    }

    return null;
  }

  function setStoredUserId(userId) {
    if (!userId) return;
    localStorage.setItem('user_id', String(userId));
  }

  function getStoredUserId() {
    const directUserId = localStorage.getItem('user_id') || sessionStorage.getItem('user_id');
    if (directUserId) return String(directUserId);

    const loginUser = getLoginUser();
    if (loginUser && loginUser.user_id) {
      return String(loginUser.user_id);
    }

    // Fallback: Cognito sub
    if (window.CognitoAuth && window.CognitoAuth.isLoggedIn()) {
      const cogUser = window.CognitoAuth.getCurrentUser();
      if (cogUser && cogUser.sub) return cogUser.sub;
    }

    return '';
  }

  function setLoginUser(userData, options) {
    const opts = options || {};
    const current = getLoginUser() || {};
    const expiresAt = opts.preserveExpires && current.expiresAt
      ? current.expiresAt
      : Date.now() + (Number(opts.expiresInMinutes || runtime.constants.defaultLoginExpireMinutes) * 60 * 1000);

    const payload = {
      ...userData,
      expiresAt
    };

    localStorage.setItem(runtime.constants.loginStorageKey, JSON.stringify(payload));
    if (payload.user_id) {
      setStoredUserId(payload.user_id);
    }
    return payload;
  }

  function patchLoginUser(nextFields, options) {
    const current = getLoginUser() || {};
    return setLoginUser({ ...current, ...(nextFields || {}) }, {
      preserveExpires: true,
      ...(options || {})
    });
  }

  function clearLoginUser() {
    localStorage.removeItem(runtime.constants.loginStorageKey);
    localStorage.removeItem('user_id');
    sessionStorage.removeItem('user_id');
    // Also clear Cognito tokens
    if (window.CognitoAuth && typeof window.CognitoAuth.clearTokens === 'function') {
      window.CognitoAuth.clearTokens();
    }
  }

  function getRememberedLoginId() {
    return localStorage.getItem(runtime.constants.rememberedLoginIdKey) || '';
  }

  function saveRememberedLoginId(value) {
    localStorage.setItem(runtime.constants.rememberedLoginIdKey, String(value || ''));
  }

  function clearRememberedLoginId() {
    localStorage.removeItem(runtime.constants.rememberedLoginIdKey);
  }

  function ensureMainBody() {
    let mainBody = document.getElementById('main-body');
    if (mainBody) return mainBody;

    mainBody = document.createElement('div');
    mainBody.id = 'main-body';

    const siteHeader = document.getElementById('site-header');
    if (siteHeader && siteHeader.parentNode) {
      if (siteHeader.nextSibling) {
        siteHeader.parentNode.insertBefore(mainBody, siteHeader.nextSibling);
      } else {
        siteHeader.parentNode.appendChild(mainBody);
      }
      return mainBody;
    }

    document.body.appendChild(mainBody);
    return mainBody;
  }

  function clearMainBody() {
    const mainBody = ensureMainBody();
    mainBody.innerHTML = '';
    mainBody.style.display = '';
    return mainBody;
  }

  function removeNodeById(id) {
    const node = document.getElementById(id);
    if (node) node.remove();
  }

  function resetPrimarySections() {
    clearMainBody();
    removeNodeById('main-body2');
  }

  function lockBodyScroll() {
    if (modalState.locked) return;
    modalState.savedScrollY = window.scrollY || window.pageYOffset || 0;
    modalState.locked = true;

    document.body.classList.add('login-modal-open');
    document.body.style.position = 'fixed';
    document.body.style.top = `-${modalState.savedScrollY}px`;
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.width = '100%';
  }

  function unlockBodyScroll() {
    if (!modalState.locked) return;
    modalState.locked = false;

    document.body.classList.remove('login-modal-open');
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.width = '';
    window.scrollTo(0, modalState.savedScrollY || 0);
  }

  /** ALB 호스트만 적힌 값에 http:// 보강 후, scheme+host(+port)만 남김(경로/쿼리 붙은 잘못된 설정 방지). */
  function normalizeTicketingApiOrigin(raw) {
    const s = String(raw || '').trim();
    if (!s) return '';
    let full = s.replace(/\/+$/, '');
    if (!/^https?:\/\//i.test(full)) {
      full = `http://${full}`;
    }
    try {
      const u = new URL(full);
      return `${u.protocol}//${u.host}`;
    } catch (e) {
      return full.replace(/\/+$/, '');
    }
  }

  /**
   * S3 정적 웹사이트와 API 호스트가 다를 때: /api/* 요청만 별도 오리진으로 보냄.
   * 오리진은 /api-origin.js(배포 후 sync) 또는 meta / APP_CONFIG — endpoints.json 미사용.
   *
   * API Gateway 전환 시: CloudFront로 API까지 한 오리진에 억지로 묶지 말고,
   * 지금과 동일하게 "프론트 오리진(S3) ≠ API 오리진(GW 또는 ALB)" 이면
   * __TICKETING_API_ORIGIN__ 에 GW/ALB 베이스 URL만 넣고, GW에서 CORS를 S3 웹 오리진에 맞게 열어 주면 됨.
   * (CloudFront에서 애먹었던 케이스는 보통 경로 프록시·이중 CORS·쿠키 도메인 불일치 쪽.)
   *
   * Cognito: window.__TICKETING_AUTH_BEARER_TOKEN__ 에 access token 넣으면 requestJson이 Authorization 자동 첨부(비어 있으면 무변화).
   */
  async function ensureTicketingEndpointsLoaded() {
    if (ticketingEndpointsLoadPromise) {
      return ticketingEndpointsLoadPromise;
    }
    ticketingEndpointsLoadPromise = (async () => {
      try {
        const res = await fetch(`/api-origin.js?__t=${Date.now()}`, {
          method: 'GET',
          cache: 'no-store',
          credentials: 'same-origin'
        });
        if (!res.ok) return;
        const text = await res.text();
        const m = text.match(/__TICKETING_API_ORIGIN__\s*=\s*"([^"]*)"/);
        if (m && String(m[1] || '').trim()) {
          window.__TICKETING_API_ORIGIN__ = normalizeTicketingApiOrigin(m[1]);
        }
      } catch (e) {
        /* 로컬 등 api-origin 없음 — 상대 /api 만 사용 */
      }
      await new Promise((r) => {
        if (typeof requestAnimationFrame === 'function') {
          requestAnimationFrame(() => r());
        } else {
          setTimeout(r, 0);
        }
      });
    })();
    try {
      await ticketingEndpointsLoadPromise;
    } catch (e) {
      ticketingEndpointsLoadPromise = null;
      throw e;
    }
    return undefined;
  }

  function getTicketingApiOrigin() {
    if (typeof window === 'undefined') return '';

    const fromWin = window.__TICKETING_API_ORIGIN__;
    if (typeof fromWin === 'string' && fromWin.trim()) {
      return normalizeTicketingApiOrigin(fromWin);
    }

    try {
      const fromLs = window.localStorage && window.localStorage.getItem('ticketing-api-origin');
      if (typeof fromLs === 'string' && fromLs.trim()) {
        return normalizeTicketingApiOrigin(fromLs);
      }
    } catch (e) {
      /* ignore */
    }

    try {
      const meta = document.querySelector('meta[name="ticketing-api-origin"]');
      const c = meta && meta.getAttribute('content');
      if (typeof c === 'string' && c.trim()) {
        return normalizeTicketingApiOrigin(c);
      }
    } catch (e) {
      /* ignore */
    }

    const cfg = window.APP_CONFIG || {};
    if (typeof cfg.apiOrigin === 'string' && cfg.apiOrigin.trim()) {
      return normalizeTicketingApiOrigin(cfg.apiOrigin);
    }

    return '';
  }

  function resolveTicketingApiUrl(url) {
    const u = String(url || '');
    if (!u.startsWith('/api/')) return u;
    const origin = getTicketingApiOrigin();
    if (!origin) return u;
    return `${origin}${u}`;
  }

  function clearTransientUi() {
    removeNodeById('login-modal-overlay');
    removeNodeById('main-video-modal');
    removeNodeById('theaters-booking-detail-overlay');
    removeNodeById('theaters-booking-calendar-overlay');
    document.body.classList.remove('login-modal-open');
    document.body.classList.remove('main-video-modal-open');
    document.body.classList.remove('theaters-booking-modal-open');
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.width = '';
    modalState.locked = false;
    modalState.savedScrollY = 0;
  }

  async function requestJson(url, options) {
    const opts = options || {};
    const resolvedPath = resolveTicketingApiUrl(url);
    const method = String(opts.method || 'GET').toUpperCase();
    const headers = { ...(opts.headers || {}) };
    const noAuth = opts && opts.noAuth === true;
    if (!noAuth) {
      try {
        const tok = typeof window.__TICKETING_AUTH_BEARER_TOKEN__ === 'string'
          ? window.__TICKETING_AUTH_BEARER_TOKEN__.trim()
          : '';
        if (tok && !headers.Authorization && !headers.authorization) {
          headers.Authorization = `Bearer ${tok}`;
        }
      } catch (e) {
        /* ignore */
      }
    }
    // GET/HEAD with Content-Type: application/json triggers a CORS preflight; ALB/배포 이슈 시 OPTIONS가
    // ACAO 없이 실패할 수 있어, 본문 없는 읽기 요청은 simple request로 보냄 (응답 CORS는 그대로 검사됨).
    const hasBody = opts.body !== undefined && opts.body !== null;
    const wantsJsonContentType = hasBody || ['POST', 'PUT', 'PATCH'].includes(method);
    if (wantsJsonContentType && !headers['Content-Type'] && !headers['content-type']) {
      headers['Content-Type'] = 'application/json';
    }

    const requestUrl = opts.query
      ? buildUrl(resolvedPath, opts.query)
      : /^https?:\/\//i.test(resolvedPath)
        ? resolvedPath
        : new URL(resolvedPath, window.location.origin).toString();
    const fetchOptions = {
      method,
      credentials: opts.credentials || 'omit',
      cache: opts.cache || 'default',
      headers
    };

    if (opts.body !== undefined && opts.body !== null) {
      fetchOptions.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
    }

    let response;
    try {
      response = await fetch(requestUrl, fetchOptions);
    } catch (error) {
      const networkError = new Error('서버에 연결할 수 없습니다.');
      networkError.cause = error;
      throw networkError;
    }

    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }

    if (!response.ok) {
      // 401 → attempt Cognito token refresh and retry once
      if (response.status === 401 && !noAuth && !opts.__retried && window.CognitoAuth) {
        try {
          await window.CognitoAuth.refreshToken();
          return requestJson(url, { ...opts, __retried: true });
        } catch (refreshErr) {
          // refresh failed → clear session, redirect to login
          if (window.CognitoAuth) window.CognitoAuth.clearTokens();
          if (typeof runtime.clearLoginUser === 'function') runtime.clearLoginUser();
          if (typeof window.openLoginPage === 'function') window.openLoginPage();
          const expired = new Error('인증이 만료되었습니다. 다시 로그인해 주세요.');
          expired.status = 401;
          expired.data = data;
          throw expired;
        }
      }

      const message = data && typeof data === 'object' && data.message
        ? data.message
        : `HTTP ${response.status}`;
      const httpError = new Error(message);
      httpError.status = response.status;
      httpError.data = data;
      throw httpError;
    }

    return data;
  }

  function getJson(url, options) {
    return requestJson(url, { method: 'GET', ...(options || {}) });
  }

  function postJson(url, body, options) {
    return requestJson(url, { method: 'POST', body, ...(options || {}) });
  }

  runtime.buildUrl = buildUrl;
  runtime.ensureStyle = ensureStyle;
  runtime.ensureScript = ensureScript;
  runtime.prefetchScript = prefetchScript;
  runtime.prefetchScripts = prefetchScripts;
  runtime.getLoginUser = getLoginUser;
  runtime.setLoginUser = setLoginUser;
  runtime.patchLoginUser = patchLoginUser;
  runtime.clearLoginUser = clearLoginUser;
  runtime.getStoredUserId = getStoredUserId;
  runtime.setStoredUserId = setStoredUserId;
  runtime.getRememberedLoginId = getRememberedLoginId;
  runtime.saveRememberedLoginId = saveRememberedLoginId;
  runtime.clearRememberedLoginId = clearRememberedLoginId;
  runtime.ensureMainBody = ensureMainBody;
  runtime.clearMainBody = clearMainBody;
  runtime.removeNodeById = removeNodeById;
  runtime.resetPrimarySections = resetPrimarySections;
  runtime.lockBodyScroll = lockBodyScroll;
  runtime.unlockBodyScroll = unlockBodyScroll;
  runtime.clearTransientUi = clearTransientUi;
  runtime.requestJson = requestJson;
  runtime.getJson = getJson;
  runtime.postJson = postJson;
  runtime.getTicketingApiOrigin = getTicketingApiOrigin;
  runtime.resolveTicketingApiUrl = resolveTicketingApiUrl;
  runtime.ensureTicketingEndpointsLoaded = ensureTicketingEndpointsLoaded;

  window.TICKETING_READ_CACHE_CHANNEL = window.TICKETING_READ_CACHE_CHANNEL || 'ticketing-cache';

  function notifyReadCacheRebuilt() {
    window.dispatchEvent(new CustomEvent('ticketing-cache-rebuilt'));
    try {
      const bc = new BroadcastChannel(window.TICKETING_READ_CACHE_CHANNEL);
      bc.postMessage({ type: 'rebuilt', t: Date.now() });
      bc.close();
    } catch (error) {
      /* BroadcastChannel unavailable */
    }
  }

  runtime.notifyReadCacheRebuilt = notifyReadCacheRebuilt;

  /**
   * Cognito 세션이 살아있는데 localStorage.user_id 가 int 가 아니면
   * (= 옛 버전에서 저장된 Cognito sub UUID) /api/read/auth/me 로 DB int user_id 를
   * 받아와 덮어씀. 예매·대기열 payload.user_id 가 항상 int 가 되도록 보장.
   */
  async function healStaleUserId() {
    try {
      if (!window.CognitoAuth || !window.CognitoAuth.isLoggedIn()) return;
      const stored = (localStorage.getItem('user_id') || '').trim();
      if (stored && /^\d+$/.test(stored)) return;
      const me = await getJson('/api/read/auth/me');
      if (me && me.user && me.user.user_id) {
        setStoredUserId(me.user.user_id);
        const current = getLoginUser() || {};
        setLoginUser({ ...current, user_id: me.user.user_id }, { preserveExpires: true });
      }
    } catch (e) { /* 네트워크/401 → 다음 로그인 시 치유 */ }
  }
  setTimeout(healStaleUserId, 0);
})();

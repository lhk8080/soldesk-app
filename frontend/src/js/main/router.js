(function () {
  const ROOT_PATHS = new Set(['/', '/index.html']);
  const LEGACY_DETAIL_PATH = '/movie/detail.html';
  const ROUTE_LOADING_ID = 'app-route-loading';
  const ROUTE_LOADING_STYLE_ID = 'app-route-loading-style';

  const SCRIPT_PATHS = {
    home: '/js/main/home.js',
    movieMain: '/js/movie/movie_main.js',
    movieDetail: '/js/movie/movie_detail.js',
    footer: '/js/main/body3.js',
    mypage: '/js/user/mypage.js',
    edit: '/js/user/edit.js',
    changepw: '/js/user/changepw.js',
    theatersMain: '/js/theaters/theaters_main.js',
    theatersDetail: '/js/theaters/theaters_detail.js',
    bookingHistory: '/js/user/booking_history.js',
    concertMain: '/js/concert/concert_main.js',
    concertDetail: '/js/concert/concert_detail.js',
    concertBooking: '/js/concert/concert_booking.js',
    concertBookingModal: '/js/concert/concert_booking_modal.js',
  };

  const VIEW_ROUTES = {
    mypage: {
      scripts: [SCRIPT_PATHS.mypage],
      action: 'openMyPage',
      prefetch: [SCRIPT_PATHS.edit, SCRIPT_PATHS.changepw]
    },
    edit: {
      scripts: [SCRIPT_PATHS.mypage, SCRIPT_PATHS.edit],
      action: 'openUserEdit',
      prefetch: [SCRIPT_PATHS.changepw]
    },
    changepw: {
      scripts: [SCRIPT_PATHS.mypage, SCRIPT_PATHS.changepw],
      action: 'openChangePw',
      prefetch: [SCRIPT_PATHS.edit]
    },
    booking_history: {
      scripts: [SCRIPT_PATHS.bookingHistory],
      action: 'openBookingHistory',
      prefetch: [SCRIPT_PATHS.mypage]
    },
    booking: {
      scripts: [SCRIPT_PATHS.theatersMain],
      action: 'openTheatersMain',
      prefetch: [SCRIPT_PATHS.theatersDetail]
    },
    concert_booking: {
      scripts: [SCRIPT_PATHS.concertBooking],
      action: 'openConcertBookingFromRouter',
      prefetch: [SCRIPT_PATHS.concertBookingModal]
    },
  };

  const ROUTE_SPINNER_DELAY_MS = 1200;
  const BACKGROUND_PREFETCH_DELAY_MS = 1400;

  const runtime = window.APP_RUNTIME || {};
  const ensureScript = runtime.ensureScript || (async () => null);
  const prefetchScripts = runtime.prefetchScripts || function () {};

  let routeJob = Promise.resolve();
  let routeLoadingSpinnerTimer = null;
  let routeLoadingToken = 0;

  function normalizePathname(pathname) {
    if (ROOT_PATHS.has(pathname)) return '/';
    if (pathname === LEGACY_DETAIL_PATH) return '/';
    return pathname;
  }

  function toPositiveInt(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : null;
  }

  function getRouteFromUrl(url = new URL(window.location.href)) {
    const pathname = normalizePathname(url.pathname);
    const params = url.searchParams;

    return {
      pathname,
      page: toPositiveInt(params.get('page')),
      movie_id: toPositiveInt(params.get('movie_id')),
      q: String(params.get('q') || '').trim(),
      view: String(params.get('view') || '').trim(),
      c_page: toPositiveInt(params.get('c_page')),
      c_q: String(params.get('c_q') || '').trim(),
      concert_id: toPositiveInt(params.get('concert_id'))
    };
  }

  function buildUrlFromRoute(route = {}) {
    const url = new URL('/', window.location.origin);

    const view = String(route.view || '').trim();
    if (view) {
      url.searchParams.set('view', view);
    }

    const page = toPositiveInt(route.page);
    if (page) {
      url.searchParams.set('page', String(page));
    }

    const q = String(route.q || '').trim();
    if (q) {
      url.searchParams.set('q', q);
    }

    const movieId = toPositiveInt(route.movie_id);
    if (movieId) {
      url.searchParams.set('movie_id', String(movieId));
    }

    const cPage = toPositiveInt(route.c_page);
    if (cPage) {
      url.searchParams.set('c_page', String(cPage));
    }

    const cQ = String(route.c_q || '').trim();
    if (cQ) {
      url.searchParams.set('c_q', cQ);
    }

    const concertId = toPositiveInt(route.concert_id);
    if (concertId) {
      url.searchParams.set('concert_id', String(concertId));
    }

    return `${url.pathname}${url.search}`;
  }

  function normalizeLegacyUrlIfNeeded() {
    const url = new URL(window.location.href);
    if (url.pathname !== LEGACY_DETAIL_PATH) return;

    const next = buildUrlFromRoute({
      page: toPositiveInt(url.searchParams.get('page')),
      q: String(url.searchParams.get('q') || '').trim(),
      movie_id: toPositiveInt(url.searchParams.get('movie_id'))
    });

    window.history.replaceState({ __app: true }, '', next);
  }

  function ensureRouteLoadingStyle() {
    if (document.getElementById(ROUTE_LOADING_STYLE_ID)) return;

    const style = document.createElement('style');
    style.id = ROUTE_LOADING_STYLE_ID;
    style.textContent = `
      #${ROUTE_LOADING_ID} {
        background: #ffffff;
        display: flex;
        align-items: center;
        justify-content: center;
        width: 100%;
        box-sizing: border-box;
      }

      .app-route-loading-inner {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 14px;
        padding: 40px 20px;
      }

      .app-route-loading-indicator {
        display: none;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 14px;
      }

      .app-route-loading-indicator.is-visible {
        display: flex;
      }

      .app-route-loading-spinner {
        width: 42px;
        height: 42px;
        border: 4px solid #e5e7eb;
        border-top-color: #111827;
        border-radius: 50%;
        animation: app-route-spin 0.8s linear infinite;
      }

      .app-route-loading-text {
        font-size: 14px;
        line-height: 1.4;
        color: #6b7280;
      }

      @keyframes app-route-spin {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
      }
    `;

    document.head.appendChild(style);
  }

  function getHeaderHeight() {
    const siteHeader = document.getElementById('site-header');
    if (!siteHeader) return 0;
    return Math.ceil(siteHeader.getBoundingClientRect().height || 0);
  }

  function getRouteLoadingHeight() {
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 800;
    const headerHeight = getHeaderHeight();
    return Math.max(viewportHeight - headerHeight, 420);
  }

  function ensureRouteLoadingLayer() {
    let layer = document.getElementById(ROUTE_LOADING_ID);
    if (layer) return layer;

    layer = document.createElement('section');
    layer.id = ROUTE_LOADING_ID;
    layer.innerHTML = `
      <div class="app-route-loading-inner" aria-live="polite" aria-busy="true">
        <div class="app-route-loading-indicator" data-route-loading-indicator>
          <div class="app-route-loading-spinner" aria-hidden="true"></div>
          <div class="app-route-loading-text">불러오는 중...</div>
        </div>
      </div>
    `;

    const siteHeader = document.getElementById('site-header');
    const mainBody = runtime.ensureMainBody ? runtime.ensureMainBody() : document.getElementById('main-body');

    if (siteHeader && siteHeader.parentNode) {
      if (mainBody && mainBody.parentNode === siteHeader.parentNode) {
        siteHeader.parentNode.insertBefore(layer, mainBody);
      } else if (siteHeader.nextSibling) {
        siteHeader.parentNode.insertBefore(layer, siteHeader.nextSibling);
      } else {
        siteHeader.parentNode.appendChild(layer);
      }
    } else {
      document.body.insertBefore(layer, document.body.firstChild);
    }

    return layer;
  }

  function clearRouteLoadingSpinnerTimer() {
    if (!routeLoadingSpinnerTimer) return;
    clearTimeout(routeLoadingSpinnerTimer);
    routeLoadingSpinnerTimer = null;
  }

  function showRouteLoading() {
    ensureRouteLoadingStyle();
    const layer = ensureRouteLoadingLayer();
    const indicator = layer.querySelector('[data-route-loading-indicator]');

    clearRouteLoadingSpinnerTimer();
    routeLoadingToken += 1;
    const currentToken = routeLoadingToken;

    layer.style.display = 'flex';
    layer.style.minHeight = `${getRouteLoadingHeight()}px`;

    if (indicator) {
      indicator.classList.remove('is-visible');
    }

    routeLoadingSpinnerTimer = window.setTimeout(() => {
      const activeLayer = document.getElementById(ROUTE_LOADING_ID);
      if (!activeLayer) return;
      if (currentToken !== routeLoadingToken) return;

      const activeIndicator = activeLayer.querySelector('[data-route-loading-indicator]');
      if (activeIndicator) {
        activeIndicator.classList.add('is-visible');
      }
    }, ROUTE_SPINNER_DELAY_MS);
  }

  function hideRouteLoading() {
    clearRouteLoadingSpinnerTimer();
    const layer = document.getElementById(ROUTE_LOADING_ID);
    if (layer) layer.remove();
  }

  async function ensureFooter() {
    await ensureScript(SCRIPT_PATHS.footer);

    if (typeof window.renderSiteFooter === 'function') {
      await window.renderSiteFooter();
      return;
    }

    if (typeof window.renderMainBody3 === 'function') {
      await window.renderMainBody3();
    }
  }

  async function runPageAction(functionName, args = { fromRouter: true }) {
    const fn = window[functionName];
    if (typeof fn !== 'function') {
      throw new Error(`route action missing: ${functionName}`);
    }
    return await fn(args);
  }

  async function renderViewRoute(view, route) {
    const config = VIEW_ROUTES[view];
    if (!config) {
      window.history.replaceState({ __app: true }, '', '/');
      return renderRoute();
    }

    if (runtime.resetPrimarySections) {
      runtime.resetPrimarySections();
    }

    for (const scriptPath of config.scripts) {
      await ensureScript(scriptPath);
    }

    if (Array.isArray(config.prefetch) && config.prefetch.length) {
      prefetchScripts(config.prefetch);
    }

    await runPageAction(config.action, { fromRouter: true, route: route || getRouteFromUrl() });
    await ensureFooter();
  }

  async function renderMovieRoute(route) {
    if (runtime.resetPrimarySections) {
      runtime.resetPrimarySections();
    }

    await ensureScript(SCRIPT_PATHS.movieMain);
    prefetchScripts([SCRIPT_PATHS.movieDetail]);

    if (route.movie_id) {
      await ensureScript(SCRIPT_PATHS.movieDetail);
      await runPageAction('renderMovieDetail');
    } else {
      await runPageAction('renderMovieMain');
    }

    await ensureFooter();
  }

  async function renderConcertRoute(route) {
    if (runtime.resetPrimarySections) {
      runtime.resetPrimarySections();
    }

    await ensureScript(SCRIPT_PATHS.concertMain);
    prefetchScripts([
      SCRIPT_PATHS.concertDetail,
      SCRIPT_PATHS.concertBooking,
      SCRIPT_PATHS.concertBookingModal
    ]);

    if (route.concert_id) {
      await ensureScript(SCRIPT_PATHS.concertDetail);
      await runPageAction('renderConcertDetail');
    } else {
      await runPageAction('renderConcertMain');
    }

    await ensureFooter();
  }

  async function renderHomeRoute() {
    if (runtime.resetPrimarySections) {
      runtime.resetPrimarySections();
    }

    await ensureScript(SCRIPT_PATHS.home);
    prefetchScripts([SCRIPT_PATHS.footer, SCRIPT_PATHS.movieMain, SCRIPT_PATHS.mypage]);
    await runPageAction('renderHomePage');
    await ensureFooter();
  }

  async function renderRoute() {
    normalizeLegacyUrlIfNeeded();

    const route = getRouteFromUrl();
    if (runtime.clearTransientUi) {
      runtime.clearTransientUi();
    }

    if (!ROOT_PATHS.has(route.pathname) && route.pathname !== '/') {
      window.history.replaceState({ __app: true }, '', '/');
      return renderRoute();
    }

    if (route.view) {
      return renderViewRoute(route.view, route);
    }

    // movie_id가 있으면 콘서트 쿼리(c_page 등)와 무관하게 영화 상세를 우선한다.
    // (콘서트 목록을 본 뒤 URL에 c_page가 남은 채 영화 카드를 누르면 상세가 망가지던 문제)
    if (route.movie_id) {
      return renderMovieRoute(route);
    }

    if (route.concert_id || route.c_page || route.c_q) {
      return renderConcertRoute(route);
    }

    if (route.page || route.q) {
      return renderMovieRoute(route);
    }

    return renderHomeRoute();
  }

  function queueRender() {
    routeJob = routeJob
      .catch(() => {})
      .then(async () => {
        showRouteLoading();
        try {
          await renderRoute();
        } finally {
          hideRouteLoading();
        }
      })
      .catch((error) => {
        hideRouteLoading();
        console.error('[router] render error:', error);
      });

    return routeJob;
  }

  function navigate(route = {}, options = {}) {
    const replace = options.replace === true;
    const nextUrl = typeof route === 'string' ? route : buildUrlFromRoute(route);

    if (replace) {
      window.history.replaceState({ __app: true }, '', nextUrl);
    } else {
      window.history.pushState({ __app: true }, '', nextUrl);
    }

    return queueRender();
  }

  function boot() {
    queueRender();

    const warm = function () {
      prefetchScripts([
        SCRIPT_PATHS.footer,
        SCRIPT_PATHS.mypage,
        SCRIPT_PATHS.movieMain,
        SCRIPT_PATHS.theatersMain,
        SCRIPT_PATHS.concertMain
      ]);
    };

    if ('requestIdleCallback' in window) {
      window.requestIdleCallback(warm, { timeout: BACKGROUND_PREFETCH_DELAY_MS });
    } else {
      window.setTimeout(warm, BACKGROUND_PREFETCH_DELAY_MS);
    }
  }

  window.appEnsureScript = ensureScript;
  window.appPrefetchScript = runtime.prefetchScript || function () {};
  window.appPrefetchScripts = prefetchScripts;
  window.appGetRoute = getRouteFromUrl;
  window.appBuildUrl = buildUrlFromRoute;
  window.appNavigate = navigate;
  window.appRenderCurrentRoute = queueRender;
  window.appShowRouteLoading = showRouteLoading;
  window.appHideRouteLoading = hideRouteLoading;

  window.addEventListener('popstate', function () {
    queueRender();
  });

  window.addEventListener('resize', function () {
    const layer = document.getElementById(ROUTE_LOADING_ID);
    if (!layer) return;
    layer.style.minHeight = `${getRouteLoadingHeight()}px`;
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

(function () {
  const BODY_JS_PATH = '/js/main/body.js';
  const BODY2_JS_PATH = '/js/main/body2.js';
  const NEXT_ROUTE_PREFETCH = ['/js/movie/movie_main.js'];
  const runtime = window.APP_RUNTIME || {};

  async function renderHomePage() {
    if (runtime.resetPrimarySections) {
      runtime.resetPrimarySections();
    }

    if (typeof window.appEnsureScript === 'function') {
      await window.appEnsureScript(BODY_JS_PATH);
      await window.appEnsureScript(BODY2_JS_PATH);
    }

    if (typeof window.appPrefetchScripts === 'function') {
      window.appPrefetchScripts(NEXT_ROUTE_PREFETCH);
    }

    if (typeof window.renderMainBody === 'function') {
      await window.renderMainBody();
    }

    if (typeof window.renderMainBody2 === 'function') {
      await window.renderMainBody2();
    }

    window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
  }

  window.renderHomePage = renderHomePage;
})();

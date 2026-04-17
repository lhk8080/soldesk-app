(function () {
  const BODY2_CSS_PATH = '/css/main/body2.css';
  const MOVIE_DETAIL_JS_PATH = '/js/movie/movie_detail.js';


  function ensureBody2Css() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureStyle === 'function') {
      return window.APP_RUNTIME.ensureStyle(BODY2_CSS_PATH);
    }

    const exists = document.querySelector(`link[href="${BODY2_CSS_PATH}"]`);
    if (exists) return Promise.resolve(exists);

    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = BODY2_CSS_PATH;
    document.head.appendChild(link);
    return Promise.resolve(link);
  }

  function ensureMountPoint() {
    let mount = document.getElementById('main-body2');
    if (mount) return mount;

    mount = document.createElement('section');
    mount.id = 'main-body2';

    const mainBody = document.getElementById('main-body');
    if (mainBody && mainBody.parentNode) {
      if (mainBody.nextSibling) {
        mainBody.parentNode.insertBefore(mount, mainBody.nextSibling);
      } else {
        mainBody.parentNode.appendChild(mount);
      }
    } else {
      document.body.appendChild(mount);
    }

    return mount;
  }


  function resolvePosterUrl(movie) {
    if (typeof window.resolveImageUrl === 'function') {
      return window.resolveImageUrl(movie.poster_url || '');
    }

    return String(movie.poster_url || '').trim();
  }

  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function formatAudienceCount(value) {
    return Number(value || 0).toLocaleString();
  }

  function formatCurrentTimeLabel() {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    const hours = String(now.getHours()).padStart(2, '0');
    const minutes = String(now.getMinutes()).padStart(2, '0');

    return `${month}.${day} ${hours}:${minutes} 기준`;
  }

  function getTimeBadgeHtml() {
    return `
      <span class="main-body2-time-badge" aria-label="현재 시간 기준">
        <svg class="main-body2-time-icon" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="2"></circle>
          <path d="M12 7.5v5l3.5 2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path>
        </svg>
        <span class="main-body2-time-text">${formatCurrentTimeLabel()}</span>
      </span>
    `;
  }

  
  async function goToMovieDetail(movieId) {
    if (!movieId) return;

    if (typeof window.appNavigate === 'function') {
      await window.appNavigate({ movie_id: movieId });
      return;
    }

    window.location.href = `/?movie_id=${movieId}`;
  }

  async function loadRankedMovies(bustCache) {
    const fetchOpts = bustCache ? { cache: 'no-store' } : {};
    const response = await readApi('/movies', fetchOpts);
    if (!Array.isArray(response)) return [];

    return response
      .filter(movie => movie.status === 'ACTIVE')
      .sort((a, b) => Number(b.audience_count || 0) - Number(a.audience_count || 0))
      .slice(0, 4);
  }

  function createMovieCard(movie, index) {
    const rank = index + 1;
    const posterUrl = resolvePosterUrl(movie);

    const article = document.createElement('article');
    article.className = 'main-body2-card';
    article.addEventListener('click', async () => {
      try {
        await goToMovieDetail(movie.movie_id);
      } catch (error) {
        console.error(error);
        alert('영화 상세정보를 불러오지 못했습니다.');
      }
    });

    article.innerHTML = `
      <div class="main-body2-poster-wrap">
        <img
          class="main-body2-poster"
          src="${posterUrl}"
          alt="${escapeHtml(movie.title)}"
          loading="lazy"
        >
        <div class="main-body2-rank">${rank}</div>
      </div>

      <div class="main-body2-info">
        <div class="main-body2-title">${escapeHtml(movie.title)}</div>
        <div class="main-body2-meta">누적관객 ${formatAudienceCount(movie.audience_count)}명</div>
      </div>
    `;

    const img = article.querySelector('.main-body2-poster');
    img.onerror = function () {
      this.src = typeof window.getFallbackImageUrl === 'function'
        ? window.getFallbackImageUrl()
        : '/images/posters/no-image.png';
    };

    return article;
  }

  async function mountBody2(options) {
    const bustCache = options && options.bustCache;
    await ensureBody2Css();

    if (typeof window.appPrefetchScripts === 'function') {
      window.appPrefetchScripts([MOVIE_DETAIL_JS_PATH]);
    }

    const mount = ensureMountPoint();
    mount.innerHTML = `
      <div class="main-body2-wrap">
        <div class="main-body2-loading">영화 순위를 불러오는 중...</div>
      </div>
    `;

    try {
      const movies = await loadRankedMovies(bustCache);

      if (!movies.length) {
        mount.innerHTML = `
          <div class="main-body2-wrap">
            <div class="main-body2-empty">표시할 순위 영화가 없습니다.</div>
          </div>
        `;
        return;
      }

      const wrap = document.createElement('div');
      wrap.className = 'main-body2-wrap';

      const section = document.createElement('section');
      section.className = 'main-body2-section';

      const header = document.createElement('div');
      header.className = 'main-body2-header';
      header.innerHTML = `
        <h3 class="main-body2-heading">인기 영화</h3>
        ${getTimeBadgeHtml()}
      `;

      const grid = document.createElement('div');
      grid.className = 'main-body2-grid';

      movies.forEach((movie, index) => {
        grid.appendChild(createMovieCard(movie, index));
      });

      section.appendChild(header);
      section.appendChild(grid);
      wrap.appendChild(section);

      mount.innerHTML = '';
      mount.appendChild(wrap);
    } catch (e) {
      console.error(e);
      mount.innerHTML = `
        <div class="main-body2-wrap">
          <div class="main-body2-error">영화 순위 영역을 불러오지 못했습니다.</div>
        </div>
      `;
    }
  }

  window.renderMainBody2 = mountBody2;

  function attachReadCacheRebuildListeners() {
    const ch = window.TICKETING_READ_CACHE_CHANNEL || 'ticketing-cache';
    const run = () => {
      const wrap = document.querySelector('#main-body2 .main-body2-wrap');
      if (!wrap || wrap.querySelector('.main-body2-loading')) return;
      if (typeof window.renderMainBody2 === 'function') {
        window.renderMainBody2({ bustCache: true });
      }
    };
    window.addEventListener('ticketing-cache-rebuilt', run);
    try {
      const bc = new BroadcastChannel(ch);
      bc.onmessage = (ev) => {
        if (ev.data && ev.data.type === 'rebuilt') run();
      };
    } catch (error) {
      /* ignore */
    }
  }
  attachReadCacheRebuildListeners();
})();
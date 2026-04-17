(function () {
  const MOVIE_MAIN_CSS_PATH = '/css/movie/movie_main.css';
  const MOVIES_PER_PAGE = 10;

  let allMovies = [];
  let currentKeyword = '';

  function ensureMovieMainCss() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureStyle === 'function') {
      return window.APP_RUNTIME.ensureStyle(MOVIE_MAIN_CSS_PATH);
    }

    const exists = document.querySelector(`link[href="${MOVIE_MAIN_CSS_PATH}"]`);
    if (exists) return Promise.resolve(exists);

    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = MOVIE_MAIN_CSS_PATH;
    document.head.appendChild(link);
    return Promise.resolve(link);
  }

  function ensureMountPoint() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureMainBody === 'function') {
      return window.APP_RUNTIME.ensureMainBody();
    }

    let mount = document.getElementById('main-body');

    if (!mount) {
      mount = document.createElement('div');
      mount.id = 'main-body';
      document.body.appendChild(mount);
    }

    return mount;
  }

  function removeSection(id) {
    const node = document.getElementById(id);
    if (node) node.remove();
  }

  function clearMainPageSections() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.resetPrimarySections === 'function') {
      window.APP_RUNTIME.resetPrimarySections();
      return;
    }

    const mainBody = ensureMountPoint();
    mainBody.innerHTML = '';
    mainBody.style.display = '';
    removeSection('main-body2');
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
    return Number(value || 0).toLocaleString('ko-KR');
  }

  function formatRuntime(value) {
    const minutes = Number(value || 0);
    return `${minutes}분`;
  }

  function getRoute() {
    if (typeof window.appGetRoute === 'function') {
      return window.appGetRoute();
    }

    const params = new URLSearchParams(window.location.search);
    const page = Number(params.get('page'));
    const movieId = Number(params.get('movie_id'));

    return {
      page: Number.isFinite(page) && page > 0 ? page : 1,
      movie_id: Number.isFinite(movieId) && movieId > 0 ? movieId : null,
      q: String(params.get('q') || '').trim()
    };
  }

  /** 라우터 기준: 지금 URL이 영화 «목록 그리드»일 때만 true (콘서트·상세·예매·홈과 구분) */
  function isMovieMainListRoute() {
    if (typeof window.appGetRoute !== 'function') return false;
    const r = window.appGetRoute();
    if (String(r.view || '').trim()) return false;
    if (r.movie_id) return false;
    if (r.concert_id || r.c_page || String(r.c_q || '').trim()) return false;
    const p = Number(r.page);
    if (Number.isFinite(p) && p > 0) return true;
    if (String(r.q || '').trim()) return true;
    return false;
  }

  function normalizeSearchKeyword(value) {
    return String(value || '')
      .trim()
      .replace(/\s+/g, '')
      .toLowerCase();
  }

  function isMovieVisibleInMainList(movie) {
    const hideOk = String(movie.hide || 'N').toUpperCase() === 'N';
    if (!hideOk) return false;
    if (String(movie.status || '').toUpperCase() === 'ACTIVE') return true;
    const title = String(movie.title || '').trim();
    const synopsis = String(movie.synopsis || '').trim();
    return title.startsWith('더미데이터') || synopsis.startsWith('더미데이터');
  }

  function applySearch(keyword) {
    currentKeyword = String(keyword || '').trim();
    const normalizedKeyword = normalizeSearchKeyword(currentKeyword);

    if (!normalizedKeyword) {
      return [...allMovies];
    }

    return allMovies.filter((movie) => {
      const normalizedTitle = normalizeSearchKeyword(movie.title || '');
      const normalizedSynopsis = normalizeSearchKeyword(movie.synopsis || '');
      return (
        normalizedTitle.includes(normalizedKeyword) ||
        normalizedSynopsis.includes(normalizedKeyword)
      );
    });
  }

  async function loadMovies(bustCache) {
    const fetchOpts = bustCache ? { cache: 'no-store' } : {};
    const response = await readApi('/movies', fetchOpts);
    if (!Array.isArray(response)) return [];

    const dedup = new Map();
    response.forEach((movie) => {
      if (!movie) return;
      const id = Number(movie.movie_id || 0);
      if (!Number.isFinite(id) || id <= 0) return;
      if (!isMovieVisibleInMainList(movie)) return;
      if (!dedup.has(id)) dedup.set(id, movie);
    });

    return Array.from(dedup.values())
      .sort((a, b) => Number(a.movie_id || 0) - Number(b.movie_id || 0));
  }

  function goToMovieDetail(movieId, currentPage) {
    if (!movieId) return;

    if (typeof window.appNavigate === 'function') {
      window.appNavigate({
        page: currentPage,
        q: currentKeyword,
        movie_id: movieId
      });
      return;
    }

    const url = new URL('/', window.location.origin);
    url.searchParams.set('page', String(currentPage));
    if (currentKeyword) {
      url.searchParams.set('q', currentKeyword);
    }
    url.searchParams.set('movie_id', String(movieId));
    window.location.href = `${url.pathname}${url.search}`;
  }

  function createMovieCard(movie, currentPage) {
    const article = document.createElement('article');
    article.className = 'movie-main-card';
    article.addEventListener('click', function () {
      goToMovieDetail(movie.movie_id, currentPage);
    });

    const posterUrl = resolvePosterUrl(movie);

    article.innerHTML = `
      <div class="movie-main-poster-shell">
        <img
          class="movie-main-poster"
          src="${posterUrl}"
          alt="${escapeHtml(movie.title)}"
          loading="lazy"
        >
      </div>

      <div class="movie-main-info-box">
        <div class="movie-main-title-row">
          <div class="movie-main-title">${escapeHtml(movie.title)}</div>
          <div class="movie-main-runtime">⏰ ${escapeHtml(formatRuntime(movie.runtime_minutes))}</div>
        </div>
        <div class="movie-main-audience">누적관객 ${formatAudienceCount(movie.audience_count)}명</div>
      </div>
    `;

    const img = article.querySelector('.movie-main-poster');
    img.onerror = function () {
      this.src = typeof window.getFallbackImageUrl === 'function'
        ? window.getFallbackImageUrl()
        : '/images/posters/no-image.png';
    };

    return article;
  }

  function createPagination(totalPages, currentPage) {
    const nav = document.createElement('div');
    nav.className = 'movie-main-pagination';

    const prevButton = document.createElement('button');
    prevButton.type = 'button';
    prevButton.className = 'movie-main-page-arrow-button';
    prevButton.textContent = '‹';
    prevButton.disabled = currentPage === 1;
    prevButton.addEventListener('click', function () {
      if (currentPage > 1 && typeof window.appNavigate === 'function') {
        window.appNavigate({
          page: currentPage - 1,
          q: currentKeyword
        });
      }
    });
    nav.appendChild(prevButton);

    const indicatorWrap = document.createElement('div');
    indicatorWrap.className = 'movie-main-indicators';

    for (let page = 1; page <= totalPages; page += 1) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `movie-main-indicator${page === currentPage ? ' active' : ''}`;
      button.setAttribute('aria-label', `${page}페이지 보기`);
      button.addEventListener('click', function () {
        if (typeof window.appNavigate === 'function') {
          window.appNavigate({
            page,
            q: currentKeyword
          });
        }
      });
      indicatorWrap.appendChild(button);
    }

    nav.appendChild(indicatorWrap);

    const nextButton = document.createElement('button');
    nextButton.type = 'button';
    nextButton.className = 'movie-main-page-arrow-button';
    nextButton.textContent = '›';
    nextButton.disabled = currentPage === totalPages;
    nextButton.addEventListener('click', function () {
      if (currentPage < totalPages && typeof window.appNavigate === 'function') {
        window.appNavigate({
          page: currentPage + 1,
          q: currentKeyword
        });
      }
    });
    nav.appendChild(nextButton);

    return nav;
  }

  function createSearchBox() {
    const wrap = document.createElement('div');
    wrap.className = 'movie-main-search-wrap';

    wrap.innerHTML = `
      <form class="movie-main-search-form" id="movie-main-search-form">
        <input
          type="text"
          id="movie-main-search-input"
          class="movie-main-search-input"
          placeholder="영화 제목 검색"
          value="${escapeHtml(currentKeyword)}"
          autocomplete="off"
        >
        <button type="submit" class="movie-main-search-button">검색</button>
      </form>
    `;

    const form = wrap.querySelector('#movie-main-search-form');
    const input = wrap.querySelector('#movie-main-search-input');

    form.addEventListener('submit', function (e) {
      e.preventDefault();

      if (typeof window.appNavigate === 'function') {
        window.appNavigate({
          page: 1,
          q: input.value.trim()
        });
      }
    });

    return wrap;
  }

  function renderMovies(mount, movies, currentPage) {
    const hasMovies = movies.length > 0;
    const totalPages = hasMovies ? Math.ceil(movies.length / MOVIES_PER_PAGE) : 0;
    const safePage = hasMovies ? Math.min(Math.max(currentPage, 1), totalPages) : 1;
    const startIndex = (safePage - 1) * MOVIES_PER_PAGE;
    const pageItems = hasMovies ? movies.slice(startIndex, startIndex + MOVIES_PER_PAGE) : [];

    const wrap = document.createElement('div');
    wrap.className = 'movie-main-wrap';

    const section = document.createElement('section');
    section.className = 'movie-main-section';

    const header = document.createElement('div');
    header.className = 'movie-main-header';
    header.innerHTML = `
      <h2 class="movie-main-heading">영화</h2>
    `;

    const grid = document.createElement('div');
    grid.className = 'movie-main-grid';

    if (pageItems.length > 0) {
      pageItems.forEach((movie) => {
        grid.appendChild(createMovieCard(movie, safePage));
      });
    } else {
      grid.innerHTML = `
        <div class="movie-main-no-result">검색 결과가 없습니다.</div>
      `;
    }

    section.appendChild(header);
    section.appendChild(grid);
    wrap.appendChild(section);

    if (hasMovies) {
      wrap.appendChild(createPagination(totalPages, safePage));
    }

    wrap.appendChild(createSearchBox());

    mount.innerHTML = '';
    mount.appendChild(wrap);
  }

  async function mountMovieMain() {
    await ensureMovieMainCss();

    if (typeof window.appPrefetchScripts === 'function') {
      window.appPrefetchScripts(['/js/movie/movie_detail.js']);
    }
    clearMainPageSections();

    const mount = ensureMountPoint();
    mount.innerHTML = `
      <div class="movie-main-wrap">
        <div class="movie-main-loading">영화 목록을 불러오는 중...</div>
      </div>
    `;

    try {
      // 예매/쓰기 직후 돌아올 때 브라우저 캐시된 GET /movies 를 쓰지 않도록 콘서트 쪽과 같이 no-store
      allMovies = await loadMovies(true);
      const route = getRoute();
      currentKeyword = route.q || '';
      const filteredMovies = applySearch(currentKeyword);

      if (!allMovies.length) {
        mount.innerHTML = `
          <div class="movie-main-wrap">
            <div class="movie-main-empty">표시할 영화가 없습니다.</div>
          </div>
        `;
        return;
      }

      renderMovies(mount, filteredMovies, route.page || 1);
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    } catch (e) {
      console.error(e);
      mount.innerHTML = `
        <div class="movie-main-wrap">
          <div class="movie-main-error">영화 목록을 불러오지 못했습니다.</div>
        </div>
      `;
    }
  }

  window.renderMovieMain = mountMovieMain;
  window.openMovieMainFromHeader = function () {
    if (typeof window.appNavigate === 'function') {
      window.appNavigate({ page: 1 });
      return;
    }

    window.location.href = '/?page=1';
  };
  window.handleMovieRoute = mountMovieMain;

  async function refetchMoviesAfterCacheRebuild() {
    if (!isMovieMainListRoute()) return;

    const wrap = document.querySelector('.movie-main-wrap');
    if (!wrap || wrap.querySelector('.movie-main-loading')) return;

    try {
      allMovies = await loadMovies(true);
      const route = getRoute();
      currentKeyword = route.q || '';
      const filteredMovies = applySearch(currentKeyword);

      if (!allMovies.length) {
        wrap.innerHTML = '<div class="movie-main-empty">표시할 영화가 없습니다.</div>';
        return;
      }

      const mount = ensureMountPoint();
      renderMovies(mount, filteredMovies, route.page || 1);
    } catch (error) {
      console.error('[movie] Redis 재구성 후 목록 갱신 실패:', error);
    }
  }

  function attachReadCacheRebuildListeners() {
    const ch = window.TICKETING_READ_CACHE_CHANNEL || 'ticketing-cache';
    const run = () => {
      refetchMoviesAfterCacheRebuild();
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

(function () {
  const CSS_PATH = '/css/movie/movie_main.css';
  const ITEMS_PER_PAGE = 10;

  let allItems = [];
  let currentKeyword = '';

  function ensureCss() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureStyle === 'function') {
      return window.APP_RUNTIME.ensureStyle(CSS_PATH);
    }
    const exists = document.querySelector(`link[href="${CSS_PATH}"]`);
    if (exists) return Promise.resolve(exists);
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = CSS_PATH;
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

  function clearMainPageSections() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.resetPrimarySections === 'function') {
      window.APP_RUNTIME.resetPrimarySections();
      return;
    }
    const mainBody = ensureMountPoint();
    mainBody.innerHTML = '';
    mainBody.style.display = '';
    const n = document.getElementById('main-body2');
    if (n) n.remove();
  }

  function resolvePosterUrl(item) {
    if (typeof window.resolveImageUrl === 'function') {
      return window.resolveImageUrl(item.poster_url || '');
    }
    return String(item.poster_url || '').trim();
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

  function formatRuntime(value) {
    return `${Number(value || 0)}분`;
  }

  function formatConcertDate(value) {
    if (!value) return '';
    const text = String(value).trim();
    if (!text) return '';
    // server returns "YYYY-MM-DD HH:MM:SS" (or iso-like); keep to minute
    return text.replace('T', ' ').slice(0, 16);
  }

  function getRoute() {
    if (typeof window.appGetRoute === 'function') {
      return window.appGetRoute();
    }
    const params = new URLSearchParams(window.location.search);
    const page = Number(params.get('c_page'));
    const concertId = Number(params.get('concert_id'));
    return {
      c_page: Number.isFinite(page) && page > 0 ? page : 1,
      concert_id: Number.isFinite(concertId) && concertId > 0 ? concertId : null,
      c_q: String(params.get('c_q') || '').trim()
    };
  }

  function normalizeSearchKeyword(value) {
    return String(value || '')
      .trim()
      .replace(/\s+/g, '')
      .toLowerCase();
  }

  function applySearch(keyword) {
    currentKeyword = String(keyword || '').trim();
    const nk = normalizeSearchKeyword(currentKeyword);
    if (!nk) return [...allItems];
    return allItems.filter((c) => {
      const t = normalizeSearchKeyword(c.title || '');
      return t.includes(nk);
    });
  }

  async function loadConcerts(bustCache) {
    const fetchOpts = bustCache ? { cache: 'no-store' } : {};
    const response = await readApi('/concerts', fetchOpts);
    if (!Array.isArray(response)) return [];
    return response
      .filter((c) => {
        const statusOk = String(c.status || '').toUpperCase() === 'ACTIVE';
        const hideOk = String(c.hide || 'N').toUpperCase() === 'N';
        return statusOk && hideOk;
      })
      .sort((a, b) => Number(a.concert_id || 0) - Number(b.concert_id || 0));
  }

  function goToDetail(concertId, currentPage) {
    if (!concertId) return;
    if (typeof window.appNavigate === 'function') {
      window.appNavigate({
        c_page: currentPage,
        c_q: currentKeyword,
        concert_id: concertId
      });
      return;
    }
    const url = new URL('/', window.location.origin);
    url.searchParams.set('c_page', String(currentPage));
    if (currentKeyword) url.searchParams.set('c_q', currentKeyword);
    url.searchParams.set('concert_id', String(concertId));
    window.location.href = `${url.pathname}${url.search}`;
  }

  function createCard(item, currentPage) {
    const article = document.createElement('article');
    article.className = 'movie-main-card';
    article.addEventListener('click', function () {
      goToDetail(item.concert_id, currentPage);
    });

    const posterUrl = resolvePosterUrl(item);
    const cat = String(item.category || '').toUpperCase() === 'MUSICAL' ? '뮤지컬' : '콘서트';
    const venue = escapeHtml(item.venue_summary || '');
    const nextShow = formatConcertDate(item.next_show_date);

    article.innerHTML = `
      <div class="movie-main-poster-shell">
        <img class="movie-main-poster" src="${posterUrl}" alt="${escapeHtml(item.title)}" loading="lazy">
      </div>
      <div class="movie-main-info-box">
        <div class="movie-main-title-row">
          <div class="movie-main-title">${escapeHtml(item.title)}</div>
          <div class="movie-main-runtime">${escapeHtml(cat)}</div>
        </div>
        <div class="movie-main-audience">${venue ? escapeHtml(venue) : '공연 정보'}</div>
        ${nextShow ? `<div class="movie-main-audience" style="margin-top:6px;color:#111;">${escapeHtml(nextShow)}</div>` : ''}
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
        window.appNavigate({ c_page: currentPage - 1, c_q: currentKeyword });
      }
    });
    nav.appendChild(prevButton);

    const indicatorWrap = document.createElement('div');
    indicatorWrap.className = 'movie-main-indicators';
    for (let p = 1; p <= totalPages; p += 1) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `movie-main-indicator${p === currentPage ? ' active' : ''}`;
      button.setAttribute('aria-label', `${p}페이지`);
      button.addEventListener('click', function () {
        if (typeof window.appNavigate === 'function') {
          window.appNavigate({ c_page: p, c_q: currentKeyword });
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
        window.appNavigate({ c_page: currentPage + 1, c_q: currentKeyword });
      }
    });
    nav.appendChild(nextButton);

    return nav;
  }

  function createSearchBox() {
    const wrap = document.createElement('div');
    wrap.className = 'movie-main-search-wrap';
    wrap.innerHTML = `
      <form class="movie-main-search-form" id="concert-main-search-form">
        <input type="text" id="concert-main-search-input" class="movie-main-search-input"
          placeholder="공연 제목 검색" value="${escapeHtml(currentKeyword)}" autocomplete="off">
        <button type="submit" class="movie-main-search-button">검색</button>
      </form>
    `;
    wrap.querySelector('#concert-main-search-form').addEventListener('submit', function (e) {
      e.preventDefault();
      const input = wrap.querySelector('#concert-main-search-input');
      if (typeof window.appNavigate === 'function') {
        window.appNavigate({ c_page: 1, c_q: input.value.trim() });
      }
    });
    return wrap;
  }

  function renderList(mount, items, currentPage) {
    const has = items.length > 0;
    const totalPages = has ? Math.ceil(items.length / ITEMS_PER_PAGE) : 0;
    const safePage = has ? Math.min(Math.max(currentPage, 1), totalPages) : 1;
    const start = (safePage - 1) * ITEMS_PER_PAGE;
    const pageItems = has ? items.slice(start, start + ITEMS_PER_PAGE) : [];

    const wrap = document.createElement('div');
    wrap.className = 'movie-main-wrap';

    const section = document.createElement('section');
    section.className = 'movie-main-section';
    const header = document.createElement('div');
    header.className = 'movie-main-header';
    header.innerHTML = '<h2 class="movie-main-heading">콘서트 / 뮤지컬</h2>';

    const grid = document.createElement('div');
    grid.className = 'movie-main-grid';
    if (pageItems.length) {
      pageItems.forEach((c) => grid.appendChild(createCard(c, safePage)));
    } else {
      grid.innerHTML = '<div class="movie-main-no-result">검색 결과가 없습니다.</div>';
    }

    section.appendChild(header);
    section.appendChild(grid);
    wrap.appendChild(section);
    if (has) wrap.appendChild(createPagination(totalPages, safePage));
    wrap.appendChild(createSearchBox());

    mount.innerHTML = '';
    mount.appendChild(wrap);
  }

  async function mountConcertMain() {
    await ensureCss();
    if (typeof window.appPrefetchScripts === 'function') {
      window.appPrefetchScripts(['/js/concert/concert_detail.js']);
    }
    clearMainPageSections();
    const mount = ensureMountPoint();
    mount.innerHTML =
      '<div class="movie-main-wrap"><div class="movie-main-loading">공연 목록을 불러오는 중...</div></div>';

    try {
      allItems = await loadConcerts(false);
      const route = getRoute();
      currentKeyword = route.c_q || '';
      const filtered = applySearch(currentKeyword);

      if (!allItems.length) {
        mount.innerHTML =
          '<div class="movie-main-wrap"><div class="movie-main-empty">표시할 공연이 없습니다.</div></div>';
        return;
      }

      renderList(mount, filtered, route.c_page || 1);
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    } catch (e) {
      console.error(e);
      mount.innerHTML =
        '<div class="movie-main-wrap"><div class="movie-main-error">목록을 불러오지 못했습니다.</div></div>';
    }
  }

  window.renderConcertMain = mountConcertMain;
  window.openConcertMainFromHeader = function () {
    if (typeof window.appNavigate === 'function') {
      window.appNavigate({ c_page: 1 });
      return;
    }
    window.location.href = '/?c_page=1';
  };
})();

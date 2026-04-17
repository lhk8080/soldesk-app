(function () {
  const CSS_PATH = '/css/movie/movie_detail.css';

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

  function clearPageSections() {
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

  function resolvePosterUrl(c) {
    if (typeof window.resolveImageUrl === 'function') {
      return window.resolveImageUrl(c.poster_url || '');
    }
    return String(c.poster_url || '').trim();
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

  function getRoute() {
    if (typeof window.appGetRoute === 'function') {
      return window.appGetRoute();
    }
    const params = new URLSearchParams(window.location.search);
    const concertId = Number(params.get('concert_id'));
    const page = Number(params.get('c_page'));
    return {
      concert_id: Number.isFinite(concertId) && concertId > 0 ? concertId : null,
      c_page: Number.isFinite(page) && page > 0 ? page : null,
      c_q: String(params.get('c_q') || '').trim()
    };
  }

  async function loadDetail(concertId) {
    return await readApi(`/concert/${concertId}`);
  }

  function createInfoItem(label, value) {
    return `
      <div class="movie-detail-info-item">
        <dt>${escapeHtml(label)}</dt>
        <dd>${escapeHtml(value || '-')}</dd>
      </div>
    `;
  }

  function goBack() {
    const route = getRoute();
    if (typeof window.appNavigate === 'function') {
      if (route.c_page) {
        window.appNavigate({ c_page: route.c_page, c_q: route.c_q }, { replace: true });
        return;
      }
      window.appNavigate({ c_page: 1 }, { replace: true });
      return;
    }
    window.location.href = '/?c_page=1';
  }

  function openBookingPage(concertId) {
    const route = { view: 'concert_booking' };
    const id = Number(concertId);
    if (Number.isFinite(id) && id > 0) route.concert_id = Math.trunc(id);
    if (typeof window.appNavigate === 'function') {
      window.appNavigate(route);
      return;
    }
    const url = new URL('/', window.location.origin);
    url.searchParams.set('view', 'concert_booking');
    if (route.concert_id) url.searchParams.set('concert_id', String(route.concert_id));
    window.location.href = `${url.pathname}${url.search}`;
  }

  function categoryLabel(c) {
    const u = String(c.category || '').toUpperCase();
    if (u === 'MUSICAL') return '뮤지컬';
    if (u === 'CONCERT') return '콘서트';
    return c.category || '-';
  }

  function renderView(mount, payload) {
    const concert = payload && payload.concert ? payload.concert : {};
    const posterUrl = resolvePosterUrl(concert);
    const topSynopsis = concert.synopsis || concert.synopsis_line || '소개 준비 중입니다.';
    const detailSynopsis = concert.synopsis_line || concert.synopsis || topSynopsis;

    mount.innerHTML = `
      <div class="movie-detail-page">
        <div class="movie-detail-top-row">
          <button type="button" class="movie-detail-pill-button" id="concert-detail-back-button">← 뒤로가기</button>
        </div>

        <section class="movie-detail-hero">
          <div class="movie-detail-poster-box">
            <img class="movie-detail-poster" src="${escapeHtml(posterUrl)}"
              alt="${escapeHtml(concert.title || '포스터')}" loading="eager">
          </div>
          <div class="movie-detail-summary">
            <h1 class="movie-detail-title">${escapeHtml(concert.title || '제목 없음')}</h1>
            <div class="movie-detail-meta">
              <span class="movie-detail-meta-text">${escapeHtml(concert.venue_summary || '공연장')}</span>
              <span class="movie-detail-meta-divider">|</span>
              <span class="movie-detail-meta-text">🕒 ${escapeHtml(formatRuntime(concert.runtime_minutes))}</span>
              <span class="movie-detail-meta-divider">|</span>
              <span class="movie-detail-meta-text">${escapeHtml(categoryLabel(concert))}</span>
            </div>
            <p class="movie-detail-synopsis">${escapeHtml(topSynopsis)}</p>
            <button type="button" class="movie-detail-booking-button">예매하기</button>
          </div>
        </section>

        <nav class="movie-detail-tab-bar">
          <a href="#concert-detail-info" class="movie-detail-tab active">상세정보</a>
        </nav>

        <section id="concert-detail-info" class="movie-detail-section">
          <h2 class="movie-detail-section-title">공연 정보</h2>
          <dl class="movie-detail-info-list">
            ${createInfoItem('구분', categoryLabel(concert))}
            ${createInfoItem('장르', concert.genre)}
            ${createInfoItem('공연 시간', formatRuntime(concert.runtime_minutes))}
            ${createInfoItem('주요 공연장', concert.venue_summary)}
            ${createInfoItem('상태', String(concert.status || '').toUpperCase() === 'ACTIVE' ? '예매 가능' : concert.status || '-')}
          </dl>
        </section>

        <section class="movie-detail-section">
          <h2 class="movie-detail-section-title">소개</h2>
          <p class="movie-detail-description">${escapeHtml(detailSynopsis)}</p>
        </section>
      </div>
    `;

    const poster = mount.querySelector('.movie-detail-poster');
    if (poster) {
      poster.onerror = function () {
        this.src = typeof window.getFallbackImageUrl === 'function'
          ? window.getFallbackImageUrl()
          : '/images/posters/no-image.png';
      };
    }

    mount.querySelector('#concert-detail-back-button').addEventListener('click', goBack);
    mount.querySelector('.movie-detail-booking-button').addEventListener('click', function (e) {
      e.preventDefault();
      openBookingPage(concert.concert_id);
    });
  }

  async function mountConcertDetail() {
    const route = getRoute();
    const concertId = route.concert_id;
    const mount = ensureMountPoint();

    if (!concertId) {
      mount.innerHTML =
        '<div class="movie-detail-page"><div class="movie-detail-message-box">잘못된 접근입니다.</div></div>';
      return;
    }

    await ensureCss();
    clearPageSections();
    mount.innerHTML =
      '<div class="movie-detail-page"><div class="movie-detail-message-box">불러오는 중...</div></div>';
    window.scrollTo({ top: 0, behavior: 'auto' });

    try {
      const payload = await loadDetail(concertId);
      renderView(mount, payload);
      window.scrollTo({ top: 0, behavior: 'auto' });
    } catch (error) {
      console.error(error);
      mount.innerHTML =
        '<div class="movie-detail-page"><div class="movie-detail-message-box">불러오지 못했습니다.</div></div>';
    }
  }

  window.renderConcertDetail = mountConcertDetail;
})();

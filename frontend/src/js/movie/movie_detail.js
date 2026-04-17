(function () {
  const MOVIE_DETAIL_CSS_PATH = '/css/movie/movie_detail.css';

  function ensureMovieDetailCss() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureStyle === 'function') {
      return window.APP_RUNTIME.ensureStyle(MOVIE_DETAIL_CSS_PATH);
    }

    const exists = document.querySelector(`link[href="${MOVIE_DETAIL_CSS_PATH}"]`);
    if (exists) return Promise.resolve(exists);

    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = MOVIE_DETAIL_CSS_PATH;
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

  function clearPageSections() {
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
      return window.resolveImageUrl(movie.poster_url || movie.main_poster_url || '');
    }

    return String(movie.poster_url || movie.main_poster_url || '').trim();
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

  function formatAudienceInWan(value) {
    const count = Number(value || 0);

    if (!count) return '0명';
    if (count >= 10000) return `${(count / 10000).toFixed(1)}만명`;

    return `${formatAudienceCount(count)}명`;
  }

  function formatRuntime(value) {
    return `${Number(value || 0)}분`;
  }

  function formatReleaseDate(value) {
    if (!value) return '-';

    const onlyDate = String(value).trim().slice(0, 10);
    const parts = onlyDate.split('-');

    if (parts.length !== 3) return onlyDate;

    const yy = parts[0].slice(2);
    const mm = parts[1];
    const dd = parts[2];

    return `${yy}. ${mm}. ${dd}`;
  }

  function getReleaseDateDisplay(movie) {
    if (!movie) return '-';

    const display = String(movie.release_date_display || '').trim();
    if (display) {
      return display;
    }

    return formatReleaseDate(movie.release_date);
  }

  function formatStatus(value) {
    const status = String(value || '').toUpperCase();

    if (status === 'ACTIVE') return '상영중';
    if (status === 'INACTIVE') return '비활성';

    return value || '-';
  }

  function getRoute() {
    if (typeof window.appGetRoute === 'function') {
      return window.appGetRoute();
    }

    const params = new URLSearchParams(window.location.search);
    const movieId = Number(params.get('movie_id'));
    const page = Number(params.get('page'));

    return {
      movie_id: Number.isFinite(movieId) && movieId > 0 ? movieId : null,
      page: Number.isFinite(page) && page > 0 ? page : null,
      q: String(params.get('q') || '').trim()
    };
  }

  async function loadMovieDetail(movieId) {
    if (typeof readApi !== 'function') {
      throw new Error('readApi가 없습니다.');
    }

    return await readApi(`/movies/detail/${movieId}`);
  }

  function createInfoItem(label, value) {
    return `
      <div class="movie-detail-info-item">
        <dt>${escapeHtml(label)}</dt>
        <dd>${escapeHtml(value || '-')}</dd>
      </div>
    `;
  }

  function renderLoading(mount) {
    mount.innerHTML = `
      <div class="movie-detail-page">
        <div class="movie-detail-message-box">영화 상세정보를 불러오는 중...</div>
      </div>
    `;
  }

  function renderError(mount) {
    mount.innerHTML = `
      <div class="movie-detail-page">
        <div class="movie-detail-message-box">영화 상세정보를 불러오지 못했습니다.</div>
      </div>
    `;
  }

  function renderInvalid(mount) {
    mount.innerHTML = `
      <div class="movie-detail-page">
        <div class="movie-detail-message-box">잘못된 영화 접근입니다.</div>
      </div>
    `;
  }

  function goBackToMovieMain() {
    const route = getRoute();

    if (typeof window.appNavigate === 'function') {
      if (route.page) {
        window.appNavigate({
          page: route.page,
          q: route.q
        }, { replace: true });
        return;
      }

      window.appNavigate({}, { replace: true });
      return;
    }

    if (route.page) {
      window.location.href = `/?page=${route.page}`;
      return;
    }

    window.location.href = '/';
  }

  function openBookingPage(movieId) {
    const route = { view: 'booking' };
    const normalizedMovieId = Number(movieId);

    if (Number.isFinite(normalizedMovieId) && normalizedMovieId > 0) {
      route.movie_id = Math.trunc(normalizedMovieId);
    }

    if (typeof window.appNavigate === 'function') {
      window.appNavigate(route);
      return;
    }

    const url = new URL('/', window.location.origin);
    url.searchParams.set('view', 'booking');

    if (route.movie_id) {
      url.searchParams.set('movie_id', String(route.movie_id));
    }

    window.location.href = `${url.pathname}${url.search}`;
  }

  function getMovieVideoUrl(movie) {
    if (!movie) return '';

    const candidates = [
      movie.video_url,
      movie.videoUrl,
      movie.trailer_url,
      movie.trailerUrl,
      movie.video,
      movie.video_path,
      movie.videoPath,
      movie.movie_video_url
    ];

    for (const candidate of candidates) {
      const value = String(candidate || '').trim();
      if (value) return value;
    }

    return '';
  }

  function extractYoutubeVideoId(url) {
    if (!url) return '';

    const value = String(url).trim();

    try {
      const parsed = new URL(value);

      if (parsed.hostname.includes('youtu.be')) {
        return parsed.pathname.replace('/', '').trim();
      }

      if (parsed.hostname.includes('youtube.com')) {
        if (parsed.pathname === '/watch') {
          return parsed.searchParams.get('v') || '';
        }

        if (parsed.pathname.startsWith('/embed/')) {
          return parsed.pathname.split('/embed/')[1] || '';
        }

        if (parsed.pathname.startsWith('/shorts/')) {
          return parsed.pathname.split('/shorts/')[1] || '';
        }
      }
    } catch (e) {
      console.error('[movie detail video] invalid youtube url:', e);
    }

    return '';
  }

  function getVideoRenderInfo(videoUrl) {
    if (!videoUrl) return null;

    const trimmed = String(videoUrl).trim();
    if (!trimmed) return null;

    const youtubeId = extractYoutubeVideoId(trimmed);
    if (youtubeId) {
      return {
        type: 'youtube',
        src: `https://www.youtube.com/embed/${youtubeId}?autoplay=1&rel=0&modestbranding=1`
      };
    }

    if (trimmed.startsWith('http://') || trimmed.startsWith('https://')) {
      return {
        type: 'direct',
        src: trimmed
      };
    }

    return null;
  }

  function closeVideoModal() {
    const modal = document.getElementById('main-video-modal');
    if (!modal) return;

    modal.classList.remove('open');

    const frameArea = modal.querySelector('.main-video-modal-media');
    if (frameArea) {
      frameArea.innerHTML = '';
    }

    document.body.classList.remove('main-video-modal-open');
  }

  function createVideoModal() {
    const existing = document.getElementById('main-video-modal');
    if (existing) return existing;

    const modal = document.createElement('div');
    modal.id = 'main-video-modal';
    modal.className = 'main-video-modal';

    modal.innerHTML = `
      <div class="main-video-modal-backdrop"></div>
      <div class="main-video-modal-dialog" role="dialog" aria-modal="true" aria-label="영상 재생 모달">
        <button type="button" class="main-video-modal-close" aria-label="영상 닫기">×</button>
        <div class="main-video-modal-media"></div>
      </div>
    `;

    const backdrop = modal.querySelector('.main-video-modal-backdrop');
    const closeBtn = modal.querySelector('.main-video-modal-close');
    const dialog = modal.querySelector('.main-video-modal-dialog');

    backdrop.addEventListener('click', closeVideoModal);
    closeBtn.addEventListener('click', closeVideoModal);

    modal.addEventListener('click', (e) => {
      if (!dialog.contains(e.target)) {
        closeVideoModal();
      }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closeVideoModal();
      }
    });

    document.body.appendChild(modal);
    return modal;
  }

  function openVideoModal(movie) {
    const rawVideoUrl = getMovieVideoUrl(movie);

    if (!rawVideoUrl) {
      console.error('[movie detail video] movie data:', movie);
      alert('재생주소가 없습니다.');
      return;
    }

    const videoInfo = getVideoRenderInfo(rawVideoUrl);
    if (!videoInfo) {
      console.error('[movie detail video] unsupported url:', rawVideoUrl);
      alert('재생 가능한 영상 주소 형식이 아닙니다.');
      return;
    }

    const modal = createVideoModal();
    const mediaNode = modal.querySelector('.main-video-modal-media');
    if (!mediaNode) return;

    mediaNode.innerHTML = '';

    if (videoInfo.type === 'youtube') {
      const iframe = document.createElement('iframe');
      iframe.src = videoInfo.src;
      iframe.title = `${movie.title || '영화'} 영상 재생`;
      iframe.allow = 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share';
      iframe.allowFullscreen = true;
      iframe.referrerPolicy = 'strict-origin-when-cross-origin';
      mediaNode.appendChild(iframe);
    } else {
      const video = document.createElement('video');
      video.src = videoInfo.src;
      video.controls = true;
      video.autoplay = true;
      video.playsInline = true;
      mediaNode.appendChild(video);
    }

    modal.classList.add('open');
    document.body.classList.add('main-video-modal-open');
  }

  function renderMovieDetailView(mount, payload) {
    const movie = payload && payload.movie ? payload.movie : {};
    const posterUrl = resolvePosterUrl(movie);

    const topSynopsis = movie.synopsis || movie.synopsis_line || '줄거리 정보가 없습니다.';
    const detailSynopsis = movie.synopsis_line || movie.synopsis || '줄거리 정보가 없습니다.';
    const releaseDateDisplay = getReleaseDateDisplay(movie);

    mount.innerHTML = `
      <div class="movie-detail-page">
        <div class="movie-detail-top-row">
          <button type="button" class="movie-detail-pill-button" id="movie-detail-back-button">← 뒤로가기</button>
        </div>

        <section class="movie-detail-hero">
          <div class="movie-detail-poster-box">
            <img
              class="movie-detail-poster"
              src="${escapeHtml(posterUrl)}"
              alt="${escapeHtml(movie.title || '영화 포스터')}"
              loading="eager"
            >
          </div>

          <div class="movie-detail-summary">
            <h1 class="movie-detail-title">${escapeHtml(movie.title || '제목 없음')}</h1>

            <div class="movie-detail-meta">
              <span class="movie-detail-meta-text">${escapeHtml(releaseDateDisplay)} 개봉</span>
              <span class="movie-detail-meta-divider">|</span>
              <span class="movie-detail-meta-text">🕒 ${escapeHtml(formatRuntime(movie.runtime_minutes))}</span>
              <span class="movie-detail-meta-divider">|</span>
              <span class="movie-detail-meta-text">${escapeHtml(formatAudienceInWan(movie.audience_count))}</span>
            </div>

            <div class="movie-detail-action-row">
              <button
                type="button"
                id="movie-detail-trailer-button"
                class="movie-detail-pill-button"
              >
                ▶ 예고편 재생
              </button>

              <button type="button" class="movie-detail-icon-button" aria-label="좋아요">♡</button>
              <button type="button" class="movie-detail-icon-button" aria-label="공유">↗</button>
            </div>

            <p class="movie-detail-synopsis">${escapeHtml(topSynopsis)}</p>

            <button type="button" class="movie-detail-booking-button">예매하기</button>
          </div>
        </section>

        <nav class="movie-detail-tab-bar">
          <a href="#movie-detail-info" class="movie-detail-tab active">상세정보</a>
        </nav>

        <section id="movie-detail-info" class="movie-detail-section">
          <h2 class="movie-detail-section-title">영화정보</h2>

          <dl class="movie-detail-info-list">
            ${createInfoItem('장르', movie.genre)}
            ${createInfoItem('감독', movie.director)}
            ${createInfoItem('상영시간', formatRuntime(movie.runtime_minutes))}
            ${createInfoItem('개봉일', releaseDateDisplay)}
            ${createInfoItem('누적관객', `${formatAudienceCount(movie.audience_count)}명`)}
            ${createInfoItem('상태', formatStatus(movie.status))}
          </dl>
        </section>

        <section class="movie-detail-section">
          <h2 class="movie-detail-section-title">줄거리</h2>
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

    const backButton = mount.querySelector('#movie-detail-back-button');
    if (backButton) {
      backButton.addEventListener('click', function () {
        goBackToMovieMain();
      });
    }

    const trailerButton = mount.querySelector('#movie-detail-trailer-button');
    if (trailerButton) {
      trailerButton.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        openVideoModal(movie);
      });
    }

    const bookingButton = mount.querySelector('.movie-detail-booking-button');
    if (bookingButton) {
      bookingButton.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        openBookingPage(movie.movie_id);
      });
    }
  }

  async function mountMovieDetail() {
    const route = getRoute();
    const movieId = route.movie_id;
    const mount = ensureMountPoint();

    if (!movieId) {
      renderInvalid(mount);
      return;
    }

    await ensureMovieDetailCss();
    clearPageSections();
    createVideoModal();
    renderLoading(mount);
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' });

    try {
      const payload = await loadMovieDetail(movieId);
      renderMovieDetailView(mount, payload);
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    } catch (error) {
      console.error(error);
      renderError(mount);
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    }
  }

  window.renderMovieDetail = mountMovieDetail;
})();

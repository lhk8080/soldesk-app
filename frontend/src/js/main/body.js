(function () {
  const BODY_CSS_PATH = '/css/main/body.css';
  const AUTO_SLIDE_INTERVAL = 5000;

  let sliderData = [];
  let currentIndex = 0;
  let autoSlideTimer = null;
  let isHoveringHero = false;

  function ensureBodyCss() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureStyle === 'function') {
      return window.APP_RUNTIME.ensureStyle(BODY_CSS_PATH);
    }

    const exists = document.querySelector(`link[href="${BODY_CSS_PATH}"]`);
    if (exists) return Promise.resolve(exists);

    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = BODY_CSS_PATH;
    document.head.appendChild(link);
    return Promise.resolve(link);
  }

  function ensureMountPoint() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureMainBody === 'function') {
      return window.APP_RUNTIME.ensureMainBody();
    }

    let mount = document.getElementById('main-body');
    if (mount) return mount;

    mount = document.createElement('div');
    mount.id = 'main-body';

    const siteHeader = document.getElementById('site-header');
    if (siteHeader && siteHeader.nextSibling) {
      siteHeader.parentNode.insertBefore(mount, siteHeader.nextSibling);
    } else {
      document.body.appendChild(mount);
    }

    return mount;
  }


  function getMainPosterUrl(movie) {
    const candidates = [
      movie.main_poster_url,
      movie.poster_url
    ];

    for (const candidate of candidates) {
      const url = typeof window.resolveImageUrl === 'function'
        ? window.resolveImageUrl(candidate)
        : String(candidate || '').trim();

      if (url) return url;
    }

    return '';
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

  function stopAutoSlide() {
    if (autoSlideTimer) {
      clearInterval(autoSlideTimer);
      autoSlideTimer = null;
    }
  }

  function startAutoSlide() {
    stopAutoSlide();

    if (!sliderData || sliderData.length <= 1) return;
    if (isHoveringHero) return;

    autoSlideTimer = setInterval(() => {
      currentIndex = (currentIndex + 1) % sliderData.length;
      renderCurrentSlide();
    }, AUTO_SLIDE_INTERVAL);
  }

  function resetAutoSlide() {
    stopAutoSlide();
    if (!isHoveringHero) {
      startAutoSlide();
    }
  }

  function preloadImage(url) {
    return new Promise((resolve, reject) => {
      if (!url) {
        reject(new Error('empty image url'));
        return;
      }

      const img = new Image();
      img.onload = () => resolve(url);
      img.onerror = () => reject(new Error(`image load fail: ${url}`));
      img.src = url;
    });
  }

  function renderIndicators() {
    const indicatorWrap = document.getElementById('main-hero-indicators');
    if (!indicatorWrap) return;

    indicatorWrap.innerHTML = '';

    sliderData.forEach((_, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `main-hero-indicator ${index === currentIndex ? 'active' : ''}`;
      button.setAttribute('aria-label', `${index + 1}번 슬라이드 보기`);
      button.addEventListener('click', (e) => {
        e.stopPropagation();
        currentIndex = index;
        renderCurrentSlide();
        resetAutoSlide();
      });
      indicatorWrap.appendChild(button);
    });
  }

  async function applyHeroBackground(movie) {
    const hero = document.getElementById('main-hero');
    if (!hero) return;

    const bgUrl = getMainPosterUrl(movie);

    if (!bgUrl) {
      hero.style.backgroundImage = 'none';
      hero.classList.add('no-poster');
      return;
    }

    try {
      await preloadImage(bgUrl);
      hero.style.backgroundImage = `url("${bgUrl}")`;
      hero.classList.remove('no-poster');
    } catch (e) {
      console.error('[main hero] background image load error:', e);
      hero.style.backgroundImage = 'none';
      hero.classList.add('no-poster');
    }
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
      console.error('[video] invalid youtube url:', e);
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

  function openVideoModal(movie) {
    const rawVideoUrl = getMovieVideoUrl(movie);

    if (!rawVideoUrl) {
      console.error('[video] movie data:', movie);
      alert('재생주소가 없습니다.');
      return;
    }

    const videoInfo = getVideoRenderInfo(rawVideoUrl);
    if (!videoInfo) {
      console.error('[video] unsupported url:', rawVideoUrl);
      alert('재생 가능한 영상 주소 형식이 아닙니다.');
      return;
    }

    const modal = document.getElementById('main-video-modal');
    if (!modal) return;

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

  async function renderCurrentSlide() {
    if (!sliderData.length) return;

    const movie = sliderData[currentIndex];
    const title = document.getElementById('main-hero-title');
    const rank = document.getElementById('main-hero-rank');
    const meta = document.getElementById('main-hero-meta');
    const desc = document.getElementById('main-hero-desc');
    const infoArea = document.getElementById('main-hero-info-area');

    if (!title || !rank || !meta || !desc || !infoArea) return;

    await applyHeroBackground(movie);

    rank.textContent = `TOP ${currentIndex + 1}`;
    title.textContent = movie.title || '-';
    meta.textContent = [
      movie.genre || '장르 미정',
      movie.release_date || '개봉일 미정',
      `누적관객 ${Number(movie.audience_count || 0).toLocaleString()}명`
    ].join(' | ');
    desc.textContent = movie.synopsis || '상세 줄거리가 준비 중입니다.';

    infoArea.onclick = (e) => {
      e.stopPropagation();
      openVideoModal(movie);
    };

    renderIndicators();
  }

  function moveSlide(direction) {
    if (!sliderData.length) return;

    if (direction === 'prev') {
      currentIndex = (currentIndex - 1 + sliderData.length) % sliderData.length;
    } else {
      currentIndex = (currentIndex + 1) % sliderData.length;
    }

    renderCurrentSlide();
    resetAutoSlide();
  }

  function createHeroLayout() {
    const section = document.createElement('section');
    section.className = 'main-hero-section';

    section.innerHTML = `
      <div id="main-hero" class="main-hero no-poster">
        <div class="main-hero-top-fade"></div>
        <div class="main-hero-bottom-fade"></div>
        <div class="main-hero-dark-layer"></div>

        <button type="button" class="main-hero-arrow main-hero-arrow-left" aria-label="이전 영화 보기">&lt;</button>

        <div
          id="main-hero-info-area"
          class="main-hero-info-area"
          role="button"
          tabindex="0"
          aria-label="영화 영상 재생"
        >
          <div class="main-hero-content">
            <div class="main-hero-text-wrap">
              <div id="main-hero-rank" class="main-hero-rank">TOP 1</div>
              <h2 id="main-hero-title" class="main-hero-title">영화 제목</h2>
              <div id="main-hero-meta" class="main-hero-meta"></div>
              <p id="main-hero-desc" class="main-hero-desc"></p>
            </div>

            <div class="main-hero-play-mark" aria-hidden="true">
              <span class="main-hero-play-mark-icon"></span>
            </div>
          </div>
        </div>

        <button type="button" class="main-hero-arrow main-hero-arrow-right" aria-label="다음 영화 보기">&gt;</button>
      </div>

      <div id="main-hero-indicators" class="main-hero-indicators"></div>
    `;

    const hero = section.querySelector('#main-hero');
    const leftArrow = section.querySelector('.main-hero-arrow-left');
    const rightArrow = section.querySelector('.main-hero-arrow-right');
    const infoArea = section.querySelector('#main-hero-info-area');

    leftArrow.addEventListener('click', (e) => {
      e.stopPropagation();
      moveSlide('prev');
    });

    rightArrow.addEventListener('click', (e) => {
      e.stopPropagation();
      moveSlide('next');
    });

    infoArea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        const movie = sliderData[currentIndex];
        openVideoModal(movie);
      }
    });

    hero.addEventListener('mouseenter', () => {
      isHoveringHero = true;
      resetAutoSlide();
      hero.classList.add('hovered');
    });

    hero.addEventListener('mouseleave', () => {
      isHoveringHero = false;
      hero.classList.remove('hovered');
      resetAutoSlide();
    });

    return section;
  }

  async function loadMainMovies(bustCache) {
    const fetchOpts = bustCache ? { cache: 'no-store' } : {};
    const response = await readApi('/movies', fetchOpts);
    if (!Array.isArray(response)) return [];

    return response
      .filter(movie => movie.status === 'ACTIVE')
      .sort((a, b) => Number(b.audience_count || 0) - Number(a.audience_count || 0))
      .slice(0, 4);
  }

  async function mountBody(options) {
    const bustCache = options && options.bustCache;
    await ensureBodyCss();
    createVideoModal();

    const mount = ensureMountPoint();
    mount.innerHTML = `
      <div class="main-body-wrap">
        <div class="main-body-loading">메인 영화를 불러오는 중...</div>
      </div>
    `;

    try {
      sliderData = await loadMainMovies(bustCache);
      currentIndex = 0;

      if (!sliderData.length) {
        mount.innerHTML = `
          <div class="main-body-wrap">
            <div class="main-body-empty">표시할 메인 영화가 없습니다.</div>
          </div>
        `;
        return;
      }

      mount.innerHTML = '';
      mount.appendChild(createHeroLayout());

      await renderCurrentSlide();
      startAutoSlide();
    } catch (e) {
      console.error(e);
      mount.innerHTML = `
        <div class="main-body-wrap">
          <div class="main-body-error">메인 영화 영역을 불러오지 못했습니다.</div>
        </div>
      `;
    }
  }

  window.renderMainBody = mountBody;
  window.closeMainVideoModal = closeVideoModal;
  window.openMainVideoModal = openVideoModal;

  function attachReadCacheRebuildListeners() {
    const ch = window.TICKETING_READ_CACHE_CHANNEL || 'ticketing-cache';
    const run = () => {
      const wrap = document.querySelector('#main-body .main-body-wrap');
      if (!wrap || wrap.querySelector('.main-body-loading')) return;
      if (typeof window.renderMainBody === 'function') {
        window.renderMainBody({ bustCache: true });
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
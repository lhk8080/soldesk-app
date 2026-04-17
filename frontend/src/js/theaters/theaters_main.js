(function () {
  const THEATERS_MAIN_CSS_PATH = '/css/theaters/theaters_main.css';
  const THEATERS_DETAIL_SCRIPT_PATH = '/js/theaters/theaters_detail.js';
  /** movie 와 동일한 /api/read/movies/* 프록시에 맞춘 bootstrap 별칭 (엔트리가 theaters 로만 열리면 404 나는 환경 대비) */
  const MOVIE_BOOKING_BOOTSTRAP_API_PATH = '/movies/booking-bootstrap';
  const THEATERS_BOOTSTRAP_API_PATH = '/theaters/bootstrap';
  const OPTIONAL_REMAIN_OVERRIDES_API_PATH = '/theaters/remain-overrides';
  const THEATERS_LAYOUT_PATCH_STYLE_ID = 'theaters-booking-layout-patch-style';
  const DEFAULT_SEAT_ROWS = 3;
  const DEFAULT_SEAT_COLS = 10;
  const DATE_STRIP_LENGTH = 7;

  const state = {
    movies: [],
    dataset: null,
    selectedTheaterId: null,
    selectedMovieId: null,
    /** 가로 날짜 스트립의 시작일(YYYY-MM-DD). 달력에서 선택 시 이 값부터 10일치 표시 */
    dateWindowStartKey: '',
    selectedDateKey: '',
    selectedHallId: null,
    selectedTimeBand: 'ALL',
    /** TITLE | AUDIENCE */
    movieSortOrder: 'AUDIENCE'
  };

  function ensureMainCss() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureStyle === 'function') {
      return window.APP_RUNTIME.ensureStyle(THEATERS_MAIN_CSS_PATH);
    }

    const exists = document.querySelector(`link[href="${THEATERS_MAIN_CSS_PATH}"]`);
    if (exists) return Promise.resolve(exists);

    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = THEATERS_MAIN_CSS_PATH;
    document.head.appendChild(link);
    return Promise.resolve(link);
  }

  function ensureDetailScript() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureScript === 'function') {
      return window.APP_RUNTIME.ensureScript(THEATERS_DETAIL_SCRIPT_PATH);
    }

    const exists = document.querySelector(`script[src="${THEATERS_DETAIL_SCRIPT_PATH}"]`);
    if (exists) return Promise.resolve(exists);

    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = THEATERS_DETAIL_SCRIPT_PATH;
      script.defer = true;
      script.addEventListener('load', () => resolve(script), { once: true });
      script.addEventListener('error', () => reject(new Error(`script load fail: ${THEATERS_DETAIL_SCRIPT_PATH}`)), { once: true });
      document.body.appendChild(script);
    });
  }

  function ensureMainCssLayoutPatch() {
    if (document.getElementById(THEATERS_LAYOUT_PATCH_STYLE_ID)) return;

    const style = document.createElement('style');
    style.id = THEATERS_LAYOUT_PATCH_STYLE_ID;
    style.textContent = `
      #main-body .theaters-booking-page {
        max-width: 1320px;
        margin: 0 auto;
        padding: 24px 20px 40px;
        box-sizing: border-box;
      }

      #main-body .theaters-booking-content {
        min-width: 0;
      }

      #main-body .theaters-booking-grid {
        min-width: 0;
      }

      #main-body .theaters-booking-panel,
      #main-body .theaters-booking-date-panel {
        min-width: 0;
      }

      @media (max-width: 1400px) {
        #main-body .theaters-booking-page {
          max-width: 1180px;
          padding-left: 16px;
          padding-right: 16px;
        }
      }
    `;

    document.head.appendChild(style);
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

  function clearPrimarySections() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.resetPrimarySections === 'function') {
      window.APP_RUNTIME.resetPrimarySections();
      return;
    }

    const mount = ensureMountPoint();
    mount.innerHTML = '';
    mount.style.display = '';

    const second = document.getElementById('main-body2');
    if (second) second.remove();
  }

  function cleanupDuplicateBookingPages(mount) {
    document.querySelectorAll('.theaters-booking-page').forEach((node) => {
      if (!mount.contains(node)) {
        node.remove();
      }
    });
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

  function toInt(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.trunc(parsed) : 0;
  }

  function parseDateValue(value) {
    if (!value) return null;
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;

    const normalized = String(value).trim().replace(' ', 'T');
    const date = new Date(normalized);
    if (!Number.isNaN(date.getTime())) return date;

    const fallback = new Date(String(value).trim());
    return Number.isNaN(fallback.getTime()) ? null : fallback;
  }

  function pad2(value) {
    return String(value).padStart(2, '0');
  }

  function startOfDayFromDateKey(key) {
    const parts = String(key || '').split('-');
    if (parts.length !== 3) return null;
    const y = toInt(parts[0]);
    const m = toInt(parts[1]);
    const d = toInt(parts[2]);
    if (!y || !m || !d) return null;
    const dt = new Date(y, m - 1, d, 0, 0, 0, 0);
    return Number.isNaN(dt.getTime()) ? null : dt;
  }

  function addDaysToDateKey(key, deltaDays) {
    const dt = startOfDayFromDateKey(key);
    if (!dt) return '';
    dt.setDate(dt.getDate() + deltaDays);
    return toDateKey(dt);
  }

  function clampDateWindowToToday() {
    const today = toDateKey(new Date());
    if (!state.dateWindowStartKey || state.dateWindowStartKey < today) {
      state.dateWindowStartKey = today;
    }
  }

  function getDateStripKeys() {
    const start = state.dateWindowStartKey || toDateKey(new Date());
    const keys = [];
    for (let i = 0; i < DATE_STRIP_LENGTH; i += 1) {
      const k = addDaysToDateKey(start, i);
      if (k) keys.push(k);
    }
    return keys;
  }

  function formatTheaterListLabel(theater) {
    if (!theater) return '';
    const id = toInt(theater.theater_id);
    const name = String(theater.theater_name || '').trim() || `극장 ${id}`;
    return `${name} · #${id}`;
  }

  function toDateKey(value) {
    const date = parseDateValue(value);
    if (!date) return '';
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  }

  function formatMonthLabel(value) {
    const date = parseDateValue(value);
    if (!date) return '';
    return `${date.getMonth() + 1}월`;
  }

  function formatDayLabel(value) {
    const date = parseDateValue(value);
    if (!date) return '';

    const names = ['일', '월', '화', '수', '목', '금', '토'];
    return {
      day: date.getDate(),
      week: names[date.getDay()],
      full: `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
    };
  }

  function formatTimeLabel(value) {
    const date = parseDateValue(value);
    if (!date) return '--:--';
    return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  }

  function addMinutes(dateValue, minutes) {
    const date = parseDateValue(dateValue);
    if (!date) return null;
    return new Date(date.getTime() + Number(minutes || 0) * 60 * 1000);
  }

  function formatRangeLabel(schedule, movie) {
    const start = parseDateValue(schedule.show_date);
    const runtime = toInt(movie && movie.runtime_minutes ? movie.runtime_minutes : 120);
    const end = addMinutes(start, runtime);
    return `${formatTimeLabel(start)}~${formatTimeLabel(end)}`;
  }

  function getTimeBand(dateValue) {
    const date = parseDateValue(dateValue);
    if (!date) return 'ALL';
    const hour = date.getHours();
    if (hour >= 23 || hour < 5) return 'LATE';
    if (hour >= 19) return 'AFTER_19';
    if (hour >= 13) return 'AFTER_13';
    return 'ALL';
  }

  function buildSpecialTag(hallName) {
    const value = String(hallName || '').toUpperCase();
    if (value.includes('ATMOS')) return 'ATMOS';
    if (value.includes('LASER')) return 'LASER';
    if (value.includes('IMAX')) return 'IMAX';
    return 'GENERAL';
  }

  function normalizeBootstrap(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};

    const theaters = Array.isArray(source.theaters) ? source.theaters.map((item, index) => ({
      theater_id: toInt(item.theater_id || index + 1),
      region_name: String(item.region_name || item.region || '서울').trim() || '서울',
      theater_name: String(item.theater_name || item.name || item.address || `극장 ${index + 1}`).trim() || `극장 ${index + 1}`,
      address: String(item.address || '').trim()
    })) : [];

    const halls = Array.isArray(source.halls) ? source.halls.map((item, index) => ({
      hall_id: toInt(item.hall_id || index + 1),
      theater_id: toInt(item.theater_id),
      hall_name: String(item.hall_name || item.name || 'A관').trim() || 'A관',
      total_seats: Math.max(1, toInt(item.total_seats || DEFAULT_SEAT_ROWS * DEFAULT_SEAT_COLS)),
      seat_rows: Math.max(1, toInt(item.seat_rows || DEFAULT_SEAT_ROWS)),
      seat_cols: Math.max(1, toInt(item.seat_cols || DEFAULT_SEAT_COLS)),
      special_tag: String(item.special_tag || '').trim().toUpperCase()
    })) : [];

    const movies = Array.isArray(source.movies) ? source.movies.map((item, index) => ({
      movie_id: toInt(item.movie_id || index + 1),
      title: String(item.title || `영화 ${index + 1}`).trim() || `영화 ${index + 1}`,
      runtime_minutes: Math.max(1, toInt(item.runtime_minutes || 120)),
      status: String(item.status || 'ACTIVE').toUpperCase(),
      hide: String(item.hide || 'N').toUpperCase(),
      audience_count: toInt(item.audience_count),
      stat: item.stat !== undefined && item.stat !== null ? String(item.stat).trim() : ''
    })) : [];

    const schedules = Array.isArray(source.schedules) ? source.schedules.map((item, index) => ({
      schedule_id: toInt(item.schedule_id || index + 1),
      movie_id: toInt(item.movie_id),
      hall_id: toInt(item.hall_id),
      show_date: String(item.show_date || '').trim(),
      total_count: Math.max(0, toInt(item.total_count || item.total_seats || DEFAULT_SEAT_ROWS * DEFAULT_SEAT_COLS)),
      remain_count: Math.max(0, toInt(item.remain_count || item.total_count || item.total_seats || DEFAULT_SEAT_ROWS * DEFAULT_SEAT_COLS)),
      status: String(item.status || 'OPEN').toUpperCase(),
      price: Math.max(0, toInt(item.price || 14000)),
      special_tag: String(item.special_tag || '').trim().toUpperCase()
    })) : [];

    const reservedSeats = {};
    const sourceReserved = source.reservedSeats && typeof source.reservedSeats === 'object' ? source.reservedSeats : {};

    Object.keys(sourceReserved).forEach((key) => {
      reservedSeats[String(key)] = Array.isArray(sourceReserved[key]) ? sourceReserved[key].map((value) => String(value)) : [];
    });

    return { theaters, halls, movies, schedules, reservedSeats };
  }

  /** 극장 상세 API 슬라이스를 기존 dataset 에 합쳐 잔여석·선점좌표를 서버와 맞춤 (콘서트 bootstrap 재조회와 동일 역할) */
  function mergeTheaterDetailSlice(dataset, detail) {
    if (!dataset || !detail || typeof detail !== 'object') return;
    const rows = Array.isArray(detail.schedules) ? detail.schedules : [];
    rows.forEach(function (incoming) {
      const sid = toInt(incoming.schedule_id);
      const existing = dataset.schedules.find(function (s) {
        return toInt(s.schedule_id) === sid;
      });
      if (!existing) return;
      existing.remain_count = Math.max(0, toInt(incoming.remain_count));
      existing.status = String(incoming.status || existing.status || 'OPEN').toUpperCase();
      if (incoming.total_count != null && incoming.total_count !== undefined) {
        existing.total_count = Math.max(0, toInt(incoming.total_count));
      }
    });
    const rs = detail.reservedSeats && typeof detail.reservedSeats === 'object' ? detail.reservedSeats : {};
    Object.keys(rs).forEach(function (key) {
      const arr = rs[key];
      dataset.reservedSeats[String(key)] = Array.isArray(arr) ? arr.map(String) : [];
    });
  }

  function isHttp404Error(error) {
    if (!error) return false;
    if (error.status === 404) return true;
    const msg = error.message ? String(error.message) : '';
    return /\b404\b/.test(msg);
  }

  async function loadInitialDataset(bustCache) {
    const fetchOpts = bustCache ? { cache: 'no-store' } : {};
    const paths = [MOVIE_BOOKING_BOOTSTRAP_API_PATH, THEATERS_BOOTSTRAP_API_PATH];
    let lastError = null;

    for (let i = 0; i < paths.length; i += 1) {
      const path = paths[i];
      try {
        const response = await readApi(path, fetchOpts);
        return normalizeBootstrap(response);
      } catch (error) {
        lastError = error;
        if (isHttp404Error(error) && i < paths.length - 1) {
          continue;
        }
        throw error;
      }
    }

    throw lastError || new Error('bootstrap 요청 실패');
  }

  async function loadOptionalRemainOverrides() {
    /*
      미래용 잔여좌석 전용 파이프입니다.
      새 py가 생기면 아래 endpoint에서
      { "1001": 28, "1002": 30 }
      형식으로 schedule_id = remain_count 맵을 내려주면 됩니다.

      현재는 endpoint가 없어도 메인 화면이 깨지면 안 되므로,
      호출 실패는 조용히 무시하고 bootstrap의 remain_count를 그대로 사용합니다.
    */
    try {
      const response = await readApi(OPTIONAL_REMAIN_OVERRIDES_API_PATH);
      if (!response || typeof response !== 'object' || Array.isArray(response)) {
        return {};
      }

      if (response.remain_overrides && typeof response.remain_overrides === 'object') {
        return response.remain_overrides;
      }

      return response;
    } catch (error) {
      console.info('[theaters] optional remain override skipped:', error && error.message ? error.message : error);
      return {};
    }
  }

  function applyRemainOverrides(dataset, remainOverrides) {
    if (!dataset || !Array.isArray(dataset.schedules)) return;
    if (!remainOverrides || typeof remainOverrides !== 'object') return;

    dataset.schedules.forEach((schedule) => {
      const key = String(schedule.schedule_id);
      if (!(key in remainOverrides)) return;
      schedule.remain_count = Math.max(0, toInt(remainOverrides[key]));
    });
  }

  function isMovieActiveForBooking(movie) {
    if (!movie) return false;
    const stat = movie.stat ? String(movie.stat).trim().toUpperCase() : '';
    if (stat === 'Y') return true;
    if (stat === 'N') return false;
    const status = String(movie.status || '').toUpperCase();
    const hide = String(movie.hide || 'N').toUpperCase();
    return status === 'ACTIVE' && hide === 'N';
  }

  function getAllTheatersSorted(dataset) {
    if (!dataset || !Array.isArray(dataset.theaters)) return [];
    return dataset.theaters.slice().sort((a, b) => {
      const idA = toInt(a.theater_id);
      const idB = toInt(b.theater_id);
      if (idA !== idB) return idA - idB;
      return String(a.theater_name || '').localeCompare(String(b.theater_name || ''), 'ko');
    });
  }

  function sortMoviesForDisplay(movies, order) {
    const list = movies.slice();
    if (order === 'AUDIENCE') {
      list.sort((a, b) => {
        const diff = toInt(b.audience_count) - toInt(a.audience_count);
        if (diff !== 0) return diff;
        return String(a.title || '').localeCompare(String(b.title || ''), 'ko');
      });
    } else {
      list.sort((a, b) => String(a.title || '').localeCompare(String(b.title || ''), 'ko'));
    }
    return list;
  }

  function getHallById(dataset, hallId) {
    return dataset.halls.find((item) => item.hall_id === hallId) || null;
  }

  function getTheaterById(dataset, theaterId) {
    return dataset.theaters.find((item) => item.theater_id === theaterId) || null;
  }

  function getMovieById(movieId) {
    return state.movies.find((item) => toInt(item.movie_id) === toInt(movieId)) || null;
  }

  function getTheaterSchedules(dataset, theaterId) {
    const hallIds = dataset.halls.filter((hall) => hall.theater_id === theaterId).map((hall) => hall.hall_id);
    return dataset.schedules.filter((schedule) => hallIds.includes(schedule.hall_id));
  }

  /** 선택 극장의 상영관을 이름순(A관·B관 위→아래)으로 정렬 */
  function getHallsForTheaterSorted(dataset, theaterId) {
    if (!dataset || !Array.isArray(dataset.halls)) return [];
    return dataset.halls
      .filter((hall) => toInt(hall.theater_id) === toInt(theaterId))
      .slice()
      .sort((a, b) => String(a.hall_name || '').localeCompare(String(b.hall_name || ''), 'ko'));
  }

  function getSchedulesForSelection() {
    if (!state.dataset || !state.selectedTheaterId || !state.selectedMovieId || !state.selectedDateKey) return [];

    const now = new Date();
    return getTheaterSchedules(state.dataset, state.selectedTheaterId)
      .filter((schedule) => toInt(schedule.movie_id) === toInt(state.selectedMovieId))
      .filter((schedule) => toDateKey(schedule.show_date) === state.selectedDateKey)
      .filter((schedule) => {
        const showDate = parseDateValue(schedule.show_date);
        if (!showDate) return false;
        return showDate.getTime() >= now.getTime();
      })
      .filter((schedule) => {
        if (state.selectedTimeBand === 'ALL') return true;
        return getTimeBand(schedule.show_date) === state.selectedTimeBand;
      })
      .sort((a, b) => parseDateValue(a.show_date) - parseDateValue(b.show_date));
  }

  function getAvailableMovies(dataset, theaterId) {
    const movieIds = new Set(getTheaterSchedules(dataset, theaterId).map((schedule) => toInt(schedule.movie_id)));
    return state.movies
      .filter(isMovieActiveForBooking)
      .filter((movie) => movieIds.has(toInt(movie.movie_id)));
  }

  function findFirstTheaterIdForMovie(dataset, movieId) {
    const mid = toInt(movieId);
    if (!mid || !dataset) return null;
    const theaters = getAllTheatersSorted(dataset);
    for (let i = 0; i < theaters.length; i += 1) {
      const tid = theaters[i].theater_id;
      const movies = getAvailableMovies(dataset, tid);
      if (movies.some((m) => toInt(m.movie_id) === mid)) return tid;
    }
    return null;
  }

  function findEarliestFutureScheduleDateKey(dataset, theaterId, movieId) {
    const mid = toInt(movieId);
    const tid = toInt(theaterId);
    const now = Date.now();
    let bestTime = null;
    getTheaterSchedules(dataset, tid).forEach((s) => {
      if (toInt(s.movie_id) !== mid) return;
      const d = parseDateValue(s.show_date);
      if (!d || d.getTime() < now) return;
      if (bestTime === null || d.getTime() < bestTime) bestTime = d.getTime();
    });
    if (bestTime === null) return null;
    return toDateKey(new Date(bestTime));
  }

  /**
   * 영화 상세에서 예매하기로 온 경우 (?view=booking&movie_id=…) 극장·영화·날짜를 맞춤.
   */
  function applyBookingUrlMoviePrefill() {
    let movieId = 0;
    if (typeof window.appGetRoute === 'function') {
      const r = window.appGetRoute();
      if (String(r.view || '').trim() === 'booking') {
        movieId = toInt(r.movie_id);
      }
    }
    if (!movieId) {
      const params = new URLSearchParams(window.location.search);
      if (String(params.get('view') || '').trim() === 'booking') {
        movieId = toInt(params.get('movie_id'));
      }
    }
    if (!movieId || !state.dataset) return false;

    const known = state.movies.some(
      (m) => toInt(m.movie_id) === movieId && isMovieActiveForBooking(m)
    );
    if (!known) return false;

    const theaterId = findFirstTheaterIdForMovie(state.dataset, movieId);
    if (!theaterId) return false;

    state.selectedTheaterId = theaterId;
    state.selectedMovieId = movieId;
    state.selectedHallId = null;

    const dateKey = findEarliestFutureScheduleDateKey(state.dataset, theaterId, movieId);
    if (dateKey) {
      state.dateWindowStartKey = dateKey;
      state.selectedDateKey = dateKey;
    } else {
      state.dateWindowStartKey = toDateKey(new Date());
      state.selectedDateKey = '';
    }

    return true;
  }

  function ensureStateDefaults() {
    const dataset = state.dataset;
    if (!dataset) return;

    const theaters = getAllTheatersSorted(dataset);
    if (!theaters.length) return;

    if (!state.selectedTheaterId || !theaters.some((item) => item.theater_id === state.selectedTheaterId)) {
      state.selectedTheaterId = theaters[0].theater_id;
    }

    const movies = getAvailableMovies(dataset, state.selectedTheaterId);
    if (movies.length) {
      if (!state.selectedMovieId || !movies.some((item) => toInt(item.movie_id) === toInt(state.selectedMovieId))) {
        state.selectedMovieId = toInt(movies[0].movie_id);
      }
    } else {
      state.selectedMovieId = null;
    }

    clampDateWindowToToday();
    const stripKeys = getDateStripKeys();
    if (!stripKeys.length) return;

    if (!state.selectedDateKey || !stripKeys.includes(state.selectedDateKey)) {
      state.selectedDateKey = stripKeys[0];
    }
  }

  function buildLayoutHtml() {
    const selectedTheater = state.selectedTheaterId ? getTheaterById(state.dataset, state.selectedTheaterId) : null;
    const selectedMovie = state.selectedMovieId ? getMovieById(state.selectedMovieId) : null;
    const selectedDateInfo = formatDayLabel(state.selectedDateKey);

    return `
      <div class="theaters-booking-page">
        <section class="theaters-booking-content">
          <div class="theaters-booking-topbar">
            <div class="theaters-booking-topcell">${escapeHtml(selectedTheater ? selectedTheater.theater_name : '극장')}</div>
            <div class="theaters-booking-topcell">${escapeHtml(selectedMovie ? selectedMovie.title : '영화')}</div>
            <div class="theaters-booking-topcell">${escapeHtml(selectedDateInfo ? `${selectedDateInfo.full}(${selectedDateInfo.week})` : '날짜')}</div>
          </div>

          <div class="theaters-booking-grid">
            <div class="theaters-booking-panel theaters-booking-theater-panel"></div>
            <div class="theaters-booking-panel theaters-booking-movie-panel"></div>
            <div class="theaters-booking-panel theaters-booking-date-panel"></div>
          </div>
        </section>
      </div>
    `;
  }

  function renderTheaterPanel(container) {
    const theaters = getAllTheatersSorted(state.dataset);

    container.innerHTML = `
      <div class="theaters-booking-panel-head">
        <button type="button" class="theaters-booking-tab is-active">극장</button>
      </div>
      <div class="theaters-booking-scroll theaters-booking-theater-list"></div>
    `;

    const list = container.querySelector('.theaters-booking-theater-list');

    theaters.forEach((theater) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `theaters-booking-list-button${theater.theater_id === state.selectedTheaterId ? ' is-active' : ''}`;
      button.innerHTML = `
        <span>${escapeHtml(formatTheaterListLabel(theater))}</span>
        <span class="theaters-booking-check">${theater.theater_id === state.selectedTheaterId ? '✓' : ''}</span>
      `;
      button.addEventListener('click', function () {
        state.selectedTheaterId = theater.theater_id;
        state.selectedMovieId = null;
        state.dateWindowStartKey = toDateKey(new Date());
        state.selectedDateKey = '';
        state.selectedHallId = null;
        render();
      });
      list.appendChild(button);
    });
  }

  function renderMoviePanel(container) {
    const rawMovies = state.selectedTheaterId ? getAvailableMovies(state.dataset, state.selectedTheaterId) : [];
    const movies = sortMoviesForDisplay(rawMovies, state.movieSortOrder);

    container.innerHTML = `
      <div class="theaters-booking-panel-head theaters-booking-panel-head-row">
        <select class="theaters-booking-select" id="theaters-booking-sort-select" aria-label="영화 정렬">
          <option value="TITLE"${state.movieSortOrder === 'TITLE' ? ' selected' : ''}>가나다순</option>
          <option value="AUDIENCE"${state.movieSortOrder === 'AUDIENCE' ? ' selected' : ''}>누적관객순</option>
        </select>
      </div>
      <div class="theaters-booking-scroll theaters-booking-movie-list"></div>
    `;

    const list = container.querySelector('.theaters-booking-movie-list');
    const sortSelect = container.querySelector('#theaters-booking-sort-select');
    if (sortSelect) {
      sortSelect.addEventListener('change', function () {
        state.movieSortOrder = sortSelect.value === 'AUDIENCE' ? 'AUDIENCE' : 'TITLE';
        render();
      });
    }

    if (!movies.length) {
      list.innerHTML = '<div class="theaters-booking-empty">상영 가능한 영화가 없습니다.</div>';
      return;
    }

    movies.forEach((movie) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `theaters-booking-movie-button${toInt(movie.movie_id) === toInt(state.selectedMovieId) ? ' is-active' : ''}`;
      button.innerHTML = `
        <span class="theaters-booking-movie-title">${escapeHtml(movie.title)}</span>
        <span class="theaters-booking-check">${toInt(movie.movie_id) === toInt(state.selectedMovieId) ? '✓' : ''}</span>
      `;
      button.addEventListener('click', function () {
        state.selectedMovieId = toInt(movie.movie_id);
        state.selectedDateKey = '';
        state.selectedHallId = null;
        render();
      });
      list.appendChild(button);
    });
  }

  function createDateButton(dateKey) {
    const info = formatDayLabel(dateKey);
    const todayKey = toDateKey(new Date());
    const button = document.createElement('button');
    button.type = 'button';
    const week = info ? String(info.week || '') : '';
    const weekClass = week === '토' ? ' is-sat' : week === '일' ? ' is-sun' : '';
    button.className = `theaters-booking-date-button${dateKey === state.selectedDateKey ? ' is-active' : ''}${weekClass}`;
    button.innerHTML = `
      <span class="theaters-booking-date-day">${info ? info.day : '-'}</span>
      <span class="theaters-booking-date-week">${info ? info.week : '-'}</span>
      <span class="theaters-booking-date-today">${dateKey === todayKey ? '오늘' : '&nbsp;'}</span>
    `;
    button.addEventListener('click', function () {
      state.selectedDateKey = dateKey;
      state.selectedHallId = null;
      render();
    });
    return button;
  }

  function closeCalendarModal() {
    const el = document.getElementById('theaters-booking-calendar-overlay');
    if (el) el.remove();
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.unlockBodyScroll === 'function') {
      window.APP_RUNTIME.unlockBodyScroll();
    }
  }

  function openCalendarModal() {
    const todayKey = toDateKey(new Date());
    const baseKey = state.selectedDateKey || state.dateWindowStartKey || todayKey;
    let view = startOfDayFromDateKey(baseKey);
    if (!view) view = new Date();

    closeCalendarModal();
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.lockBodyScroll === 'function') {
      window.APP_RUNTIME.lockBodyScroll();
    }

    const overlay = document.createElement('div');
    overlay.id = 'theaters-booking-calendar-overlay';
    overlay.className = 'theaters-booking-calendar-overlay';

    const dialog = document.createElement('div');
    dialog.className = 'theaters-booking-calendar-modal';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-label', '날짜 선택');

    dialog.innerHTML = `
      <div class="theaters-booking-calendar-head">
        <button type="button" class="theaters-booking-cal-nav" data-cal-prev="" aria-label="이전 달">‹</button>
        <div class="theaters-booking-cal-title" data-cal-title=""></div>
        <button type="button" class="theaters-booking-cal-nav" data-cal-next="" aria-label="다음 달">›</button>
      </div>
      <div class="theaters-booking-cal-weekday-row">
        <span>일</span><span>월</span><span>화</span><span>수</span><span>목</span><span>금</span><span>토</span>
      </div>
      <div class="theaters-booking-cal-grid" data-cal-grid=""></div>
      <button type="button" class="theaters-booking-calendar-close">닫기</button>
    `;
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    function renderCalMonth() {
      const title = dialog.querySelector('[data-cal-title]');
      title.textContent = `${view.getFullYear()}년 ${view.getMonth() + 1}월`;
      const grid = dialog.querySelector('[data-cal-grid]');
      grid.innerHTML = '';

      const first = new Date(view.getFullYear(), view.getMonth(), 1);
      const startWeekday = first.getDay();
      const daysInMonth = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();

      for (let i = 0; i < startWeekday; i += 1) {
        const pad = document.createElement('div');
        pad.className = 'theaters-booking-cal-slot is-empty';
        grid.appendChild(pad);
      }

      for (let day = 1; day <= daysInMonth; day += 1) {
        const dk = `${view.getFullYear()}-${pad2(view.getMonth() + 1)}-${pad2(day)}`;
        const cell = document.createElement('button');
        cell.type = 'button';
        cell.className = 'theaters-booking-cal-cell';
        cell.textContent = String(day);

        if (dk < todayKey) {
          cell.classList.add('is-disabled');
          cell.disabled = true;
        } else {
          cell.addEventListener('click', function () {
            state.dateWindowStartKey = dk;
            state.selectedDateKey = dk;
            closeCalendarModal();
            render();
          });
        }
        if (dk === todayKey) {
          cell.classList.add('is-today');
        }
        if (dk === state.selectedDateKey) {
          cell.classList.add('is-selected');
        }
        grid.appendChild(cell);
      }
    }

    dialog.querySelector('[data-cal-prev]').addEventListener('click', function () {
      view = new Date(view.getFullYear(), view.getMonth() - 1, 1);
      renderCalMonth();
    });
    dialog.querySelector('[data-cal-next]').addEventListener('click', function () {
      view = new Date(view.getFullYear(), view.getMonth() + 1, 1);
      renderCalMonth();
    });
    dialog.querySelector('.theaters-booking-calendar-close').addEventListener('click', closeCalendarModal);
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) closeCalendarModal();
    });

    document.addEventListener(
      'keydown',
      function escCalendar(ev) {
        if (ev.key === 'Escape') {
          document.removeEventListener('keydown', escCalendar);
          closeCalendarModal();
        }
      },
      { once: true }
    );

    renderCalMonth();
  }

  function createFilterButton(label, value, selectedValue, onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `theaters-booking-filter-button${value === selectedValue ? ' is-active' : ''}`;
    button.textContent = label;
    button.addEventListener('click', function () {
      onClick(value);
    });
    return button;
  }

  async function openScheduleDetail(schedule) {
    const hall = getHallById(state.dataset, schedule.hall_id);
    const theater = hall ? getTheaterById(state.dataset, hall.theater_id) : null;
    const movie = getMovieById(schedule.movie_id);
    const reservedSeats = state.dataset.reservedSeats[String(schedule.schedule_id)] || [];

    await ensureDetailScript();

    if (typeof window.openTheatersDetail !== 'function') {
      throw new Error('openTheatersDetail이 없습니다.');
    }

    window.openTheatersDetail({
      schedule,
      hall,
      theater,
      movie,
      reservedSeats,
      onBooked(result) {
        const selectedSeats = Array.isArray(result && result.selectedSeats) ? result.selectedSeats : [];
        const seatStore = state.dataset.reservedSeats[String(schedule.schedule_id)] || [];
        selectedSeats.forEach((seatKey) => {
          if (!seatStore.includes(seatKey)) {
            seatStore.push(seatKey);
          }
        });
        state.dataset.reservedSeats[String(schedule.schedule_id)] = seatStore;
        schedule.remain_count = Math.max(0, toInt(schedule.remain_count) - selectedSeats.length);
        render();

        const tid = toInt((theater && theater.theater_id) || (hall && hall.theater_id));
        if (tid > 0 && typeof readApi === 'function') {
          readApi('/theater/' + tid, { cache: 'no-store' })
            .then(function (detail) {
              if (!detail || !state.dataset) return;
              mergeTheaterDetailSlice(state.dataset, detail);
              render();
            })
            .catch(function () {});
        }
      }
    });
  }

  function createScheduleCard(schedule) {
    const hall = getHallById(state.dataset, schedule.hall_id);
    const movie = getMovieById(schedule.movie_id);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'theaters-booking-schedule-card';

    const rangeLabel = formatRangeLabel(schedule, movie);
    const specialTag = String(schedule.special_tag || (hall && hall.special_tag) || buildSpecialTag(hall && hall.hall_name)).toUpperCase();
    const isOpen = String(schedule.status || '').toUpperCase() === 'OPEN';
    const isSoldOut = toInt(schedule.remain_count) <= 0;
    const isDisabled = !isOpen || isSoldOut;
    if (isDisabled) {
      button.disabled = true;
      button.classList.add('is-disabled');
    }

    button.innerHTML = `
      <div class="theaters-booking-schedule-time">${escapeHtml(rangeLabel)}</div>
      <div class="theaters-booking-schedule-meta">
        <span>${escapeHtml(hall ? hall.hall_name : '상영관')}</span>
        <span>${escapeHtml(specialTag === 'GENERAL' ? '일반관' : specialTag)}</span>
      </div>
      <div class="theaters-booking-schedule-remain">잔여좌석 ${escapeHtml(String(schedule.remain_count))} / ${escapeHtml(String(schedule.total_count))}</div>
    `;

    if (!isDisabled) {
      button.addEventListener('click', function () {
        openScheduleDetail(schedule).catch((error) => {
          console.error(error);
          alert(error.message || '상세 모달을 열지 못했습니다.');
        });
      });
    }

    return button;
  }

  function renderDatePanel(container) {
    const stripKeys = getDateStripKeys();
    const schedules = getSchedulesForSelection();
    const selectedInfo = formatDayLabel(state.selectedDateKey);

    const sk0 = stripKeys[0] || '';
    const headerMonthLabel = sk0 ? `${sk0.slice(0, 4)}년 ${formatMonthLabel(sk0)}` : '';

    const hasMovies = Boolean(
      state.selectedTheaterId &&
      state.dataset &&
      getAvailableMovies(state.dataset, state.selectedTheaterId).length
    );

    container.innerHTML = `
      <div class="theaters-booking-date-header">
        <div class="theaters-booking-date-header-row">
          <div class="theaters-booking-month">${escapeHtml(headerMonthLabel)}</div>
          <button type="button" class="theaters-booking-calendar-btn" aria-label="달력에서 날짜 선택">달력</button>
        </div>
        <div class="theaters-booking-date-strip"></div>
      </div>
      <div class="theaters-booking-date-filters"></div>
      <div class="theaters-booking-date-divider"></div>
      <div class="theaters-booking-schedule-area"></div>
    `;

    const calBtn = container.querySelector('.theaters-booking-calendar-btn');
    if (calBtn) {
      calBtn.addEventListener('click', function () {
        openCalendarModal();
      });
    }

    const strip = container.querySelector('.theaters-booking-date-strip');
    stripKeys.forEach((dateKey) => {
      strip.appendChild(createDateButton(dateKey));
    });

    const filters = container.querySelector('.theaters-booking-date-filters');
    const scheduleArea = container.querySelector('.theaters-booking-schedule-area');

    const timeButtons = [
      { label: '전체', value: 'ALL' },
      { label: '13시 이후', value: 'AFTER_13' },
      { label: '19시 이후', value: 'AFTER_19' },
      { label: '심야', value: 'LATE' }
    ];

    const timeWrap = document.createElement('div');
    timeWrap.className = 'theaters-booking-filter-group theaters-booking-filter-group-time-only';
    timeButtons.forEach((item) => {
      timeWrap.appendChild(createFilterButton(item.label, item.value, state.selectedTimeBand, function (value) {
        state.selectedTimeBand = value;
        render();
      }));
    });

    filters.appendChild(timeWrap);

    if (!hasMovies) {
      scheduleArea.innerHTML = '<div class="theaters-booking-empty-large">상영 중인 영화가 없습니다.</div>';
      return;
    }

    if (!state.selectedMovieId) {
      scheduleArea.innerHTML = '<div class="theaters-booking-empty-large">영화를 선택해 주세요.</div>';
      return;
    }

    const hallsOrdered = getHallsForTheaterSorted(state.dataset, state.selectedTheaterId);
    const byHallId = new Map();
    schedules.forEach((schedule) => {
      const hid = schedule.hall_id;
      if (!byHallId.has(hid)) byHallId.set(hid, []);
      byHallId.get(hid).push(schedule);
    });

    const hasAnySchedule = schedules.length > 0;

    if (!hallsOrdered.length && !hasAnySchedule) {
      scheduleArea.innerHTML = '<div class="theaters-booking-empty-large">선택한 날짜에 상영 시간이 없습니다.</div>';
      return;
    }

    const listSource = hallsOrdered.length
      ? hallsOrdered
      : Array.from(byHallId.keys())
          .map((hid) => getHallById(state.dataset, hid))
          .filter(Boolean)
          .sort((a, b) => String(a.hall_name || '').localeCompare(String(b.hall_name || ''), 'ko'));

    listSource.forEach((hall) => {
      const items = (byHallId.get(hall.hall_id) || []).slice().sort((a, b) => parseDateValue(a.show_date) - parseDateValue(b.show_date));
      const hallTitle = String(hall.hall_name || '상영관').trim() || '상영관';

      const group = document.createElement('section');
      group.className = 'theaters-booking-schedule-group';
      group.innerHTML = `
        <div class="theaters-booking-schedule-group-title">${escapeHtml(selectedInfo ? `${selectedInfo.full}(${selectedInfo.week})` : '')} · ${escapeHtml(hallTitle)} 상영관</div>
        <div class="theaters-booking-schedule-list"></div>
      `;

      const list = group.querySelector('.theaters-booking-schedule-list');
      if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'theaters-booking-schedule-empty-hall';
        empty.textContent = '이 날짜에 상영 시간이 없습니다.';
        list.appendChild(empty);
      } else {
        items.forEach((schedule) => {
          list.appendChild(createScheduleCard(schedule));
        });
      }
      scheduleArea.appendChild(group);
    });
  }

  function render() {
    ensureStateDefaults();

    const mount = ensureMountPoint();
    mount.innerHTML = buildLayoutHtml();
    cleanupDuplicateBookingPages(mount);

    renderTheaterPanel(mount.querySelector('.theaters-booking-theater-panel'));
    renderMoviePanel(mount.querySelector('.theaters-booking-movie-panel'));
    renderDatePanel(mount.querySelector('.theaters-booking-date-panel'));
  }

  async function mountTheatersMain() {
    await ensureMainCss();
    ensureMainCssLayoutPatch();

    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.prefetchScripts === 'function') {
      window.APP_RUNTIME.prefetchScripts([THEATERS_DETAIL_SCRIPT_PATH]);
    }

    clearPrimarySections();
    const mount = ensureMountPoint();
    cleanupDuplicateBookingPages(mount);
    mount.innerHTML = '<div class="theaters-booking-page"><div class="theaters-booking-loading">예매 화면을 불러오는 중...</div></div>';

    try {
      state.movieSortOrder = 'AUDIENCE';
      state.dateWindowStartKey = toDateKey(new Date());
      // 극장 부트스트랩은 잔여석 포함 단일 Redis 키 → 브라우저 HTTP 캐시까지 막아 새로고침 시 서버가 재조회하도록
      state.dataset = await loadInitialDataset(true);
      state.movies = Array.isArray(state.dataset.movies) ? state.dataset.movies.slice() : [];

      const remainOverrides = await loadOptionalRemainOverrides();
      applyRemainOverrides(state.dataset, remainOverrides);

      applyBookingUrlMoviePrefill();
      ensureStateDefaults();

      render();
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    } catch (error) {
      console.error(error);
      const message = error && error.message ? error.message : '예매 화면을 불러오지 못했습니다.';
      mount.innerHTML = `
        <div class="theaters-booking-page">
          <div class="theaters-booking-error">${escapeHtml(message)}</div>
          <div class="theaters-booking-error" style="margin-top:8px;font-size:12px;opacity:.75;">
            시도한 API: ${escapeHtml(MOVIE_BOOKING_BOOTSTRAP_API_PATH)}, ${escapeHtml(THEATERS_BOOTSTRAP_API_PATH)}
          </div>
        </div>
      `;
    }
  }

  window.openTheatersMain = mountTheatersMain;
  // movie 쪽과 동일하게 “render*” 별칭도 제공해서 라우터/헤더 어디서 불러도 연결되도록 합니다.
  window.renderTheatersMain = mountTheatersMain;
  window.handleTheatersRoute = mountTheatersMain;

  async function refetchBookingDatasetAfterCacheRebuild() {
    const page = document.querySelector('.theaters-booking-page');
    if (!page) return;
    if (page.querySelector('.theaters-booking-loading')) return;

    try {
      state.dataset = await loadInitialDataset(true);
      state.movies = Array.isArray(state.dataset.movies) ? state.dataset.movies.slice() : [];
      const remainOverrides = await loadOptionalRemainOverrides();
      applyRemainOverrides(state.dataset, remainOverrides);
      ensureStateDefaults();
      render();
    } catch (error) {
      console.error('[theaters] Redis 재구성 후 예매 데이터 갱신 실패:', error);
    }
  }

  function attachReadCacheRebuildListeners() {
    const ch = window.TICKETING_READ_CACHE_CHANNEL || 'ticketing-cache';
    const run = () => {
      refetchBookingDatasetAfterCacheRebuild();
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
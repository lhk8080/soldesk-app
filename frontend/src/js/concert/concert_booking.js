(function () {
  const CONCERT_BOOKING_CSS_PATH = '/css/concert/concert_booking.css';
  const CONCERT_BOOKING_CSS_URL = `${CONCERT_BOOKING_CSS_PATH}?v=20260408_concert_booking`;
  const MODAL_SCRIPT = '/js/concert/concert_booking_modal.js';

  const runtime = window.APP_RUNTIME || {};

  function ensureMainCss() {
    if (runtime.ensureStyle) {
      return runtime.ensureStyle(CONCERT_BOOKING_CSS_URL);
    }
    const exists = document.querySelector(`link[href="${CONCERT_BOOKING_CSS_PATH}"], link[href="${CONCERT_BOOKING_CSS_URL}"]`);
    if (exists) return Promise.resolve(exists);
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = CONCERT_BOOKING_CSS_URL;
    document.head.appendChild(link);
    return Promise.resolve(link);
  }

  function ensureModalScript() {
    if (runtime.ensureScript) {
      return runtime.ensureScript(MODAL_SCRIPT);
    }
    const exists = document.querySelector(`script[src="${MODAL_SCRIPT}"]`);
    if (exists) return Promise.resolve(exists);
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = MODAL_SCRIPT;
      script.defer = true;
      script.addEventListener('load', () => resolve(script), { once: true });
      script.addEventListener('error', () => reject(new Error('modal script load fail')), { once: true });
      document.body.appendChild(script);
    });
  }

  function ensureMountPoint() {
    if (runtime.ensureMainBody) {
      return runtime.ensureMainBody();
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
    if (runtime.resetPrimarySections) {
      runtime.resetPrimarySections();
      return;
    }
    const mount = ensureMountPoint();
    mount.innerHTML = '';
    mount.style.display = '';
    const n = document.getElementById('main-body2');
    if (n) n.remove();
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

  function getRoute() {
    if (typeof window.appGetRoute === 'function') {
      return window.appGetRoute();
    }
    const params = new URLSearchParams(window.location.search);
    const concertId = Number(params.get('concert_id'));
    return {
      concert_id: Number.isFinite(concertId) && concertId > 0 ? concertId : null
    };
  }

  function parseDateValue(value) {
    if (!value) return null;
    const normalized = String(value).trim().replace(' ', 'T');
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatRangeLabel(show, concert) {
    const start = parseDateValue(show.show_date);
    if (!start) return String(show.show_date || '').slice(0, 16);
    const end = new Date(start.getTime() + toInt(concert.runtime_minutes || 120) * 60 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(start.getHours())}:${pad(start.getMinutes())} ~ ${pad(end.getHours())}:${pad(end.getMinutes())}`;
  }

  async function loadBootstrap(concertId) {
    const delays = [0, 280, 600];
    let lastErr = null;
    for (let i = 0; i < delays.length; i += 1) {
      if (delays[i] > 0) {
        await new Promise((r) => setTimeout(r, delays[i]));
      }
      try {
        if (runtime.ensureTicketingEndpointsLoaded) {
          await runtime.ensureTicketingEndpointsLoaded();
        }
        return await readApi(`/concert/${concertId}/booking-bootstrap`, { cache: 'no-store' });
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr || new Error('예매 정보를 불러오지 못했습니다.');
  }

  async function openShowModal(concert, show, state) {
    await ensureModalScript();
    if (typeof window.openConcertBookingModal !== 'function') {
      throw new Error('openConcertBookingModal이 없습니다.');
    }

    const confirmed = Array.isArray(show.confirmed_seats) ? show.confirmed_seats : [];
    const hold = Array.isArray(show.hold_seats) ? show.hold_seats : [];

    window.openConcertBookingModal({
      concert,
      show,
      confirmedSeats: confirmed,
      holdSeats: hold,
      onBooked(result) {
        const sid = String(result.show_id);
        const selected = Array.isArray(result.selectedSeats) ? result.selectedSeats : [];
        const store = state.reservedByShow[sid] || [];
        selected.forEach((k) => {
          if (!store.includes(k)) store.push(k);
        });
        state.reservedByShow[sid] = store;
        const target = state.shows.find((s) => String(s.show_id) === sid);
        if (target) {
          target.remain_count = Math.max(0, toInt(target.remain_count) - selected.length);
          const open = String(target.status || '').toUpperCase() === 'OPEN';
          const sold = toInt(target.remain_count) <= 0;
          if (open && sold) target.status = 'CLOSED';
        }
        renderPage(state.mount, state);

        const cid = toInt(concert.concert_id);
        const rawSid = toInt(result.show_id);
        if (cid > 0 && rawSid > 0 && typeof readApi === 'function') {
          readApi(`/concert/${cid}/booking-bootstrap?show_id=${rawSid}`)
            .then(function (data) {
              const one =
                data && Array.isArray(data.shows) && data.shows.length ? data.shows[0] : null;
              if (!one) return;
              const t2 = state.shows.find(function (s) {
                return String(s.show_id) === sid;
              });
              if (t2) {
                t2.remain_count = toInt(one.remain_count);
                t2.status = one.status;
                t2.reserved_seats = (one.reserved_seats || []).slice();
                state.reservedByShow[sid] = t2.reserved_seats.slice();
              }
              renderPage(state.mount, state);
            })
            .catch(function () {});
        }
      }
    });
  }

  function createScheduleButton(concert, show, state) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'concert-booking-card';

    // 마감/비활성 판단은 remain_count만 사용한다.
    // 과거에 show.status가 CLOSED로 "박히고" remain은 양수인 꼬임이 발생할 수 있어,
    // UI 단계에서는 status를 신뢰하지 않는다(요구사항: 잔여 기반으로만 마감 판단).
    const isOpen = true;
    const isSoldOut = toInt(show.remain_count) <= 0;
    const disabled = !isOpen || isSoldOut;
    if (disabled) {
      btn.disabled = true;
      btn.classList.add('is-disabled');
    }

    const remainText =
      isSoldOut ? '매진' : `잔여좌석 ${escapeHtml(String(show.remain_count))} / ${escapeHtml(String(show.total_count))}`;

    btn.innerHTML = `
      <div class="concert-booking-time">${escapeHtml(formatRangeLabel(show, concert))}</div>
      <div class="concert-booking-meta">
        <span>${escapeHtml(show.hall_name || '홀')}</span>
        <span>${escapeHtml(show.venue_name || '')}</span>
      </div>
      <div class="concert-booking-remain">${remainText}</div>
    `;

    if (!disabled) {
      btn.addEventListener('click', function () {
        openShowModal(concert, show, state).catch((err) => {
          console.error(err);
          alert(err.message || '예매 창을 열 수 없습니다.');
        });
      });
    }

    return btn;
  }

  function renderPage(mount, state) {
    const { concert, shows } = state;
    const title = escapeHtml(concert.title || '공연');
    const anyClosed = false;

    mount.innerHTML = `
      <div class="concert-booking-page">
        <div class="concert-booking-shell">
          <div class="concert-booking-titlebar">${title}</div>
          <div style="margin-top:14px;">
            <button type="button" class="concert-booking-back" id="concert-booking-back">← 공연 상세</button>
            <p class="concert-booking-subhead">회차를 선택한 뒤 좌석을 고르세요.</p>
            ${anyClosed ? '<div class="concert-booking-empty" style="margin-top:10px;">모든 투표가 마감되었습니다.</div>' : ''}
            <div class="concert-booking-card-list" id="concert-booking-show-list"></div>
          </div>
        </div>
      </div>
    `;

    const back = mount.querySelector('#concert-booking-back');
    if (back) {
      back.addEventListener('click', function () {
        if (typeof window.appNavigate === 'function') {
          window.appNavigate({ concert_id: concert.concert_id, c_page: 1 });
          return;
        }
        window.location.href = `/?concert_id=${concert.concert_id}`;
      });
    }

    const list = mount.querySelector('#concert-booking-show-list');
    if (!list) return;

    if (!shows.length) {
      list.innerHTML = '<div class="concert-booking-empty">예매 가능한 회차가 없습니다.</div>';
      return;
    }

    shows.forEach((show) => {
      const copy = { ...show };
      copy.reserved_seats = state.reservedByShow[String(show.show_id)] || show.reserved_seats || [];
      list.appendChild(createScheduleButton(concert, copy, state));
    });
  }

  async function mountConcertBookingPage(route) {
    await ensureMainCss();
    if (runtime.ensureTicketingEndpointsLoaded) {
      await runtime.ensureTicketingEndpointsLoaded();
    }
    if (runtime.prefetchScripts) {
      runtime.prefetchScripts([MODAL_SCRIPT]);
    }

    clearPrimarySections();
    const mount = ensureMountPoint();
    const concertId = (route && route.concert_id) || getRoute().concert_id;

    if (!concertId) {
      mount.innerHTML =
        '<div class="theaters-booking-page"><div class="theaters-booking-error">공연을 선택해주세요.</div></div>';
      return;
    }

    mount.innerHTML =
      '<div class="theaters-booking-page"><div class="theaters-booking-loading">예매 정보를 불러오는 중...</div></div>';

    try {
      const data = await loadBootstrap(concertId);
      const concert = data.concert || {};
      const shows = Array.isArray(data.shows) ? data.shows : [];

      const state = {
        mount,
        concert,
        shows,
        reservedByShow: {}
      };

      shows.forEach((s) => {
        state.reservedByShow[String(s.show_id)] = (s.reserved_seats || []).slice();
      });

      renderPage(mount, state);
      window.scrollTo({ top: 0, behavior: 'auto' });
    } catch (e) {
      console.error(e);
      mount.innerHTML = `
        <div class="theaters-booking-page">
          <div class="theaters-booking-error">예매 정보를 불러오지 못했습니다.</div>
        </div>
      `;
    }
  }

  window.openConcertBookingPage = mountConcertBookingPage;

  window.openConcertBookingFromRouter = function (args) {
    const route = (args && args.route) || getRoute();
    return mountConcertBookingPage(route);
  };
})();

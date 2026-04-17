(function () {
  const THEATERS_DETAIL_CSS_PATH = '/css/theaters/theaters_detail.css';
  const THEATERS_DETAIL_CSS_URL = `${THEATERS_DETAIL_CSS_PATH}?v=20260408_concert_base`;

  const CONCERT_MODAL_CSS_PATH = '/css/concert/concert_booking_modal.css';
  const CONCERT_MODAL_CSS_URL = `${CONCERT_MODAL_CSS_PATH}?v=20260408_concert_modal`;
  const OVERLAY_ID = 'concert-booking-detail-overlay';
  const BODY_ACTIVE_CLASS = 'theaters-booking-modal-open';

  let holdRevPollTimer = null;
  let lastAppliedHoldRev = -1;
  let holdPollSeq = 0;
  let holdPollInFlight = false;

  function stopHoldRevPoll() {
    if (holdRevPollTimer) {
      clearInterval(holdRevPollTimer);
      holdRevPollTimer = null;
    }
  }

  function ensureDetailCss() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureStyle === 'function') {
      return Promise.all([
        window.APP_RUNTIME.ensureStyle(THEATERS_DETAIL_CSS_URL),
        window.APP_RUNTIME.ensureStyle(CONCERT_MODAL_CSS_URL),
      ]);
    }
    const exists = document.querySelector(
      `link[href="${THEATERS_DETAIL_CSS_PATH}"], link[href="${THEATERS_DETAIL_CSS_URL}"]`
    );
    const existsConcert = document.querySelector(
      `link[href="${CONCERT_MODAL_CSS_PATH}"], link[href="${CONCERT_MODAL_CSS_URL}"]`
    );

    if (exists && existsConcert) return Promise.resolve([exists, existsConcert]);

    const links = [];

    if (!exists) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = THEATERS_DETAIL_CSS_URL;
      document.head.appendChild(link);
      links.push(link);
    } else {
      links.push(exists);
    }

    if (!existsConcert) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = CONCERT_MODAL_CSS_URL;
      document.head.appendChild(link);
      links.push(link);
    } else {
      links.push(existsConcert);
    }

    return Promise.resolve(links);
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

  function pad2(value) {
    return String(value).padStart(2, '0');
  }

  function parseDateValue(value) {
    if (!value) return null;
    const normalized = String(value).trim().replace(' ', 'T');
    const date = new Date(normalized);
    if (!Number.isNaN(date.getTime())) return date;
    const fallback = new Date(String(value).trim());
    return Number.isNaN(fallback.getTime()) ? null : fallback;
  }

  function addMinutes(value, minutes) {
    const date = parseDateValue(value);
    if (!date) return null;
    return new Date(date.getTime() + Number(minutes || 0) * 60 * 1000);
  }

  function formatTime(value) {
    const date = parseDateValue(value);
    if (!date) return '--:--';
    return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  }

  function lockBodyScroll() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.lockBodyScroll === 'function') {
      window.APP_RUNTIME.lockBodyScroll();
      return;
    }
    document.body.classList.add(BODY_ACTIVE_CLASS);
    document.body.style.overflow = 'hidden';
  }

  function unlockBodyScroll() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.unlockBodyScroll === 'function') {
      window.APP_RUNTIME.unlockBodyScroll();
      return;
    }
    document.body.classList.remove(BODY_ACTIVE_CLASS);
    document.body.style.overflow = '';
  }

  function closeModal() {
    stopHoldRevPoll();
    const overlay = document.getElementById(OVERLAY_ID);
    if (overlay) overlay.remove();
    unlockBodyScroll();
  }

  function closeModalFromUser(bookingSucceededFlag) {
    closeModal();
    if (bookingSucceededFlag === true) {
      window.location.reload();
    }
  }

  function getStoredUserId() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.getStoredUserId === 'function') {
      return String(window.APP_RUNTIME.getStoredUserId() || '').trim();
    }
    return String(localStorage.getItem('user_id') || sessionStorage.getItem('user_id') || '').trim();
  }

  function createSeatKey(row, col) {
    return `${row}-${col}`;
  }

  function createSeatLabel(row, col) {
    const rowNo = toInt(row);
    return `${rowNo}열 ${col}번`;
  }

  async function requestConcertBooking(payload) {
    if (typeof writeApi !== 'function') {
      throw new Error('writeApi가 없습니다.');
    }
    return await writeApi('/concerts/booking/commit', 'POST', payload);
  }

  async function enterWaitingRoom(showId, userId) {
    return await writeApi(`/concerts/${encodeURIComponent(String(showId))}/waiting-room/enter`, 'POST', {
      user_id: userId
    });
  }

  async function waitingRoomStatus(queueRef) {
    // 대기열 status는 인증/쿠키가 필요 없으므로 simple request로 보내
    // 배포/ALB 이슈로 OPTIONS(preflight)가 깨질 때도 폴링이 멈추지 않게 한다.
    return await writeApi(
      `/concerts/waiting-room/status/${encodeURIComponent(String(queueRef))}`,
      'GET',
      null,
      { credentials: 'omit', noAuth: true, cache: 'no-store' }
    );
  }

  async function waitingRoomMetrics(showId) {
    return await writeApi(
      `/concerts/${encodeURIComponent(String(showId))}/waiting-room/metrics`,
      'GET',
      null,
      { credentials: 'omit', noAuth: true, cache: 'no-store' }
    );
  }

  async function openConcertBookingModal(options) {
    await ensureDetailCss();
    closeModal();
    lastAppliedHoldRev = -1;
    lockBodyScroll();

    const data = options && typeof options === 'object' ? options : {};
    const show = data.show || {};
    const concert = data.concert || {};
    const onBooked = typeof data.onBooked === 'function' ? data.onBooked : function () {};
    const confirmedSeats = new Set(
      Array.isArray(data.confirmedSeats) ? data.confirmedSeats.map((value) => String(value)) : []
    );
    const holdSeats = new Set(
      Array.isArray(data.holdSeats) ? data.holdSeats.map((value) => String(value)) : []
    );

    const runtimeMinutes = toInt(concert.runtime_minutes || 120);
    const start = parseDateValue(show.show_date);
    const end = addMinutes(show.show_date, runtimeMinutes);

    const seatCols = Math.max(1, toInt(show.seat_cols) || 10);
    const seatRows = Math.max(1, toInt(show.seat_rows) || 5);
    let remainCount = Math.max(0, toInt(show.remain_count || 0));
    let totalCount = Math.max(0, toInt(show.total_count || 0)) || seatRows * seatCols;
    // UI 표시용 잔여/전체 좌석 수는 "모달 진입 시점"을 기준으로 고정한다.
    // 실시간 폴링은 좌석(hold/confirmed) 상태를 보여주기 위한 것이고,
    // 잔여 숫자 자체는 출렁이는 UX를 만들기 쉬워 표시를 분리한다.
    let displayRemainBase = remainCount;
    let displayTotalBase = totalCount;
    const price = Math.max(0, toInt(show.price || 0));
    const showId = toInt(show.show_id);

    const venueLine = [show.venue_name || '', show.hall_name || '홀'].filter(Boolean).join(' · ');

    const selectedSeats = new Set();
    let currentStep = 1;
    let lastResult = null;
    let bookingSucceeded = false;
    let permitToken = '';
    let waitingRoomReady = false;
    let waitingRoomRef = '';
    let waitingRoomSeq = 0;

    const overlay = document.createElement('div');
    overlay.id = OVERLAY_ID;
    overlay.className = 'theaters-detail-overlay';
    overlay.innerHTML = `
      <div class="theaters-detail-shell" role="dialog" aria-modal="true" aria-label="콘서트 예매">
        <nav class="theaters-detail-steps" aria-label="예매 단계">
          <button type="button" class="theaters-detail-step is-active" data-step="1" disabled tabindex="-1" aria-disabled="true">
            <span class="theaters-detail-step-no">01</span>
            <span class="theaters-detail-step-label">좌석선택</span>
          </button>
          <button type="button" class="theaters-detail-step" data-step="2" disabled tabindex="-1" aria-disabled="true">
            <span class="theaters-detail-step-no">02</span>
            <span class="theaters-detail-step-label">결제</span>
          </button>
          <button type="button" class="theaters-detail-step" data-step="3" disabled tabindex="-1" aria-disabled="true">
            <span class="theaters-detail-step-no">03</span>
            <span class="theaters-detail-step-label">결제완료</span>
          </button>
        </nav>

        <div class="theaters-detail-modal">
          <div class="theaters-detail-head">
            <div class="theaters-detail-title">${escapeHtml(formatTime(start))}~${escapeHtml(formatTime(end))} (${escapeHtml(show.hall_name || '회차')})</div>
            <button type="button" class="theaters-detail-close" aria-label="닫기">×</button>
          </div>

          <div class="theaters-detail-body">
            <div class="theaters-detail-summary">
              <div class="theaters-detail-summary-main">
                <div class="theaters-detail-movie">${escapeHtml(concert.title || '공연')}</div>
                <div class="theaters-detail-place">${escapeHtml(venueLine || '공연장')}</div>
                <div class="theaters-detail-date">${escapeHtml(String(show.show_date || '').slice(0, 16))}</div>
              </div>
              <div class="theaters-detail-summary-side">
                <div class="theaters-detail-queue" hidden aria-live="polite"></div>
              </div>
            </div>

            <section class="theaters-detail-panel theaters-detail-panel-seat" data-panel="1">
              <div class="theaters-detail-seat-count">
                잔여좌석 <strong class="theaters-detail-remain">${escapeHtml(String(remainCount))}</strong><span>/${escapeHtml(String(totalCount))}</span>
              </div>
              <div class="theaters-detail-screen-label">STAGE</div>
              <div class="theaters-detail-screen-bar"></div>
              <div class="theaters-detail-seat-grid"></div>

              <div class="theaters-detail-selected-wrap">
                <div class="theaters-detail-selected-label">선택 좌석</div>
                <div class="theaters-detail-selected-value">없음</div>
              </div>

              <div class="theaters-detail-price-wrap">
                <div>선택 인원 <strong class="theaters-detail-count">0명</strong></div>
                <div>예상 금액 <strong class="theaters-detail-price">0원</strong></div>
              </div>
            </section>

            <section class="theaters-detail-panel theaters-detail-panel-confirm" data-panel="2" hidden>
              <div class="theaters-detail-confirm-title">선택 정보 확인</div>
              <div class="theaters-detail-confirm-list">
                <div class="theaters-detail-confirm-row"><span>공연</span><strong class="theaters-detail-confirm-movie"></strong></div>
                <div class="theaters-detail-confirm-row"><span>장소</span><strong class="theaters-detail-confirm-theater"></strong></div>
                <div class="theaters-detail-confirm-row"><span>홀</span><strong class="theaters-detail-confirm-hall"></strong></div>
                <div class="theaters-detail-confirm-row"><span>일시</span><strong class="theaters-detail-confirm-date"></strong></div>
                <div class="theaters-detail-confirm-row"><span>좌석</span><strong class="theaters-detail-confirm-seats"></strong></div>
                <div class="theaters-detail-confirm-row"><span>인원</span><strong class="theaters-detail-confirm-count"></strong></div>
                <div class="theaters-detail-confirm-row"><span>결제금액</span><strong class="theaters-detail-confirm-price"></strong></div>
              </div>
              <div class="theaters-detail-confirm-ask-row">
                <div class="theaters-detail-confirm-ask">결제를 진행하시겠습니까?</div>
                <button type="button" class="theaters-detail-reselect">좌석 재선택</button>
              </div>
            </section>

            <section class="theaters-detail-panel theaters-detail-panel-result" data-panel="3" hidden>
              <div class="theaters-detail-result-title">결제 결과</div>
              <div class="theaters-detail-result-message"></div>
            </section>
          </div>

          <div class="theaters-detail-actions">
            <button type="button" class="theaters-detail-cancel">취소</button>
            <button type="button" class="theaters-detail-submit">결제 진행</button>
          </div>
        </div>
      </div>
    `;

    document.body.appendChild(overlay);

    const closeButton = overlay.querySelector('.theaters-detail-close');
    const cancelButton = overlay.querySelector('.theaters-detail-cancel');
    const submitButton = overlay.querySelector('.theaters-detail-submit');
    const seatGrid = overlay.querySelector('.theaters-detail-seat-grid');
    const selectedValue = overlay.querySelector('.theaters-detail-selected-value');
    const selectedCount = overlay.querySelector('.theaters-detail-count');
    const selectedPrice = overlay.querySelector('.theaters-detail-price');
    const remainNode = overlay.querySelector('.theaters-detail-remain');
    const stepButtons = overlay.querySelectorAll('.theaters-detail-step');
    const seatPanel = overlay.querySelector('.theaters-detail-panel-seat');
    const confirmPanel = overlay.querySelector('.theaters-detail-panel-confirm');
    const resultPanel = overlay.querySelector('.theaters-detail-panel-result');
    const confirmMovie = overlay.querySelector('.theaters-detail-confirm-movie');
    const confirmTheater = overlay.querySelector('.theaters-detail-confirm-theater');
    const confirmHall = overlay.querySelector('.theaters-detail-confirm-hall');
    const confirmDate = overlay.querySelector('.theaters-detail-confirm-date');
    const confirmSeats = overlay.querySelector('.theaters-detail-confirm-seats');
    const confirmCount = overlay.querySelector('.theaters-detail-confirm-count');
    const confirmPrice = overlay.querySelector('.theaters-detail-confirm-price');
    const resultMessage = overlay.querySelector('.theaters-detail-result-message');
    const reselectButton = overlay.querySelector('.theaters-detail-reselect');
    const queueNode = overlay.querySelector('.theaters-detail-queue');

    function createWaitingRoomOverlay() {
      const wrap = document.createElement('div');
      wrap.className = 'wr-overlay';
      wrap.innerHTML = `
        <div class="wr-card" role="dialog" aria-label="대기열">
          <button type="button" class="wr-close" aria-label="닫기">×</button>
          <div class="wr-title">접속 인원이 많아 대기 중입니다.</div>
          <div class="wr-sub">조금만 기다려주세요.</div>
          <div class="wr-metrics">
            <div class="wr-metrics-label">나의 대기순서</div>
            <div class="wr-position">-</div>
            <div class="wr-meta">
              <div>예상 대기시간 <strong class="wr-eta">-</strong></div>
              <div>현재 대기인원 <strong class="wr-backlog">-</strong>명</div>
              <div class="wr-msg"></div>
            </div>
          </div>
        </div>
      `;
      const closeBtn = wrap.querySelector('.wr-close');
      if (closeBtn) closeBtn.addEventListener('click', () => closeModalFromUser(false));
      return {
        wrap,
        pos: wrap.querySelector('.wr-position'),
        eta: wrap.querySelector('.wr-eta'),
        backlog: wrap.querySelector('.wr-backlog'),
        msg: wrap.querySelector('.wr-msg')
      };
    }

    const wrUi = createWaitingRoomOverlay();
    let wrVisible = false;

    function showWaitingRoomOverlay() {
      if (wrVisible) return;
      if (wrUi && wrUi.wrap && overlay) {
        overlay.appendChild(wrUi.wrap);
        wrVisible = true;
      }
    }

    function hideWaitingRoomOverlay() {
      if (!wrVisible) return;
      if (wrUi && wrUi.wrap) wrUi.wrap.remove();
      wrVisible = false;
    }

    function updateRemainTotalDisplay() {
      if (remainNode) {
        remainNode.textContent = String(Math.max(0, displayRemainBase - selectedSeats.size));
      }
      const totalSpan = remainNode && remainNode.nextElementSibling;
      if (totalSpan && totalSpan.tagName === 'SPAN') {
        totalSpan.textContent = `/${String(displayTotalBase)}`;
      }
    }

    function updateSummary() {
      const seatLabels = Array.from(selectedSeats).map((seatKey) => {
        const parts = seatKey.split('-');
        return createSeatLabel(parts[0], parts[1]);
      });

      selectedValue.textContent = seatLabels.length ? seatLabels.join(', ') : '없음';
      selectedCount.textContent = `${selectedSeats.size}명`;
      selectedPrice.textContent = `${(selectedSeats.size * price).toLocaleString('ko-KR')}원`;
      updateRemainTotalDisplay();
      if (currentStep === 1) {
        submitButton.disabled = selectedSeats.size === 0;
      }
    }

    /**
     * 대기열 통과 직후·SQS 처리 중 등으로 확정/홀드 좌석이 바뀐 뒤 서버 스냅샷과 UI를 맞춘다.
     */
    function applySeatAvailabilityFromSets() {
      if (!seatGrid) return;
      seatGrid.querySelectorAll('.theaters-detail-seat').forEach((btn) => {
        const key = btn.dataset && btn.dataset.seatKey ? String(btn.dataset.seatKey) : '';
        if (!key) return;
        btn.classList.remove('is-disabled', 'is-hold');
        btn.disabled = false;
        if (confirmedSeats.has(key)) {
          btn.classList.add('is-disabled');
          btn.disabled = true;
          if (selectedSeats.has(key)) {
            selectedSeats.delete(key);
            btn.classList.remove('is-selected');
          }
        } else if (holdSeats.has(key)) {
          btn.classList.add('is-hold');
          btn.disabled = true;
          if (selectedSeats.has(key)) {
            selectedSeats.delete(key);
            btn.classList.remove('is-selected');
          }
        }
      });
      updateSummary();
    }

    function applyBookingHoldsPayload(light, options) {
      if (!light || !light.ok) return false;
      const opt = options && typeof options === 'object' ? options : {};
      const updateDisplayCounts = opt.updateDisplayCounts === true;
      // remain은 단일 카운터(서버)만 신뢰한다.
      if (Number.isFinite(Number(light.remain_count))) {
        remainCount = Math.max(0, toInt(light.remain_count));
      }
      if (Number.isFinite(Number(light.snapshot_total_count)) && toInt(light.snapshot_total_count) > 0) {
        totalCount = toInt(light.snapshot_total_count);
      }
      if (updateDisplayCounts) {
        displayRemainBase = remainCount;
        displayTotalBase = totalCount;
      }
      if (Array.isArray(light.confirmed_seats)) {
        // booking-holds의 confirmed_seats가 일시적으로 빈 배열로 내려오면(조회 오류/지연 등)
        // 이미 회색으로 잠긴 좌석이 흰색으로 "풀려 보이는" UX가 생길 수 있다.
        // 기존 confirmedSeats가 있고, 새 payload가 빈 배열이면 기존 값을 유지한다.
        const next = light.confirmed_seats.map((k) => String(k));
        if (next.length > 0 || confirmedSeats.size === 0) {
          confirmedSeats.clear();
          next.forEach((k) => confirmedSeats.add(k));
        }
      }
      // cache/holds 기능이 꺼진 환경에서는 booking-holds가 hold_seats/confirmed_seats를 내려주지 않을 수 있다.
      // 그 경우 기존(bootstrap 기반) 좌석 상태를 유지해, 빈 배열로 덮어쓰며 좌석이 "풀려 보이는" 문제를 막는다.
      if (Array.isArray(light.hold_seats)) {
        holdSeats.clear();
        light.hold_seats.forEach((k) => {
          holdSeats.add(String(k));
        });
      }
      applySeatAvailabilityFromSets();
      return true;
    }

    async function pollHoldRevIfChanged() {
      // 처리중(홀드) 좌석(주황)은 "입장 대기열 통과"와 무관하게 실시간으로 보여야 한다.
      // waitingRoomReady 전이라도 hold_rev 기반으로 갱신해 UI에서 백그라운드 처리 상태를 관측할 수 있게 한다.
      if (currentStep !== 1) return;
      const cid = toInt(concert.concert_id);
      if (!cid || !showId || typeof readApi !== 'function') return;
      // 폴링 중복 호출을 막아 out-of-order/덮어쓰기 자체를 차단한다.
      if (holdPollInFlight) return;
      holdPollInFlight = true;
      const mySeq = (holdPollSeq += 1);
      try {
        const light = await readApi(`/concert/${cid}/booking-holds`, {
          cache: 'no-store',
          query: { show_id: showId }
        });
        // 폴링 요청이 겹치면 응답 순서가 뒤집힐 수 있다.
        // 가장 마지막 요청만 반영해 오래된 응답이 최신 UI를 덮어쓰지 않게 한다.
        if (mySeq !== holdPollSeq) return;
        const rev = light && Number.isFinite(Number(light.hold_rev)) ? Number(light.hold_rev) : 0;
        if (rev === lastAppliedHoldRev) return;
        applyBookingHoldsPayload(light, { updateDisplayCounts: false });
        lastAppliedHoldRev = rev;
        Object.assign(show, { remain_count: remainCount, total_count: totalCount });
      } catch (e) {
        /* ignore */
      } finally {
        holdPollInFlight = false;
      }
    }

    function startHoldRevPoll() {
      stopHoldRevPoll();
      holdRevPollTimer = setInterval(() => {
        pollHoldRevIfChanged();
      }, 750);
    }

    async function refreshSeatSnapshotAfterWaitingRoom() {
      const cid = toInt(concert.concert_id);
      if (!cid || !showId || typeof readApi !== 'function') return;
      try {
        const light = await readApi(`/concert/${cid}/booking-holds`, {
          cache: 'no-store',
          query: { show_id: showId }
        });
        if (applyBookingHoldsPayload(light, { updateDisplayCounts: true })) {
          if (Number.isFinite(Number(light.hold_rev))) {
            lastAppliedHoldRev = Number(light.hold_rev);
          }
          Object.assign(show, { remain_count: remainCount, total_count: totalCount });
          return;
        }
      } catch (e) {
        // 단일 기준: 좌석/잔여는 booking-holds만 사용한다.
        // bootstrap 폴백은 스냅샷/메타가 섞이면서 출렁임(덮어쓰기)을 만들 수 있어 비활성화한다.
        console.warn('[concert booking] booking-holds 실패(좌석 상태 갱신 생략)', e);
      }
    }

    function setSeatUiEnabled(enabled) {
      if (!seatGrid) return;
      seatGrid.style.pointerEvents = enabled ? '' : 'none';
      seatGrid.style.opacity = enabled ? '' : '0.55';
      if (submitButton && currentStep === 1) {
        // 대기열 통과 전에는 "결제 진행" 버튼을 막아서 강하게 연출
        submitButton.disabled = !enabled || selectedSeats.size === 0;
      }
    }

    function updateConfirmPanel() {
      const seatLabels = Array.from(selectedSeats).map((seatKey) => {
        const parts = seatKey.split('-');
        return createSeatLabel(parts[0], parts[1]);
      });

      if (confirmMovie) confirmMovie.textContent = String(concert.title || '');
      if (confirmTheater) {
        confirmTheater.textContent = String(show.venue_address || show.venue_name || '');
      }
      if (confirmHall) confirmHall.textContent = String(show.hall_name || '');
      if (confirmDate) confirmDate.textContent = String(show.show_date || '').slice(0, 16);
      if (confirmSeats) confirmSeats.textContent = seatLabels.length ? seatLabels.join(', ') : '없음';
      if (confirmCount) confirmCount.textContent = `${selectedSeats.size}명`;
      if (confirmPrice) confirmPrice.textContent = `${(selectedSeats.size * price).toLocaleString('ko-KR')}원`;
    }

    function setStep(step) {
      if (step !== 1) {
        stopHoldRevPoll();
      }
      currentStep = step;

      stepButtons.forEach((btn) => {
        const isActive = String(btn.getAttribute('data-step')) === String(step);
        btn.classList.toggle('is-active', isActive);
      });

      if (seatPanel) seatPanel.hidden = step !== 1;
      if (confirmPanel) confirmPanel.hidden = step !== 2;
      if (resultPanel) resultPanel.hidden = step !== 3;

      if (step === 1) {
        if (cancelButton) {
          cancelButton.hidden = false;
          cancelButton.style.display = '';
        }
        submitButton.textContent = '결제 진행';
        submitButton.disabled = !waitingRoomReady || selectedSeats.size === 0;
      } else if (step === 2) {
        if (cancelButton) {
          cancelButton.hidden = false;
          cancelButton.style.display = '';
        }
        submitButton.textContent = '확인';
        submitButton.disabled = selectedSeats.size === 0;
        updateConfirmPanel();
      } else {
        // 결제 완료 화면은 "닫기"만 보여야 한다(영화 모달과 동일 UX).
        if (cancelButton) {
          cancelButton.hidden = true;
          cancelButton.style.display = 'none';
        }
        submitButton.textContent = '닫기';
        submitButton.disabled = false;
        if (resultMessage) {
          resultMessage.textContent = lastResult && lastResult.message ? lastResult.message : '';
        }
      }
    }

    async function acquirePermitOrThrow() {
      const userId = getStoredUserId();
      if (!userId) {
        throw new Error('로그인이 필요합니다.');
      }

      if (permitToken) {
        await refreshSeatSnapshotAfterWaitingRoom();
        startHoldRevPoll();
        return permitToken;
      }

      // "대기열" UI는 즉시 띄우지 말고 잠깐 지연 후에도 admit이 안 되면 그때만 보여준다(깜빡임 방지).
      let wrTimer = null;
      try {
        wrTimer = setTimeout(() => {
          if (!waitingRoomReady) {
            showWaitingRoomOverlay();
          }
        }, 250);
      } catch (e) {
        wrTimer = null;
      }

      // 1) enter (네트워크 일시 장애는 재시도)
      if (!waitingRoomRef) {
        const enterDeadline = Date.now() + 30_000;
        while (Date.now() < enterDeadline) {
          try {
            const enter = await enterWaitingRoom(showId, userId);
            const qref = enter && enter.queue_ref ? String(enter.queue_ref) : '';
            if (qref) {
              waitingRoomRef = qref;
              const seq = enter && Number.isFinite(Number(enter.seq)) ? Number(enter.seq) : 0;
              if (seq > 0) waitingRoomSeq = seq;
              if (wrUi.pos && waitingRoomSeq > 0) wrUi.pos.textContent = String(waitingRoomSeq);
              break;
            }
          } catch (e) {
            // 실제로 대기열 단계가 필요할 때만 오버레이를 보여준다.
            showWaitingRoomOverlay();
            if (wrUi.msg) wrUi.msg.textContent = '서버 연결이 불안정합니다. 재시도 중…';
          }
          await new Promise((r) => setTimeout(r, 500));
        }
        if (!waitingRoomRef) {
          if (wrTimer) clearTimeout(wrTimer);
          throw new Error('서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.');
        }
      }

      const deadline = Date.now() + 10 * 60 * 1000;
      // 폴링 간격을 늘려 서버 부담/콘솔 에러 스팸을 줄인다.
      // - 정상: 약 1초 간격
      // - 에러: 점진적 백오프(최대 4초)
      const BASE_POLL_MS = 1000;
      const MAX_POLL_MS = 4000;
      const METRICS_POLL_MS = 3000;
      let pollMs = BASE_POLL_MS;
      let lastMetricsAt = 0;
      let lastBacklog = 0;
      while (Date.now() < deadline) {
        let st = null;
        try {
          st = await waitingRoomStatus(waitingRoomRef);
          pollMs = BASE_POLL_MS;
        } catch (e) {
          showWaitingRoomOverlay();
          if (wrUi.msg) wrUi.msg.textContent = '서버 연결이 불안정합니다. 재시도 중…';
          pollMs = Math.min(MAX_POLL_MS, Math.floor(pollMs * 1.6));
          await new Promise((r) => setTimeout(r, pollMs));
          continue;
        }

        if (st && st.status === 'ADMITTED' && st.permit_token) {
          permitToken = String(st.permit_token);
          waitingRoomReady = true;
          if (wrTimer) clearTimeout(wrTimer);
          await refreshSeatSnapshotAfterWaitingRoom();
          startHoldRevPoll();
          setSeatUiEnabled(true);
          hideWaitingRoomOverlay();
          return permitToken;
        }

        // 아직 admit이 아니면(대기열 순번이 생기든, 그냥 대기 중이든) 오버레이를 보여준다.
        showWaitingRoomOverlay();
        const q = st && st.queue ? st.queue : null;
        const position = q && Number.isFinite(Number(q.position)) ? Number(q.position) : 0;
        const ahead = q && Number.isFinite(Number(q.ahead)) ? Number(q.ahead) : Math.max(0, position - 1);
        if (wrUi.pos) wrUi.pos.textContent = position > 0 ? String(position) : (waitingRoomSeq > 0 ? String(waitingRoomSeq) : '-');
        if (wrUi.eta) {
          const etaSec = st && Number.isFinite(Number(st.eta_sec)) ? Number(st.eta_sec) : 0;
          if (etaSec > 0) {
            const m = Math.floor(etaSec / 60);
            const s = Math.floor(etaSec % 60);
            wrUi.eta.textContent = m > 0 ? `약 ${m}분 ${s}초` : `약 ${s}초`;
          } else {
            // ahead=0 이거나 rate 계산 불가(일시 오류) 등
            wrUi.eta.textContent = '-';
          }
        }

        // backlog는 metrics가 있으면 그걸 우선, 없으면 ahead 기반 근사
        if (wrUi.backlog) {
          // metrics는 매 루프마다 치지 말고(부하 큼) 3초에 한 번만 갱신
          let backlog = ahead > 0 ? ahead + 1 : 0;
          const now = Date.now();
          if (now - lastMetricsAt >= METRICS_POLL_MS) {
            lastMetricsAt = now;
            try {
              const m = await waitingRoomMetrics(showId);
              if (m && m.ok && Number.isFinite(Number(m.backlog))) {
                lastBacklog = Number(m.backlog);
              }
            } catch (e) { /* ignore */ }
          }
          backlog = lastBacklog > 0 ? lastBacklog : backlog;
          wrUi.backlog.textContent = backlog > 0 ? backlog.toLocaleString('ko-KR') : '-';
        }
        if (wrUi.msg) {
          const ctl = st && st.control ? st.control : null;
          const msg = ctl && ctl.message ? String(ctl.message) : '';
          wrUi.msg.textContent = msg;
        }

        // 기존 자리(queueNode)는 보조로만 유지(상단 얇은 라인)
        // 실제로 "대기열"이 의미 있을 때만 노출해서, 빈 배지가 레이아웃/시선을 방해하지 않게 한다.
        if (queueNode) {
          if (position > 0 || ahead > 0) {
            queueNode.hidden = false;
            queueNode.textContent = `대기열 ${position > 0 ? position : 1}번째 (앞에 ${ahead}명)`;
          } else {
            queueNode.hidden = true;
            queueNode.textContent = '';
          }
        }
        // 약간의 지터를 넣어 동시 폴링 피크를 완화
        const jitter = Math.floor(Math.random() * 180);
        await new Promise((r) => setTimeout(r, Math.min(MAX_POLL_MS, pollMs) + jitter));
      }
      if (wrTimer) clearTimeout(wrTimer);
      throw new Error('대기열 처리 시간이 초과되었습니다.');
    }

    function handleSeatClick(button, seatKey) {
      if (button.disabled) return;

      if (selectedSeats.has(seatKey)) {
        selectedSeats.delete(seatKey);
        button.classList.remove('is-selected');
      } else {
        if (selectedSeats.size >= Math.max(1, remainCount)) {
          alert('선택 가능한 좌석 수를 초과했습니다.');
          return;
        }
        selectedSeats.add(seatKey);
        button.classList.add('is-selected');
      }

      updateSummary();
    }

    function renderSmallGrid() {
      for (let row = 1; row <= seatRows; row += 1) {
        const rowWrap = document.createElement('div');
        rowWrap.className = 'theaters-detail-seat-row';

        const rowLabel = document.createElement('div');
        rowLabel.className = 'theaters-detail-seat-row-label';
        rowLabel.textContent = `${row}열`;
        rowWrap.appendChild(rowLabel);

        const rowSeats = document.createElement('div');
        rowSeats.className = 'theaters-detail-seat-row-seats';

        for (let col = 1; col <= seatCols; col += 1) {
          const seatKey = createSeatKey(row, col);
          const seat = document.createElement('button');
          seat.type = 'button';
          seat.className = 'theaters-detail-seat';
          seat.textContent = String(col);
          seat.setAttribute('aria-label', createSeatLabel(row, col));
          seat.dataset.seatKey = seatKey;

          if (confirmedSeats.has(seatKey)) {
            seat.classList.add('is-disabled');
            seat.disabled = true;
          } else if (holdSeats.has(seatKey)) {
            seat.classList.add('is-hold');
            seat.disabled = true;
          }

          seat.addEventListener('click', function () {
            handleSeatClick(seat, seatKey);
          });

          rowSeats.appendChild(seat);
        }

        rowWrap.appendChild(rowSeats);
        seatGrid.appendChild(rowWrap);
      }
    }

    function renderLargeGrid() {
      // Large venue: avoid 10k~100k DOM nodes.
      // UX: interpark-like seat selection -> legend + row buttons + seat number pages + jump search.
      // 한 회차의 seat_cols가 큰 공연(예: 250/500/1000+)에서
      // "좌석 다음"을 너무 자주 눌러야 하는 UX를 줄이기 위해 페이지 단위를 크게 잡는다.
      // (UI는 10열 그리드로 촘촘하게 렌더링하므로 한 페이지에 100~200개 정도가 한눈에 들어온다.)
      const COLS_PER_PAGE = seatCols; // 한 화면에 최대한 보이도록(페이지는 자동 1페이지로 수렴)
      const ROW_BUTTONS_PER_PAGE = 20;
      const GRID_COLS = 25;

      let activeRow = 1;
      let activeColPage = 1;
      let activeRowPage = 1;

      overlay.classList.add('is-large-venue');

      const controls = document.createElement('div');
      controls.className = 'concert-seat-controls';

      const legend = document.createElement('div');
      legend.className = 'concert-seat-legend';
      legend.innerHTML = `
        <span class="concert-seat-legend-item"><span class="concert-seat-dot"></span> 선택가능</span>
        <span class="concert-seat-legend-item"><span class="concert-seat-dot is-selected"></span> 선택</span>
        <span class="concert-seat-legend-item"><span class="concert-seat-dot is-hold"></span> 처리중</span>
        <span class="concert-seat-legend-item"><span class="concert-seat-dot is-disabled"></span> 예매완료</span>
      `;

      const rowButtonsWrap = document.createElement('div');
      rowButtonsWrap.className = 'concert-seat-row-buttons';

      const rowPaging = document.createElement('div');
      rowPaging.className = 'concert-seat-page';

      const rowPrev = document.createElement('button');
      rowPrev.type = 'button';
      rowPrev.className = 'concert-seat-nav-btn';
      rowPrev.textContent = '열 이전';

      const rowNext = document.createElement('button');
      rowNext.type = 'button';
      rowNext.className = 'concert-seat-nav-btn';
      rowNext.textContent = '열 다음';

      const pageInfo = document.createElement('div');
      pageInfo.className = 'concert-seat-page-info';

      // 좌석 이전/다음(컬럼 페이지)은 "한 화면에 최대한" 정책으로 제거한다.

      rowPaging.appendChild(rowPrev);
      rowPaging.appendChild(rowNext);

      const topLine = document.createElement('div');
      topLine.className = 'concert-seat-topline';
      topLine.appendChild(pageInfo); // "선택 : 2열 / 좌석 ..." 을 맨 위 중앙

      const rowLine = document.createElement('div');
      rowLine.className = 'concert-seat-rowline';
      rowLine.appendChild(legend);
      rowLine.appendChild(rowPaging); // 우측에 "열 이전/열 다음"

      const rowMoreHint = document.createElement('div');
      rowMoreHint.className = 'concert-row-more-hint';
      rowMoreHint.textContent = '다음';

      const nextRange = document.createElement('div');
      nextRange.className = 'concert-row-next-range';
      nextRange.hidden = true;

      const rowsWrap = document.createElement('div');
      rowsWrap.className = 'concert-seat-rowswrap';
      rowsWrap.appendChild(rowButtonsWrap);
      rowsWrap.appendChild(rowMoreHint);
      rowsWrap.appendChild(nextRange);

      controls.appendChild(topLine);
      controls.appendChild(rowLine);
      controls.appendChild(rowsWrap);

      const rowWrap = document.createElement('div');
      rowWrap.className = 'theaters-detail-seat-row';

      const rowLabel = document.createElement('div');
      rowLabel.className = 'theaters-detail-seat-row-label';
      rowLabel.textContent = '1열';
      rowWrap.appendChild(rowLabel);

      const rowSeats = document.createElement('div');
      rowSeats.className = 'theaters-detail-seat-row-seats';
      rowSeats.style.gridTemplateColumns = `repeat(${Math.min(COLS_PER_PAGE, GRID_COLS)}, minmax(0, 1fr))`;
      rowWrap.appendChild(rowSeats);

      seatGrid.appendChild(controls);
      seatGrid.appendChild(rowWrap);

      function getRowLabel(row) {
        return `${row}열`;
      }

      function renderRowButtons() {
        const totalRowPages = Math.max(1, Math.ceil(seatRows / ROW_BUTTONS_PER_PAGE));
        activeRowPage = Math.min(Math.max(activeRowPage, 1), totalRowPages);

        const startRow = (activeRowPage - 1) * ROW_BUTTONS_PER_PAGE + 1;
        const endRow = Math.min(seatRows, startRow + ROW_BUTTONS_PER_PAGE - 1);
        const nextStart = endRow + 1;
        const nextEnd = Math.min(seatRows, endRow + ROW_BUTTONS_PER_PAGE);

        rowButtonsWrap.innerHTML = '';
        for (let r = startRow; r <= endRow; r += 1) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'concert-seat-row-btn';
          btn.textContent = getRowLabel(r);
          if (r === activeRow) btn.classList.add('is-active');
          btn.addEventListener('click', function () {
            activeRow = r;
            activeColPage = 1;
            renderRowButtons();
            renderRowPage();
          });
          rowButtonsWrap.appendChild(btn);
        }

        rowPrev.disabled = activeRowPage <= 1;
        rowNext.disabled = activeRowPage >= totalRowPages;
        rowMoreHint.hidden = activeRowPage >= totalRowPages;
        if (nextStart <= seatRows) {
          nextRange.hidden = false;
          nextRange.textContent = `${nextStart}~${nextEnd}열`;
        } else {
          nextRange.hidden = true;
          nextRange.textContent = '';
        }
      }

      function renderRowPage() {
        const totalSeatPages = Math.max(1, Math.ceil(seatCols / COLS_PER_PAGE));
        activeColPage = Math.min(Math.max(activeColPage, 1), totalSeatPages);

        const startCol = (activeColPage - 1) * COLS_PER_PAGE + 1;
        const endCol = Math.min(seatCols, startCol + COLS_PER_PAGE - 1);

        rowLabel.textContent = getRowLabel(activeRow);

        rowSeats.innerHTML = '';
        // keep grid visually dense
        const visibleCols = endCol - startCol + 1;
        rowSeats.style.gridTemplateColumns = `repeat(${Math.min(visibleCols, GRID_COLS)}, minmax(0, 1fr))`;

        for (let col = startCol; col <= endCol; col += 1) {
          const seatKey = createSeatKey(activeRow, col);
          const seat = document.createElement('button');
          seat.type = 'button';
          seat.className = 'theaters-detail-seat';
          seat.textContent = String(col);
          seat.setAttribute('aria-label', createSeatLabel(activeRow, col));
          seat.dataset.seatKey = seatKey;

          if (confirmedSeats.has(seatKey)) {
            seat.classList.add('is-disabled');
            seat.disabled = true;
          } else if (holdSeats.has(seatKey)) {
            seat.classList.add('is-hold');
            seat.disabled = true;
          }
          if (selectedSeats.has(seatKey)) {
            seat.classList.add('is-selected');
          }

          seat.addEventListener('click', function () {
            handleSeatClick(seat, seatKey);
          });

          rowSeats.appendChild(seat);
        }

        pageInfo.textContent = `현재위치: ${getRowLabel(activeRow)} / 좌석 ${startCol}~${endCol}`;
      }

      rowPrev.addEventListener('click', function () {
        activeRowPage -= 1;
        renderRowButtons();
      });

      rowNext.addEventListener('click', function () {
        activeRowPage += 1;
        renderRowButtons();
      });

      // 좌석번호 이동은 "한 화면에 최대한" 정책으로 제거 (UI 단순화)

      renderRowButtons();
      renderRowPage();
    }

    const totalSeats = seatRows * seatCols;
    if (totalSeats > 1500) {
      renderLargeGrid();
    } else {
      renderSmallGrid();
    }

    updateSummary();
    setStep(1);

    // 강한 연출: 모달 진입 즉시 "입장 대기열"부터 시작 → 통과 전 좌석 선택/다음 단계 막기
    setSeatUiEnabled(false);
    // 좌석 "처리중(주황)"은 모달 오픈 즉시부터 관측(대기열 통과 전이라도 다른 유저/부하의 변화를 볼 수 있게)
    startHoldRevPoll();
    (async function () {
      try {
        const userId = getStoredUserId();
        if (!userId) {
          alert('로그인이 필요합니다.');
          if (typeof window.openLoginPage === 'function') {
            window.openLoginPage();
          }
          closeModalFromUser(false);
          return;
        }
        await acquirePermitOrThrow();
        waitingRoomReady = true;
        setSeatUiEnabled(true);
        updateSummary();
      } catch (e) {
        console.error(e);
        alert(e && e.message ? e.message : '대기열 처리 실패');
        closeModalFromUser(false);
      }
    })();

    closeButton.addEventListener('click', function () {
      closeModalFromUser(bookingSucceeded);
    });
    cancelButton.addEventListener('click', function () {
      closeModalFromUser(bookingSucceeded);
    });

    overlay.addEventListener('click', function (event) {
      if (event.target === overlay) {
        closeModalFromUser(bookingSucceeded);
      }
    });

    document.addEventListener(
      'keydown',
      function escHandler(event) {
        if (event.key === 'Escape') {
          document.removeEventListener('keydown', escHandler);
          closeModalFromUser(bookingSucceeded);
        }
      },
      { once: true }
    );

    submitButton.addEventListener('click', async function () {
      if (currentStep === 1) {
        if (!selectedSeats.size) {
          alert('좌석을 먼저 선택해주세요.');
          return;
        }

        const userId = getStoredUserId();
        if (!userId) {
          alert('로그인이 필요합니다.');
          if (typeof window.openLoginPage === 'function') {
            window.openLoginPage();
          }
          return;
        }

        setStep(2);
        return;
      }

      if (currentStep === 2) {
        const userId = getStoredUserId();
        if (!userId) {
          alert('로그인이 필요합니다.');
          if (typeof window.openLoginPage === 'function') {
            window.openLoginPage();
          }
          return;
        }

        submitButton.disabled = true;
        submitButton.textContent = '처리 중...';
        if (queueNode) {
          queueNode.hidden = true;
          queueNode.textContent = '';
        }

        try {
          await refreshSeatSnapshotAfterWaitingRoom();
          const permit = await acquirePermitOrThrow();
          const commit = await requestConcertBooking({
            user_id: userId,
            show_id: showId,
            seats: Array.from(selectedSeats)
            ,
            permit_token: permit,
            // permit TTL 만료/중복좌석 재시도 시, 서버가 status(queue_ref)로 즉시 새 permit 발급해 우선 처리할 수 있게 전달
            queue_ref: waitingRoomRef
          });

          let result = commit;
          if (
            commit &&
            commit.ok &&
            String(commit.code || '') === 'QUEUED' &&
            commit.booking_ref &&
            typeof pollAsyncBookingStatus === 'function'
          ) {
            submitButton.textContent = '예매 처리 중…';
            const ref = encodeURIComponent(String(commit.booking_ref).trim());
            result = await pollAsyncBookingStatus(`/concerts/booking/status/${ref}`, {
              timeoutSec: 600,
              intervalMs: 400,
              onProgress(status) {
                const q = status && status.queue ? status.queue : null;
                const position = q && Number.isFinite(Number(q.position)) ? Number(q.position) : 0;
                const ahead = q && Number.isFinite(Number(q.ahead)) ? Number(q.ahead) : Math.max(0, position - 1);
                if (queueNode) {
                  if (position > 0 || ahead > 0) {
                    queueNode.hidden = false;
                    queueNode.textContent = `대기열 ${position > 0 ? position : 1}번째 (앞에 ${ahead}명)`;
                  } else {
                    queueNode.hidden = true;
                    queueNode.textContent = '';
                  }
                }
                if (position > 0) {
                  submitButton.textContent = `대기열 ${position}번째…`;
                } else {
                  submitButton.textContent = '예매 처리 중…';
                }
              }
            });
          }

          if (result && result.ok === true && String(result.code || '') === 'OK') {
            bookingSucceeded = true;
            const bookingCode = result.booking_code ? String(result.booking_code) : '';
            lastResult = {
              ok: true,
              message: `${bookingCode ? `예매번호 : ${bookingCode}\n` : ''}결제에 성공했습니다.`
            };
            onBooked({
              show_id: showId,
              selectedSeats: Array.from(selectedSeats)
            });
          } else if (result && result.ok === false) {
            const code = result.code ? String(result.code) : 'ERROR';
            if (code === 'DUPLICATE_SEAT') {
              lastResult = { ok: false, message: '중복좌석입니다.' };
            } else if (code === 'SOLD_OUT') {
              lastResult = { ok: false, message: '매진입니다.' };
            } else if (code === 'SALES_CLOSED') {
              lastResult = { ok: false, message: '모든 투표가 마감되었습니다.' };
            } else if (code === 'INVALID_SEAT' || code === 'BAD_SEAT_KEY') {
              lastResult = { ok: false, message: '좌석 정보가 올바르지 않습니다. 새로고침 후 다시 시도해주세요.' };
            } else if (code === 'TIMEOUT') {
              lastResult = { ok: false, message: result.message || '처리 시간이 초과되었습니다. 마이페이지에서 예매 내역을 확인해 주세요.' };
            } else if (code === 'NOT_FOUND') {
              lastResult = { ok: false, message: '회차를 찾을 수 없습니다.' };
            } else if (code === 'ERROR') {
              lastResult = { ok: false, message: result.message || '예매 처리 중 오류가 발생했습니다.' };
            } else {
              lastResult = { ok: false, message: '결제 실패' };
            }
          } else if (result && (result.status === 'UNKNOWN_OR_EXPIRED' || result.status === 'INVALID_REF')) {
            lastResult = {
              ok: false,
              message: result.message || '요청을 찾을 수 없습니다. 다시 시도해 주세요.'
            };
          } else {
            lastResult = { ok: false, message: '결제 실패' };
          }

          setStep(3);
        } catch (error) {
          console.error(error);
          const status = error && error.status ? Number(error.status) : 0;
          const errData = error && error.data ? error.data : null;
          if ((status === 400 || status === 409) && errData && errData.code) {
            const code = String(errData.code);
            if (code === 'INVALID_SEAT' || code === 'BAD_SEAT_KEY') {
              lastResult = { ok: false, message: '좌석 정보가 올바르지 않습니다. 새로고침 후 다시 시도해주세요.' };
            } else if (code === 'NO_SEATS') {
              lastResult = { ok: false, message: '좌석을 선택해주세요.' };
            } else if (code === 'DUPLICATE_SEAT' || code === 'CONFIRMED_SEAT') {
              lastResult = { ok: false, message: '중복좌석입니다.' };
            } else if (code === 'SOLD_OUT') {
              lastResult = { ok: false, message: '매진입니다.' };
            } else if (code === 'SALES_CLOSED') {
              lastResult = { ok: false, message: '모든 투표가 마감되었습니다.' };
            } else if (code === 'WAITING_ROOM_REQUIRED') {
              lastResult = { ok: false, message: '대기열 처리 중입니다. 잠시 후 다시 시도해주세요.' };
            } else {
              lastResult = { ok: false, message: '요청값이 올바르지 않습니다.' };
            }
          } else {
            lastResult = { ok: false, message: error && error.message ? error.message : '결제 실패' };
          }
          setStep(3);
        } finally {
          submitButton.disabled = false;
          submitButton.textContent = currentStep === 2 ? '확인' : submitButton.textContent;
        }

        return;
      }

      if (currentStep === 3) {
        closeModalFromUser(bookingSucceeded);
      }
    });

    if (reselectButton) {
      reselectButton.addEventListener('click', function () {
        bookingSucceeded = false;
        setStep(1);
      });
    }
  }

  window.openConcertBookingModal = openConcertBookingModal;
})();

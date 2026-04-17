(function () {
  const THEATERS_DETAIL_CSS_PATH = '/css/theaters/theaters_detail.css';
  // 캐시로 CSS가 안 바뀌는 경우가 있어 버전 쿼리를 붙입니다.
  const THEATERS_DETAIL_CSS_URL = `${THEATERS_DETAIL_CSS_PATH}?v=20260413_result_actions`;
  const OVERLAY_ID = 'theaters-booking-detail-overlay';
  const BODY_ACTIVE_CLASS = 'theaters-booking-modal-open';

  function ensureDetailCss() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureStyle === 'function') {
      return window.APP_RUNTIME.ensureStyle(THEATERS_DETAIL_CSS_URL);
    }

    const exists = document.querySelector(
      `link[href="${THEATERS_DETAIL_CSS_PATH}"], link[href="${THEATERS_DETAIL_CSS_URL}"]`
    );
    if (exists) return Promise.resolve(exists);

    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = THEATERS_DETAIL_CSS_URL;
    document.head.appendChild(link);
    return Promise.resolve(link);
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
    const overlay = document.getElementById(OVERLAY_ID);
    if (overlay) overlay.remove();
    unlockBodyScroll();
  }

  /** 모달만 닫음. 잔여석은 theaters_main 의 onBooked + (선택) 극장 상세 재조회로 갱신 — 전체 reload 는 불필요한 버퍼링만 유발 */
  function closeModalFromUser() {
    closeModal();
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
    const rowLabel = rowNo === 1 ? 'A열' : rowNo === 2 ? 'B열' : rowNo === 3 ? 'C열' : `${rowNo}열`;
    return `${rowLabel} ${col}번`;
  }

  async function requestBooking(payload) {
    if (typeof writeApi !== 'function') {
      throw new Error('writeApi가 없습니다.');
    }

    return await writeApi('/theaters/booking/commit', 'POST', payload);
  }

  async function openTheatersDetail(options) {
    await ensureDetailCss();
    closeModal();
    lockBodyScroll();

    const data = options && typeof options === 'object' ? options : {};
    const schedule = data.schedule || {};
    const hall = data.hall || {};
    const theater = data.theater || {};
    const movie = data.movie || {};
    const onBooked = typeof data.onBooked === 'function' ? data.onBooked : function () {};
    const reservedSeats = new Set(Array.isArray(data.reservedSeats) ? data.reservedSeats.map((value) => String(value)) : []);

    const runtimeMinutes = toInt(movie.runtime_minutes || 120);
    const start = parseDateValue(schedule.show_date);
    const end = addMinutes(schedule.show_date, runtimeMinutes);

    // 좌석 UI는 schedule.total_count 기준으로 10열 고정(최대 30석=3x10)으로 맞춥니다.
    // hall_seats 가 덜 들어간 환경에서도 UI가 20칸으로 축소되지 않도록 합니다.
    const seatCols = 10;
    const remainCount = Math.max(0, toInt(schedule.remain_count || 0));
    const totalCount = Math.max(0, toInt(schedule.total_count || 0)) || Math.max(1, toInt(hall.total_seats || 30));
    const seatRows = Math.max(1, Math.ceil(totalCount / seatCols));
    const price = Math.max(0, toInt(schedule.price || 14000));
    const selectedSeats = new Set();
    let currentStep = 1; // 1: 좌석선택, 2: 결제확인(화면만), 3: 결과(화면만)
    let lastResult = null; // { ok: boolean, message: string }

    const overlay = document.createElement('div');
    overlay.id = OVERLAY_ID;
    overlay.className = 'theaters-detail-overlay';
    overlay.innerHTML = `
      <div class="theaters-detail-shell" role="dialog" aria-modal="true" aria-label="예매 진행">
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
            <div class="theaters-detail-title">${escapeHtml(formatTime(start))}~${escapeHtml(formatTime(end))} (${escapeHtml(hall.hall_name || '상영관')})</div>
            <button type="button" class="theaters-detail-close" aria-label="닫기">×</button>
          </div>

          <div class="theaters-detail-body">
            <div class="theaters-detail-summary">
              <div class="theaters-detail-movie">${escapeHtml(movie.title || '영화')}</div>
              <div class="theaters-detail-place">${escapeHtml(theater.theater_name || '극장')} · ${escapeHtml(hall.hall_name || '상영관')}</div>
              <div class="theaters-detail-date">${escapeHtml(String(schedule.show_date || '').slice(0, 16))}</div>
            </div>

            <section class="theaters-detail-panel theaters-detail-panel-seat" data-panel="1">
              <div class="theaters-detail-seat-count">잔여좌석 <strong class="theaters-detail-remain">${escapeHtml(String(remainCount))}</strong><span>/${escapeHtml(String(totalCount))}</span></div>
              <div class="theaters-detail-screen-label">SCREEN</div>
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
                <div class="theaters-detail-confirm-row"><span>영화</span><strong class="theaters-detail-confirm-movie"></strong></div>
                <div class="theaters-detail-confirm-row"><span>극장</span><strong class="theaters-detail-confirm-theater"></strong></div>
                <div class="theaters-detail-confirm-row"><span>상영관</span><strong class="theaters-detail-confirm-hall"></strong></div>
                <div class="theaters-detail-confirm-row"><span>일시</span><strong class="theaters-detail-confirm-date"></strong></div>
                <div class="theaters-detail-confirm-row"><span>좌석</span><strong class="theaters-detail-confirm-seats"></strong></div>
                <div class="theaters-detail-confirm-row"><span>인원</span><strong class="theaters-detail-confirm-count"></strong></div>
                <div class="theaters-detail-confirm-row"><span>결제금액</span><strong class="theaters-detail-confirm-price"></strong></div>
              </div>
              <div class="theaters-detail-queue" hidden aria-live="polite"></div>
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
    const actionsRow = overlay.querySelector('.theaters-detail-actions');
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

    function updateSummary() {
      const seatLabels = Array.from(selectedSeats).map((seatKey) => {
        const parts = seatKey.split('-');
        return createSeatLabel(parts[0], parts[1]);
      });

      selectedValue.textContent = seatLabels.length ? seatLabels.join(', ') : '없음';
      selectedCount.textContent = `${selectedSeats.size}명`;
      selectedPrice.textContent = `${(selectedSeats.size * price).toLocaleString('ko-KR')}원`;
      remainNode.textContent = String(Math.max(0, remainCount - selectedSeats.size));
      if (currentStep === 1) {
        submitButton.disabled = selectedSeats.size === 0;
      }
    }

    function updateConfirmPanel() {
      const seatLabels = Array.from(selectedSeats).map((seatKey) => {
        const parts = seatKey.split('-');
        return createSeatLabel(parts[0], parts[1]);
      });

      if (confirmMovie) confirmMovie.textContent = String(movie.title || '');
      if (confirmTheater) confirmTheater.textContent = String(theater.theater_name || theater.address || '');
      if (confirmHall) confirmHall.textContent = String(hall.hall_name || '');
      if (confirmDate) confirmDate.textContent = String(schedule.show_date || '').slice(0, 16);
      if (confirmSeats) confirmSeats.textContent = seatLabels.length ? seatLabels.join(', ') : '없음';
      if (confirmCount) confirmCount.textContent = `${selectedSeats.size}명`;
      if (confirmPrice) confirmPrice.textContent = `${(selectedSeats.size * price).toLocaleString('ko-KR')}원`;
    }

    function setStep(step) {
      currentStep = step;

      stepButtons.forEach((btn) => {
        const isActive = String(btn.getAttribute('data-step')) === String(step);
        btn.classList.toggle('is-active', isActive);
      });

      if (seatPanel) seatPanel.hidden = step !== 1;
      if (confirmPanel) confirmPanel.hidden = step !== 2;
      if (resultPanel) resultPanel.hidden = step !== 3;

      if (step === 3) {
        if (cancelButton) cancelButton.hidden = true;
        if (actionsRow) actionsRow.classList.add('is-result-step');
      } else {
        if (cancelButton) cancelButton.hidden = false;
        if (actionsRow) actionsRow.classList.remove('is-result-step');
      }

      if (step === 1) {
        submitButton.textContent = '결제 진행';
        submitButton.disabled = selectedSeats.size === 0;
      } else if (step === 2) {
        submitButton.textContent = '확인';
        submitButton.disabled = selectedSeats.size === 0;
        updateConfirmPanel();
      } else {
        submitButton.textContent = '닫기';
        submitButton.disabled = false;
        if (resultMessage) {
          resultMessage.textContent = lastResult && lastResult.message ? lastResult.message : '';
        }
      }
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

    for (let row = 1; row <= seatRows; row += 1) {
      const rowWrap = document.createElement('div');
      rowWrap.className = 'theaters-detail-seat-row';

      const rowLabel = document.createElement('div');
      rowLabel.className = 'theaters-detail-seat-row-label';
      rowLabel.textContent = row === 1 ? 'A열' : row === 2 ? 'B열' : row === 3 ? 'C열' : `${row}열`;
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

        if (reservedSeats.has(seatKey)) {
          seat.classList.add('is-disabled');
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

    updateSummary();
    setStep(1);

    closeButton.addEventListener('click', function () {
      closeModalFromUser();
    });
    cancelButton.addEventListener('click', function () {
      closeModalFromUser();
    });

    overlay.addEventListener('click', function (event) {
      if (event.target === overlay) {
        closeModalFromUser();
      }
    });

    document.addEventListener('keydown', function escHandler(event) {
      if (event.key === 'Escape') {
        document.removeEventListener('keydown', escHandler);
        closeModalFromUser();
      }
    }, { once: true });

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
          const commit = await requestBooking({
            user_id: userId,
            schedule_id: schedule.schedule_id,
            seats: Array.from(selectedSeats)
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
            result = await pollAsyncBookingStatus(`/booking/status/${ref}`, {
              timeoutSec: 600,
              intervalMs: 400,
              onProgress(status) {
                const q = status && status.queue ? status.queue : null;
                const position = q && Number.isFinite(Number(q.position)) ? Number(q.position) : 0;
                const ahead = q && Number.isFinite(Number(q.ahead)) ? Number(q.ahead) : Math.max(0, position - 1);
                if (queueNode) {
                  queueNode.hidden = false;
                  if (position > 0) {
                    queueNode.textContent = `대기열 ${position}번째 (앞에 ${ahead}명)`;
                  } else {
                    queueNode.textContent = '대기열 진입 중…';
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
            const bookingCode = result.booking_code ? String(result.booking_code) : '';
            lastResult = {
              ok: true,
              message: `${bookingCode ? `예매번호 : ${bookingCode}\n` : ''}결제에 성공했습니다.`
            };
            onBooked({
              schedule_id: schedule.schedule_id,
              selectedSeats: Array.from(selectedSeats)
            });
          } else if (result && result.ok === false) {
            const code = result.code ? String(result.code) : 'ERROR';
            if (code === 'DUPLICATE_SEAT') {
              lastResult = { ok: false, message: '중복좌석입니다.' };
            } else if (code === 'SOLD_OUT') {
              lastResult = { ok: false, message: '매진입니다.' };
            } else if (code === 'INVALID_SEAT' || code === 'BAD_SEAT_KEY') {
              lastResult = { ok: false, message: '좌석 정보가 올바르지 않습니다. 새로고침 후 다시 시도해주세요.' };
            } else if (code === 'TIMEOUT') {
              lastResult = { ok: false, message: result.message || '처리 시간이 초과되었습니다. 마이페이지에서 예매 내역을 확인해 주세요.' };
            } else if (code === 'NOT_FOUND') {
              lastResult = { ok: false, message: '상영 회차를 찾을 수 없습니다.' };
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

          if (window.APP_RUNTIME && typeof window.APP_RUNTIME.notifyReadCacheRebuilt === 'function') {
            window.APP_RUNTIME.notifyReadCacheRebuilt();
          }

          setStep(3);
        } catch (error) {
          console.error(error);
          const status = error && error.status ? Number(error.status) : 0;
          const data = error && error.data ? error.data : null;
          if (status === 400 && data && data.code) {
            const code = String(data.code);
            if (code === 'INVALID_SEAT' || code === 'BAD_SEAT_KEY') {
              lastResult = { ok: false, message: '좌석 정보가 올바르지 않습니다. 새로고침 후 다시 시도해주세요.' };
            } else if (code === 'NO_SEATS') {
              lastResult = { ok: false, message: '좌석을 선택해주세요.' };
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
        closeModalFromUser();
      }
    });

    if (reselectButton) {
      reselectButton.addEventListener('click', function () {
        setStep(1);
      });
    }
  }

  window.openTheatersDetail = openTheatersDetail;
})();

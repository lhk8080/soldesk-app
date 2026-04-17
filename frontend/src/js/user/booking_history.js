(function () {
  const CSS_PATH = '/css/user/booking_history.css';
  const BOOKINGS_API = '/api/read/user/bookings';
  const REFUND_API = '/api/write/user/bookings/refund';
  const PAGE_SIZE = 10;
  const CANCEL_STATUSES = new Set(['CANCEL', 'CANCELED', 'CANCELLED']);

  const runtime = window.APP_RUNTIME || {};
  let currentPage = 1;
  let isRefunding = false;

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatDate(value) {
    if (!value) return '-';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  }

  function formatDateTime(value) {
    if (!value) return '-';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    const hh = String(d.getHours()).padStart(2, '0');
    const mi = String(d.getMinutes()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd} ${hh}:${mi}`;
  }

  /** DB 키 "행-열" → "1열 n번" 또는 "95열 18번" (화면 표기용) */
  function formatSeat(seatKey) {
    const parts = String(seatKey).split('-');
    if (parts.length !== 2) return escapeHtml(String(seatKey));
    const row = parseInt(parts[0], 10);
    const col = parseInt(parts[1], 10);
    if (Number.isNaN(row) || Number.isNaN(col)) return escapeHtml(String(seatKey));
    const rowLabel = `${row}열`;
    return escapeHtml(`${rowLabel} ${col}번`);
  }

  function isCancelled(booking) {
    return CANCEL_STATUSES.has(String(booking.book_status || '').toUpperCase());
  }

  function isExpired(booking) {
    if (!booking.show_date) return false;
    const showTime = new Date(booking.show_date).getTime();
    if (Number.isNaN(showTime)) return false;
    return showTime < Date.now();
  }

  function renderLayout() {
    return `
      <section id="booking-history-page">
        <div class="bh-wrap">
          <div class="bh-card">
            <div class="bh-title-box">
              <h2 class="bh-title">전체 예매 내역</h2>
            </div>
            <div class="bh-content">
              <div class="bh-back-row">
                <button type="button" id="bh-back-btn" class="bh-back-btn">&larr; 마이페이지</button>
              </div>
              <div id="bh-list-area">
                <div class="bh-loading">불러오는 중...</div>
              </div>
              <div id="bh-pagination-area"></div>
            </div>
          </div>
        </div>
      </section>
    `;
  }

  function bookingLineKey(b) {
    const kind = String(b.booking_kind || 'movie').toLowerCase() === 'concert' ? 'concert' : 'movie';
    return `${kind}:${b.booking_id}`;
  }

  function renderBookingItem(b) {
    const lineKey = bookingLineKey(b);
    const kind = String(b.booking_kind || 'movie').toLowerCase() === 'concert' ? 'concert' : 'movie';
    const cancelled = isCancelled(b);
    const expired = !cancelled && isExpired(b);
    const date = formatDate(b.booking_date);
    const typeBadge =
      kind === 'concert'
        ? '<span class="bh-item-type bh-item-type-concert">콘서트</span> '
        : '<span class="bh-item-type bh-item-type-movie">영화</span> ';
    const title = typeBadge + escapeHtml(b.movie_title || '-');
    const region = escapeHtml(b.region_name || '-');
    const count = b.reg_count || 0;
    const seats = (b.seats || []).map(formatSeat).join(', ') || '-';
    const showDate = formatDateTime(b.show_date);
    const theater = escapeHtml(b.theater_address || '-');
    const hall = escapeHtml(b.hall_name || '-');
    const code = escapeHtml(b.booking_code || '-');
    const payStatus = b.pay_yn === 'Y' ? '결제완료' : '미결제';

    let itemClass = '';
    if (cancelled) itemClass = ' is-cancelled';
    else if (expired) itemClass = ' is-expired';

    let summaryContent = '';
    let detailPayLabel = payStatus;

    if (cancelled) {
      summaryContent = `
        <span class="bh-item-date">${date}</span>
        <span class="bh-item-title">${title}</span>
        <span class="bh-item-hint">클릭 시 펼쳐집니다</span>
        <span class="bh-item-region">${region}</span>
        <span class="bh-item-count">${count}매</span>
        <span class="bh-item-refunded">-예매취소-</span>
      `;
      detailPayLabel = '환불완료';
    } else if (expired) {
      summaryContent = `
        <span class="bh-item-date">${date}</span>
        <span class="bh-item-title">${title}</span>
        <span class="bh-item-hint">클릭 시 펼쳐집니다</span>
        <span class="bh-item-region">${region}</span>
        <span class="bh-item-count">${count}매</span>
        <span class="bh-item-completed">-완료-</span>
      `;
      detailPayLabel = payStatus;
    } else {
      summaryContent = `
        <span class="bh-item-date">${date}</span>
        <span class="bh-item-title">${title}</span>
        <span class="bh-item-hint">클릭 시 펼쳐집니다</span>
        <span class="bh-item-region">${region}</span>
        <span class="bh-item-count">${count}매</span>
        <button type="button" class="bh-refund-btn" data-booking-id="${b.booking_id}" data-booking-kind="${kind}">환불</button>
      `;
    }

    return `
      <div class="bh-item${itemClass}" data-booking-line="${lineKey}">
        <div class="bh-item-summary" data-toggle-id="${lineKey}">
          ${summaryContent}
        </div>
        <div class="bh-item-detail" id="bh-detail-${kind}-${b.booking_id}" style="display:none;">
          <table class="bh-detail-table">
            <tbody>
              <tr><th>예매코드</th><td>${code}</td></tr>
              <tr><th>${kind === 'concert' ? '공연일시' : '상영일시'}</th><td>${showDate}</td></tr>
              <tr><th>${kind === 'concert' ? '장소' : '극장'}</th><td>${theater}</td></tr>
              <tr><th>${kind === 'concert' ? '홀' : '관'}</th><td>${hall}</td></tr>
              <tr><th>좌석</th><td>${seats}</td></tr>
              <tr><th>인원</th><td>${count}명</td></tr>
              <tr><th>결제상태</th><td>${detailPayLabel}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  function renderBookingList(bookings) {
    const area = document.getElementById('bh-list-area');
    if (!area) return;

    if (!bookings || bookings.length === 0) {
      area.innerHTML = '<div class="bh-empty">예매 내역이 없습니다</div>';
      return;
    }

    area.innerHTML = bookings.map(renderBookingItem).join('');
  }

  function renderPagination(page, totalPages) {
    const area = document.getElementById('bh-pagination-area');
    if (!area) return;

    if (totalPages <= 1) {
      area.innerHTML = '';
      return;
    }

    const maxVisible = 5;
    let startPage = Math.max(1, page - Math.floor(maxVisible / 2));
    let endPage = startPage + maxVisible - 1;
    if (endPage > totalPages) {
      endPage = totalPages;
      startPage = Math.max(1, endPage - maxVisible + 1);
    }

    let html = '<div class="bh-pagination">';

    if (page > 1) {
      html += `<button type="button" class="bh-page-btn" data-page="${page - 1}">&laquo;</button>`;
    }

    for (let i = startPage; i <= endPage; i++) {
      const activeClass = i === page ? ' is-active' : '';
      html += `<button type="button" class="bh-page-btn${activeClass}" data-page="${i}">${i}</button>`;
    }

    if (page < totalPages) {
      html += `<button type="button" class="bh-page-btn" data-page="${page + 1}">&raquo;</button>`;
    }

    html += '</div>';
    area.innerHTML = html;
  }

  function toggleDetail(lineKey) {
    const parts = String(lineKey || '').split(':');
    const kind = parts[0] === 'concert' ? 'concert' : 'movie';
    const bookingId = parts[1];
    if (!bookingId) return;
    const detail = document.getElementById(`bh-detail-${kind}-${bookingId}`);
    if (!detail) return;
    const isOpen = detail.style.display !== 'none';
    detail.style.display = isOpen ? 'none' : '';

    const item = detail.closest('.bh-item');
    if (item) {
      item.classList.toggle('is-open', !isOpen);
    }
  }

  async function handleRefund(bookingId, bookingKind) {
    if (isRefunding) return;

    const confirmed = window.confirm('해당 예매를 환불하시겠습니까?');
    if (!confirmed) return;

    const userId = runtime.getStoredUserId ? runtime.getStoredUserId() : '';
    if (!userId) {
      alert('로그인이 필요합니다.');
      return;
    }

    const kind = String(bookingKind || 'movie').toLowerCase() === 'concert' ? 'concert' : 'movie';

    isRefunding = true;
    try {
      const result = await runtime.postJson(REFUND_API, {
        user_id: userId,
        booking_id: bookingId,
        booking_kind: kind,
      });

      // 백엔드는 { ok: true, message } 형식; success 별칭은 구버전 호환
      if (result && (result.ok === true || result.success === true)) {
        alert(result.message || '환불이 완료되었습니다.');
        if (runtime && typeof runtime.notifyReadCacheRebuilt === 'function') {
          runtime.notifyReadCacheRebuilt();
        }
        await loadBookings(currentPage);
      } else {
        alert(result.message || '환불 처리에 실패했습니다.');
      }
    } catch (error) {
      alert(error.message || '환불 처리 중 오류가 발생했습니다.');
    } finally {
      isRefunding = false;
    }
  }

  async function loadBookings(page) {
    const userId = runtime.getStoredUserId ? runtime.getStoredUserId() : '';
    if (!userId) {
      renderBookingList([]);
      renderPagination(1, 1);
      return;
    }

    const area = document.getElementById('bh-list-area');
    if (area) area.innerHTML = '<div class="bh-loading">불러오는 중...</div>';

    try {
      const data = await runtime.getJson(BOOKINGS_API, {
        query: { user_id: userId, page: page, page_size: PAGE_SIZE },
        cache: 'no-store',
      });

      currentPage = data.page || 1;
      renderBookingList(data.bookings || []);
      renderPagination(data.page || 1, data.total_pages || 1);
    } catch (error) {
      console.error('[booking_history] load error:', error);
      if (area) area.innerHTML = '<div class="bh-empty">예매 내역을 불러올 수 없습니다</div>';
      renderPagination(1, 1);
    }
  }

  function bindEvents() {
    const backBtn = document.getElementById('bh-back-btn');
    if (backBtn) {
      backBtn.addEventListener('click', function () {
        if (typeof window.appNavigate === 'function') {
          window.appNavigate({ view: 'mypage' });
        }
      });
    }

    const page = document.getElementById('booking-history-page');
    if (page) {
      page.addEventListener('click', function (e) {
        const refundBtn = e.target.closest('.bh-refund-btn');
        if (refundBtn) {
          e.stopPropagation();
          const bookingId = refundBtn.dataset.bookingId;
          const bk = refundBtn.dataset.bookingKind || 'movie';
          if (bookingId) handleRefund(Number(bookingId), bk);
          return;
        }

        const summaryEl = e.target.closest('.bh-item-summary');
        if (summaryEl) {
          const toggleId = summaryEl.dataset.toggleId;
          if (toggleId) toggleDetail(toggleId);
          return;
        }

        const pageBtn = e.target.closest('.bh-page-btn');
        if (pageBtn) {
          const targetPage = Number(pageBtn.dataset.page);
          if (targetPage && targetPage !== currentPage) {
            loadBookings(targetPage);
            const wrap = document.querySelector('.bh-wrap');
            if (wrap) wrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }
          return;
        }
      });
    }
  }

  async function initBookingHistoryPage() {
    try {
      await runtime.ensureStyle(CSS_PATH);
    } catch (error) {
      console.error('[booking_history] css load error:', error);
    }

    if (runtime.resetPrimarySections) {
      runtime.resetPrimarySections();
    }

    const mainBody = runtime.ensureMainBody
      ? runtime.ensureMainBody()
      : document.getElementById('main-body');
    mainBody.innerHTML = renderLayout();

    bindEvents();
    await loadBookings(1);

    window.scrollTo({ top: 0, behavior: 'auto' });
  }

  window.openBookingHistory = initBookingHistoryPage;
})();

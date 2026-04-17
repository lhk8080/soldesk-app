(function () {
  const MY_PAGE_CSS_PATH = '/css/user/mypage.css';
  const MY_PAGE_READ_API = '/api/read/user/mypage';
  const RECENT_BOOKINGS_API = '/api/read/user/bookings/recent';
  const PREFETCH_SCRIPTS = ['/js/user/edit.js', '/js/user/changepw.js'];
  const runtime = window.APP_RUNTIME || {};

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatPhone(phone) {
    const raw = String(phone || '').replace(/[^0-9]/g, '');
    if (raw.length === 11) {
      return `${raw.slice(0, 3)}-${raw.slice(3, 7)}-${raw.slice(7)}`;
    }
    if (raw.length === 10) {
      return `${raw.slice(0, 3)}-${raw.slice(3, 6)}-${raw.slice(6)}`;
    }
    return phone || '-';
  }

  function formatJoinDate(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }

    const yyyy = date.getFullYear();
    const mm = String(date.getMonth() + 1).padStart(2, '0');
    const dd = String(date.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  }

  function renderMyPageLayout() {
    return `
      <section id="mypage-page">
        <div class="mypage-wrap">
          <div class="mypage-card">
            <div class="mypage-title-box">
              <h2 class="mypage-title">마이페이지</h2>
            </div>

            <div class="mypage-content">
              <div class="mypage-profile-box">
                <div class="mypage-profile-left">
                  <h3 id="mypage-profile-name" class="mypage-profile-name">회원정보</h3>

                  <table class="mypage-profile-table">
                    <tbody>
                      <tr>
                        <th>이름</th>
                        <td id="mypage-user-name">-</td>
                      </tr>
                      <tr>
                        <th>핸드폰번호</th>
                        <td id="mypage-user-phone">-</td>
                      </tr>
                      <tr>
                        <th>가입일</th>
                        <td id="mypage-user-created-at">-</td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <div class="mypage-profile-actions">
                  <button type="button" id="mypage-edit-button" class="mypage-action-button">수정</button>
                  <button type="button" id="mypage-password-button" class="mypage-action-button">비밀번호변경</button>
                </div>
              </div>

              <section class="mypage-section">
                <div class="mypage-section-head">
                  <h3 class="mypage-section-title">최근 예매 내역</h3>
                  <button type="button" id="mypage-view-all-bookings" class="mypage-section-link">전체내역 보기 &gt;</button>
                </div>

                <div id="mypage-booking-area">
                  <div class="mypage-empty-box">불러오는 중...</div>
                </div>
              </section>
            </div>
          </div>
        </div>
      </section>
    `;
  }

  function renderRecentBookings(bookings) {
    const area = document.getElementById('mypage-booking-area');
    if (!area) return;

    if (!bookings || bookings.length === 0) {
      area.innerHTML = '<div class="mypage-empty-box">예매 내역이 없습니다</div>';
      return;
    }

    let html = '<div class="mypage-recent-list">';
    for (const b of bookings) {
      const date = formatJoinDate(b.booking_date);
      const isConcert = String(b.booking_kind || '').toLowerCase() === 'concert';
      const badge = isConcert
        ? '<span class="mypage-recent-badge mypage-recent-badge-concert">콘서트</span> '
        : '<span class="mypage-recent-badge mypage-recent-badge-movie">영화</span> ';
      const title = badge + escapeHtml(b.movie_title || '-');
      const region = escapeHtml(b.region_name || '-');
      html += `
        <div class="mypage-recent-item">
          <span class="mypage-recent-date">${date}</span>
          <span class="mypage-recent-title">${title}</span>
          <span class="mypage-recent-region">${region}</span>
        </div>
      `;
    }
    html += '</div>';
    area.innerHTML = html;
  }

  function bindMyPageButtons() {
    const editButton = document.getElementById('mypage-edit-button');
    const passwordButton = document.getElementById('mypage-password-button');
    const viewAllButton = document.getElementById('mypage-view-all-bookings');

    if (editButton) {
      editButton.addEventListener('click', function () {
        if (typeof window.appNavigate === 'function') {
          window.appNavigate({ view: 'edit' });
        }
      });
    }

    if (passwordButton) {
      passwordButton.addEventListener('click', function () {
        if (typeof window.appNavigate === 'function') {
          window.appNavigate({ view: 'changepw' });
        }
      });
    }

    if (viewAllButton) {
      viewAllButton.addEventListener('click', function () {
        if (typeof window.appNavigate === 'function') {
          window.appNavigate({ view: 'booking_history' });
        }
      });
    }
  }

  async function loadMyPageUser() {
    const userId = runtime.getStoredUserId ? runtime.getStoredUserId() : '';
    if (!userId) {
      console.warn('[mypage] localStorage user_id not found');
      return;
    }

    const data = await runtime.getJson(MY_PAGE_READ_API, { query: { user_id: userId } });
    const user = data.user || data.result || data.data || data || {};
    const finalUserId = user.user_id || userId;

    if (runtime.setStoredUserId) {
      runtime.setStoredUserId(finalUserId);
    }
    if (runtime.patchLoginUser) {
      runtime.patchLoginUser({
        user_id: finalUserId,
        name: user.name,
        phone: user.phone,
        created_at: user.created_at
      });
    }

    const nameNode = document.getElementById('mypage-user-name');
    const phoneNode = document.getElementById('mypage-user-phone');
    const createdAtNode = document.getElementById('mypage-user-created-at');

    if (nameNode) nameNode.textContent = user.name || '-';
    if (phoneNode) phoneNode.textContent = formatPhone(user.phone);
    if (createdAtNode) createdAtNode.textContent = formatJoinDate(user.created_at);
  }

  async function loadRecentBookings() {
    const userId = runtime.getStoredUserId ? runtime.getStoredUserId() : '';
    if (!userId) {
      renderRecentBookings([]);
      return;
    }

    try {
      const data = await runtime.getJson(RECENT_BOOKINGS_API, { query: { user_id: userId } });
      renderRecentBookings(data.bookings || []);
    } catch (error) {
      console.error('[mypage] recent bookings load error:', error);
      renderRecentBookings([]);
    }
  }

  async function initMyPagePage() {
    try {
      await runtime.ensureStyle(MY_PAGE_CSS_PATH);
    } catch (error) {
      console.error('[mypage] css load error:', error);
    }

    if (runtime.resetPrimarySections) {
      runtime.resetPrimarySections();
    }

    const mainBody = runtime.ensureMainBody ? runtime.ensureMainBody() : document.getElementById('main-body');
    mainBody.innerHTML = renderMyPageLayout();

    bindMyPageButtons();

    if (typeof window.appPrefetchScripts === 'function') {
      window.appPrefetchScripts(PREFETCH_SCRIPTS);
    }

    try {
      await loadMyPageUser();
    } catch (error) {
      console.error('[mypage] user load error:', error);
    }

    await loadRecentBookings();

    window.scrollTo({ top: 0, behavior: 'auto' });
  }

  window.openMyPage = initMyPagePage;
})();

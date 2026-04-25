(function () {
  const EDIT_CSS_PATH = '/css/user/edit.css?v=4';
  const USER_READ_MYPAGE_API = '/api/read/user/mypage';
  const USER_WRITE_EDIT_API = '/api/write/auth/edit';
  const runtime = window.APP_RUNTIME || {};

  let isEditSubmitting = false;

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

  function renderEditLayout(user) {
    return `
      <section id="edit-page">
        <div class="edit-wrap">
          <div class="edit-card">
            <div class="edit-title-box">
              <h2 class="edit-title">회원정보 수정</h2>
            </div>

            <div class="edit-content">
              <form id="edit-form" class="edit-form" novalidate>
                <div class="edit-row">
                  <label class="edit-label" for="edit-name">이름</label>
                  <div class="edit-field">
                    <input type="text" id="edit-name" class="edit-input" maxlength="20" value="${user.name || ''}" />
                    <div id="edit-name-error" class="edit-error"></div>
                  </div>
                </div>

                <div class="edit-row">
                  <label class="edit-label">이메일</label>
                  <div class="edit-field">
                    <div class="edit-readonly-box">${user.email || '-'}</div>
                  </div>
                </div>

                <div class="edit-row">
                  <label class="edit-label">가입일</label>
                  <div class="edit-field">
                    <div class="edit-readonly-box">${formatJoinDate(user.created_at)}</div>
                  </div>
                </div>

                <div id="edit-common-error" class="edit-common-error"></div>

                <div class="edit-button-row">
                  <button type="submit" id="edit-submit-button" class="edit-button is-primary">수정완료</button>
                  <button type="button" id="edit-cancel-button" class="edit-button is-secondary">취소</button>
                </div>
              </form>
            </div>
          </div>
        </div>
      </section>
    `;
  }

  function clearErrors() {
    document.getElementById('edit-name-error').textContent = '';
    document.getElementById('edit-common-error').textContent = '';
  }

  function setFieldError(id, message) {
    const element = document.getElementById(id);
    if (element) element.textContent = message || '';
  }

  function setCommonError(message) {
    const element = document.getElementById('edit-common-error');
    if (element) element.textContent = message || '';
  }

  function validateEditForm(name) {
    if (!name) {
      setFieldError('edit-name-error', '이름을 입력해 주세요.');
      return false;
    }
    return true;
  }

  async function fetchMyInfo(userId) {
    const data = await runtime.getJson(USER_READ_MYPAGE_API, { query: { user_id: userId } });
    return data.user || data.result || data.data || data || {};
  }

  async function requestEdit(payload) {
    return runtime.postJson(USER_WRITE_EDIT_API, payload);
  }

  function bindEditEvents(user) {
    const form = document.getElementById('edit-form');
    const nameInput = document.getElementById('edit-name');
    const submitButton = document.getElementById('edit-submit-button');
    const cancelButton = document.getElementById('edit-cancel-button');

    nameInput.addEventListener('input', function () {
      setFieldError('edit-name-error', '');
      setCommonError('');
    });

    cancelButton.addEventListener('click', function () {
      if (typeof window.appNavigate === 'function') {
        window.appNavigate({ view: 'mypage' });
        return;
      }
      if (typeof window.openMyPage === 'function') {
        window.openMyPage();
      }
    });

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (isEditSubmitting) return;

      clearErrors();

      const userId = runtime.getStoredUserId ? runtime.getStoredUserId() : '';
      const name = nameInput.value.trim();

      if (!userId) {
        setCommonError('로그인 정보가 없습니다.');
        return;
      }

      if (!validateEditForm(name)) {
        return;
      }

      isEditSubmitting = true;
      submitButton.disabled = true;
      submitButton.textContent = '수정중...';

      try {
        await requestEdit({ user_id: userId, name });

        if (runtime.setStoredUserId) {
          runtime.setStoredUserId(userId);
        }
        if (runtime.patchLoginUser) {
          runtime.patchLoginUser({
            user_id: userId,
            name,
            email: user.email,
            created_at: user.created_at
          });
        }

        if (typeof window.refreshSiteHeader === 'function') {
          await window.refreshSiteHeader();
        }

        const mypageUrl = `${window.location.pathname}?view=mypage`;
        window.location.href = mypageUrl;
      } catch (error) {
        console.error('[edit] submit error:', error);
        setCommonError(error.message || '회원정보 수정 중 오류가 발생했습니다.');
      } finally {
        isEditSubmitting = false;
        submitButton.disabled = false;
        submitButton.textContent = '수정완료';
      }
    });
  }

  async function initUserEditPage() {
    try {
      await runtime.ensureStyle(EDIT_CSS_PATH);
    } catch (error) {
      console.error('[edit] css load error:', error);
    }

    if (runtime.resetPrimarySections) {
      runtime.resetPrimarySections();
    }

    const userId = runtime.getStoredUserId ? runtime.getStoredUserId() : '';
    const mainBody = runtime.ensureMainBody ? runtime.ensureMainBody() : document.getElementById('main-body');

    if (!userId) {
      mainBody.innerHTML = `
        <section id="edit-page">
          <div class="edit-wrap">
            <div class="edit-card">
              <div class="edit-title-box">
                <h2 class="edit-title">회원정보 수정</h2>
              </div>
              <div class="edit-content">
                <div class="edit-common-error is-static">로그인 정보가 없습니다.</div>
              </div>
            </div>
          </div>
        </section>
      `;
      return;
    }

    try {
      const user = await fetchMyInfo(userId);
      mainBody.innerHTML = renderEditLayout(user);
      bindEditEvents(user);
      window.scrollTo({ top: 0, behavior: 'auto' });
    } catch (error) {
      console.error('[edit] init error:', error);
      mainBody.innerHTML = `
        <section id="edit-page">
          <div class="edit-wrap">
            <div class="edit-card">
              <div class="edit-title-box">
                <h2 class="edit-title">회원정보 수정</h2>
              </div>
              <div class="edit-content">
                <div class="edit-common-error is-static">회원정보를 불러오지 못했습니다.</div>
              </div>
            </div>
          </div>
        </section>
      `;
    }
  }

  window.openUserEdit = initUserEditPage;
})();

(function () {
  const LOGIN_CSS_PATH = '/css/user/login.css';
  const PWFIND_CSS_PATH = '/css/user/pwfind.css';
  const FIND_PASSWORD_API = '/api/read/auth/find-password';
  const RESET_PASSWORD_API = '/api/write/auth/reset-password';
  const runtime = window.APP_RUNTIME || {};

  function ensureModalCss() {
    return Promise.all([
      runtime.ensureStyle(LOGIN_CSS_PATH),
      runtime.ensureStyle(PWFIND_CSS_PATH)
    ]);
  }

  function closePwFindPage() {
    if (runtime.removeNodeById) {
      runtime.removeNodeById('login-modal-overlay');
    }
    if (runtime.unlockBodyScroll) {
      runtime.unlockBodyScroll();
    }
  }

  function goToLoginPage() {
    closePwFindPage();
    if (typeof window.openLoginPage === 'function') {
      window.openLoginPage();
      return true;
    }
    alert('로그인 화면을 불러오지 못했습니다.');
    return false;
  }

  function setFieldError(modal, field, message) {
    const target = modal.querySelector(`[data-error-for="${field}"]`);
    if (target) target.textContent = message || '';
  }

  function setCommonError(modal, message) {
    const target = modal.querySelector('.pwfind-common-error');
    if (target) target.textContent = message || '';
  }

  function clearErrors(modal) {
    modal.querySelectorAll('.pwfind-field-error').forEach((node) => {
      node.textContent = '';
    });
    setCommonError(modal, '');
  }

  function onlyDigits(value) {
    return String(value || '').replace(/\D/g, '');
  }

  function sanitizeName(value) {
    return String(value || '').replace(/[^가-힣a-zA-Z\s]/g, '');
  }

  function isValidPhone(phone) {
    return /^01[016789]\d{7,8}$/.test(String(phone || '').trim());
  }

  function isValidName(name) {
    return /^[가-힣a-zA-Z\s]{2,20}$/.test(String(name || '').trim());
  }

  function isValidPassword(password) {
    return String(password || '').trim().length >= 4;
  }

  function formatJoinDate(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value).slice(0, 10).replaceAll('-', '.');
    }

    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    return `${y}.${m}.${d}`;
  }

  async function requestFindUser(phone, name) {
    try {
      return await runtime.postJson(FIND_PASSWORD_API, { phone, name });
    } catch (error) {
      return {
        success: false,
        message: error.message || '서버에 연결할 수 없습니다.'
      };
    }
  }

  async function requestResetPassword(phone, name, password) {
    try {
      return await runtime.postJson(RESET_PASSWORD_API, { phone, name, password });
    } catch (error) {
      return {
        success: false,
        message: error.message || '서버에 연결할 수 없습니다.'
      };
    }
  }

  function normalizeFindResult(result, phone, name) {
    const user = result.user || result.data || result.result || null;
    const matchedPhone =
      result.matched_phone === true ||
      result.phone_match === true ||
      result.phoneMatched === true ||
      result.is_phone_match === true ||
      (user && String(user.phone || '') === String(phone));

    const matchedName =
      result.matched_name === true ||
      result.name_match === true ||
      result.nameMatched === true ||
      result.is_name_match === true ||
      (user && String(user.name || '').trim() === String(name).trim());

    const success =
      result.success === true ||
      result.exists === true ||
      result.found === true ||
      result.matched === true ||
      result.both_matched === true ||
      !!user;

    return {
      success,
      matchedPhone,
      matchedName,
      user: success ? (user || { phone, name }) : null,
      message: result.message || ''
    };
  }

  function buildVerifyHtml() {
    const overlay = document.createElement('div');
    overlay.id = 'login-modal-overlay';
    overlay.className = 'login-modal-overlay';

    overlay.innerHTML = `
      <div class="login-modal pwfind-modal" role="dialog" aria-modal="true" aria-labelledby="pwfind-modal-title">
        <button type="button" class="login-modal-close" aria-label="닫기">×</button>

        <div class="login-modal-header pwfind-header">
          <h2 id="pwfind-modal-title" class="login-modal-title">PW 찾기</h2>
          <p class="login-modal-subtitle">핸드폰번호와 이름을 입력해 주세요.</p>
        </div>

        <form class="pwfind-form pwfind-verify-form" novalidate>
          <div class="pwfind-form-group">
            <input type="text" name="phone" class="login-input pwfind-input" placeholder="핸드폰번호" inputmode="numeric" maxlength="11" />
            <div class="pwfind-field-error" data-error-for="phone"></div>
          </div>

          <div class="pwfind-form-group">
            <input type="text" name="name" class="login-input pwfind-input" placeholder="이름" maxlength="20" />
            <div class="pwfind-field-error" data-error-for="name"></div>
          </div>

          <div class="pwfind-common-error"></div>

          <div class="pwfind-button-row">
            <button type="submit" class="login-submit-button pwfind-submit-button">확인</button>
            <button type="button" class="pwfind-cancel-button">취소</button>
          </div>
        </form>
      </div>
    `;

    return overlay;
  }

  function buildResetHtml(user) {
    const overlay = document.createElement('div');
    overlay.id = 'login-modal-overlay';
    overlay.className = 'login-modal-overlay';

    overlay.innerHTML = `
      <div class="login-modal pwfind-modal" role="dialog" aria-modal="true" aria-labelledby="pwreset-modal-title">
        <button type="button" class="login-modal-close" aria-label="닫기">×</button>

        <div class="login-modal-header pwfind-header">
          <h2 id="pwreset-modal-title" class="login-modal-title">PW 찾기</h2>
          <p class="login-modal-subtitle">가입 정보가 확인되었습니다.</p>
        </div>

        <div class="pwfind-found-user-box">
          <div class="pwfind-found-user-date">${formatJoinDate(user.created_at)}</div>
          <div class="pwfind-found-user-text">위 날짜에 가입한 정보가 있습니다.</div>
        </div>

        <form class="pwfind-form pwfind-reset-form" novalidate>
          <input type="hidden" name="phone" value="${user.phone || ''}">
          <input type="hidden" name="name" value="${user.name || ''}">

          <div class="pwfind-form-group">
            <input type="password" name="password" class="login-input pwfind-input" placeholder="비밀번호 변경" />
            <div class="pwfind-field-error" data-error-for="password"></div>
          </div>

          <div class="pwfind-form-group">
            <input type="password" name="password_confirm" class="login-input pwfind-input" placeholder="비밀번호 확인" />
            <div class="pwfind-field-error" data-error-for="password_confirm"></div>
          </div>

          <div class="pwfind-common-error"></div>

          <div class="pwfind-button-row">
            <button type="submit" class="login-submit-button pwfind-submit-button">확인</button>
            <button type="button" class="pwfind-cancel-button">취소</button>
          </div>
        </form>
      </div>
    `;

    return overlay;
  }

  function bindVerifyEvents(overlay) {
    const closeButton = overlay.querySelector('.login-modal-close');
    const cancelButton = overlay.querySelector('.pwfind-cancel-button');
    const form = overlay.querySelector('.pwfind-verify-form');
    const phoneInput = form.querySelector('input[name="phone"]');
    const nameInput = form.querySelector('input[name="name"]');
    const submitButton = form.querySelector('.pwfind-submit-button');
    let isSubmitting = false;
    let isComposingName = false;

    overlay.addEventListener('click', function (event) {
      if (event.target === overlay) {
        closePwFindPage();
      }
    });

    closeButton.addEventListener('click', closePwFindPage);
    cancelButton.addEventListener('click', goToLoginPage);

    phoneInput.addEventListener('input', function () {
      phoneInput.value = onlyDigits(phoneInput.value).slice(0, 11);
      clearErrors(overlay);
    });

    nameInput.addEventListener('compositionstart', function () {
      isComposingName = true;
    });

    nameInput.addEventListener('compositionend', function () {
      isComposingName = false;
      nameInput.value = sanitizeName(nameInput.value);
    });

    nameInput.addEventListener('input', function () {
      if (!isComposingName) {
        nameInput.value = sanitizeName(nameInput.value);
      }
      clearErrors(overlay);
    });

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (isSubmitting) return;

      clearErrors(overlay);

      const phone = phoneInput.value.trim();
      const name = nameInput.value.trim();
      let hasError = false;

      if (!isValidPhone(phone)) {
        setFieldError(overlay, 'phone', '핸드폰번호를 확인하세요.');
        hasError = true;
      }
      if (!isValidName(name)) {
        setFieldError(overlay, 'name', '이름을 확인하세요.');
        hasError = true;
      }
      if (hasError) return;

      isSubmitting = true;
      submitButton.disabled = true;
      submitButton.textContent = '확인 중...';

      try {
        const result = normalizeFindResult(await requestFindUser(phone, name), phone, name);
        if (!result.success || !result.user) {
          if (!result.matchedPhone) {
            setFieldError(overlay, 'phone', '가입된 핸드폰번호가 없습니다.');
          }
          if (!result.matchedName) {
            setFieldError(overlay, 'name', '가입된 이름이 없습니다.');
          }
          if (result.matchedPhone && result.matchedName) {
            setCommonError(overlay, result.message || '가입 정보를 찾을 수 없습니다.');
          }
          return;
        }

        openPwResetPage(result.user);
      } finally {
        isSubmitting = false;
        submitButton.disabled = false;
        submitButton.textContent = '확인';
      }
    });
  }

  function bindResetEvents(overlay) {
    const closeButton = overlay.querySelector('.login-modal-close');
    const cancelButton = overlay.querySelector('.pwfind-cancel-button');
    const form = overlay.querySelector('.pwfind-reset-form');
    const passwordInput = form.querySelector('input[name="password"]');
    const passwordConfirmInput = form.querySelector('input[name="password_confirm"]');
    const submitButton = form.querySelector('.pwfind-submit-button');
    let isSubmitting = false;

    overlay.addEventListener('click', function (event) {
      if (event.target === overlay) {
        closePwFindPage();
      }
    });

    closeButton.addEventListener('click', closePwFindPage);
    cancelButton.addEventListener('click', goToLoginPage);

    [passwordInput, passwordConfirmInput].forEach((input) => {
      input.addEventListener('input', function () {
        clearErrors(overlay);
      });
    });

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (isSubmitting) return;

      clearErrors(overlay);

      const phone = form.querySelector('input[name="phone"]').value.trim();
      const name = form.querySelector('input[name="name"]').value.trim();
      const password = passwordInput.value;
      const passwordConfirm = passwordConfirmInput.value;
      let hasError = false;

      if (!isValidPassword(password)) {
        setFieldError(overlay, 'password', '비밀번호는 4자 이상 입력하세요.');
        hasError = true;
      }
      if (password !== passwordConfirm) {
        setFieldError(overlay, 'password_confirm', '비밀번호가 일치하지 않습니다.');
        hasError = true;
      }
      if (hasError) return;

      isSubmitting = true;
      submitButton.disabled = true;
      submitButton.textContent = '변경 중...';

      try {
        const result = await requestResetPassword(phone, name, password);
        if (result.success === false) {
          setCommonError(overlay, result.message || '비밀번호 변경에 실패했습니다.');
          return;
        }

        alert('비밀번호가 변경되었습니다.');
        goToLoginPage();
      } finally {
        isSubmitting = false;
        submitButton.disabled = false;
        submitButton.textContent = '확인';
      }
    });
  }

  function openPwResetPage(user) {
    closePwFindPage();
    const overlay = buildResetHtml(user);
    runtime.lockBodyScroll();
    document.body.appendChild(overlay);
    bindResetEvents(overlay);
  }

  async function openPwFindPage() {
    if (runtime.clearTransientUi) {
      runtime.clearTransientUi();
    }

    try {
      await ensureModalCss();
    } catch (error) {
      console.error('[pwfind] css load error:', error);
    }

    const overlay = buildVerifyHtml();
    runtime.lockBodyScroll();
    document.body.appendChild(overlay);
    bindVerifyEvents(overlay);

    const firstInput = overlay.querySelector('input[name="phone"]');
    if (firstInput) {
      requestAnimationFrame(() => firstInput.focus());
    }
  }

  window.openPwFindPage = openPwFindPage;
  window.closePwFindPage = closePwFindPage;
})();

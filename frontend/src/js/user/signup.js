(function () {
  const LOGIN_CSS_PATH = '/css/user/login.css';
  const SIGNUP_CSS_PATH = '/css/user/signup.css';
  const runtime = window.APP_RUNTIME || {};

  function ensureModalCss() {
    return Promise.all([
      runtime.ensureStyle(LOGIN_CSS_PATH),
      runtime.ensureStyle(SIGNUP_CSS_PATH)
    ]);
  }

  function closeSignupPage() {
    if (runtime.removeNodeById) {
      runtime.removeNodeById('login-modal-overlay');
    }
    if (runtime.unlockBodyScroll) {
      runtime.unlockBodyScroll();
    }
  }

  function goToLoginPage() {
    closeSignupPage();
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
    const target = modal.querySelector('.signup-common-error');
    if (target) target.textContent = message || '';
  }

  function clearErrors(modal) {
    modal.querySelectorAll('.signup-field-error').forEach((node) => {
      node.textContent = '';
    });
    setCommonError(modal, '');
  }

  function sanitizeName(value) {
    return String(value || '').replace(/[^가-힣a-zA-Z\s]/g, '');
  }

  function isValidEmail(email) {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(email || '').trim());
  }

  function isValidName(name) {
    return /^[가-힣a-zA-Z\s]{2,20}$/.test(String(name || '').trim());
  }

  function isValidPassword(password) {
    return String(password || '').trim().length >= 8;
  }

  /**
   * Map Cognito error codes to user-friendly messages.
   */
  function cognitoSignupErrorMessage(error) {
    const code = error.code || '';
    if (code.indexOf('UsernameExistsException') !== -1) {
      return '이미 등록된 이메일입니다.';
    }
    if (code.indexOf('InvalidPasswordException') !== -1) {
      return '비밀번호 형식이 올바르지 않습니다. (8자 이상, 대소문자/숫자/특수문자 포함)';
    }
    if (code.indexOf('InvalidParameterException') !== -1) {
      return '입력 정보를 확인해 주세요.';
    }
    return error.message || '회원가입에 실패했습니다.';
  }

  function buildSignupHtml() {
    const overlay = document.createElement('div');
    overlay.id = 'login-modal-overlay';
    overlay.className = 'login-modal-overlay';

    overlay.innerHTML = `
      <div class="login-modal signup-modal" role="dialog" aria-modal="true" aria-labelledby="signup-modal-title">
        <button type="button" class="login-modal-close" aria-label="닫기">&times;</button>

        <div class="login-modal-header signup-header">
          <h2 id="signup-modal-title" class="login-modal-title">회원가입</h2>
          <p class="login-modal-subtitle">회원 정보를 입력해 주세요.</p>
        </div>

        <form class="signup-form" novalidate>
          <div class="signup-form-group">
            <input type="email" name="email" class="login-input signup-input" placeholder="이메일" autocomplete="email" />
            <div class="signup-field-error" data-error-for="email"></div>
          </div>

          <div class="signup-form-group">
            <input type="password" name="password" class="login-input signup-input" placeholder="비밀번호 (8자 이상)" autocomplete="new-password" />
            <div class="signup-field-error" data-error-for="password"></div>
          </div>

          <div class="signup-form-group">
            <input type="text" name="name" class="login-input signup-input" placeholder="이름" maxlength="20" />
            <div class="signup-field-error" data-error-for="name"></div>
          </div>

          <div class="signup-common-error"></div>

          <div class="signup-button-row">
            <button type="submit" class="login-submit-button signup-submit-button">가입</button>
            <button type="button" class="signup-cancel-button">취소</button>
          </div>
        </form>
      </div>
    `;

    return overlay;
  }

  function bindEvents(overlay) {
    const closeButton = overlay.querySelector('.login-modal-close');
    const cancelButton = overlay.querySelector('.signup-cancel-button');
    const form = overlay.querySelector('.signup-form');
    const emailInput = form.querySelector('input[name="email"]');
    const passwordInput = form.querySelector('input[name="password"]');
    const nameInput = form.querySelector('input[name="name"]');
    const submitButton = form.querySelector('.signup-submit-button');
    let isComposingName = false;
    let isSubmitting = false;

    overlay.addEventListener('click', function (event) {
      if (event.target === overlay) {
        closeSignupPage();
      }
    });

    closeButton.addEventListener('click', closeSignupPage);
    cancelButton.addEventListener('click', goToLoginPage);

    emailInput.addEventListener('input', function () {
      clearErrors(overlay);
    });

    passwordInput.addEventListener('input', function () {
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

      const email = emailInput.value.trim();
      const password = passwordInput.value;
      const name = nameInput.value.trim();

      let hasError = false;
      if (!isValidEmail(email)) {
        setFieldError(overlay, 'email', '올바른 이메일을 입력해 주세요.');
        hasError = true;
      }
      if (!isValidPassword(password)) {
        setFieldError(overlay, 'password', '비밀번호는 8자 이상 입력하세요.');
        hasError = true;
      }
      if (!isValidName(name)) {
        setFieldError(overlay, 'name', '이름을 확인하세요.');
        hasError = true;
      }
      if (hasError) return;

      isSubmitting = true;
      submitButton.disabled = true;
      submitButton.textContent = '가입 중...';

      try {
        // ── Cognito signup (Lambda auto-confirm, no verification step) ──
        await window.CognitoAuth.signUp(email, password, name);

        // Auto-login after signup
        try {
          const loginResult = await window.CognitoAuth.login(email, password);
          const authResult = loginResult.AuthenticationResult;
          if (authResult && authResult.IdToken) {
            const userInfo = window.CognitoAuth.getCurrentUser();
            const userData = {
              user_id: userInfo.sub,
              name: userInfo.name || name,
              email: userInfo.email || email
            };
            runtime.setLoginUser(userData);
            runtime.setStoredUserId(userInfo.sub);

            // Cognito sub(UUID) → DB int user_id 치환 + DB name/email 동기화
            // (login.js 와 동일 이유: id_token refresh 후 name claim 이 빠져도 localStorage 유지)
            try {
              const me = await runtime.getJson('/api/read/auth/me');
              if (me && me.user && me.user.user_id) {
                const patch = { user_id: me.user.user_id };
                if (me.user.name)  patch.name  = me.user.name;
                if (me.user.email) patch.email = me.user.email;
                runtime.patchLoginUser(patch);
              }
            } catch (meErr) {
              console.warn('[signup] resolve DB user_id failed:', meErr);
            }
          }
        } catch (loginErr) {
          console.warn('[signup] auto-login after signup failed:', loginErr);
        }

        alert('회원가입이 완료되었습니다.');
        closeSignupPage();

        if (typeof window.refreshSiteHeader === 'function') {
          await window.refreshSiteHeader();
        }
      } catch (error) {
        console.error('[signup] error:', error);
        setCommonError(overlay, cognitoSignupErrorMessage(error));
      } finally {
        isSubmitting = false;
        submitButton.disabled = false;
        submitButton.textContent = '가입';
      }
    });
  }

  async function openSignupPage() {
    if (runtime.clearTransientUi) {
      runtime.clearTransientUi();
    }

    try {
      await ensureModalCss();
    } catch (error) {
      console.error('[signup] css load error:', error);
    }

    const overlay = buildSignupHtml();
    runtime.lockBodyScroll();
    document.body.appendChild(overlay);
    bindEvents(overlay);

    const firstInput = overlay.querySelector('input[name="email"]');
    if (firstInput) {
      requestAnimationFrame(() => firstInput.focus());
    }
  }

  window.openSignupPage = openSignupPage;
  window.closeSignupPage = closeSignupPage;
})();

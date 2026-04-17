(function () {
  const HEADER_CSS_PATH = '/css/main/head.css';
  const LOGO_PATH = '/images/logo.png';
  const runtime = window.APP_RUNTIME || {};

  function getLoginUser() {
    return typeof runtime.getLoginUser === 'function' ? runtime.getLoginUser() : null;
  }

  async function ensureHeadCss() {
    if (typeof runtime.ensureStyle === 'function') {
      try {
        await runtime.ensureStyle(HEADER_CSS_PATH);
      } catch (error) {
        console.error(error);
      }
      return;
    }
  }

  function navigateHome(event) {
    if (event) event.preventDefault();

    if (typeof window.appNavigate === 'function') {
      window.appNavigate({}, { replace: false });
      return;
    }

    window.location.href = '/';
  }

  function openLogin(event) {
    if (event) event.preventDefault();
    if (typeof window.openLoginPage === 'function') {
      window.openLoginPage();
    }
  }

  function openMyPage(event) {
    if (event) event.preventDefault();

    if (typeof window.appNavigate === 'function') {
      window.appNavigate({ view: 'mypage' });
      return;
    }

    if (typeof window.openMyPage === 'function') {
      window.openMyPage();
    }
  }

  function openMoviePage(event) {
    if (event) event.preventDefault();

    if (typeof window.appNavigate === 'function') {
      window.appNavigate({ page: 1 });
      return;
    }

    window.location.href = '/?page=1';
  }

  function openConcertPage(event) {
    if (event) event.preventDefault();

    if (typeof window.appNavigate === 'function') {
      window.appNavigate({ c_page: 1 });
      return;
    }

    window.location.href = '/?c_page=1';
  }

  function openBookingPage(event) {
    if (event) event.preventDefault();

    if (typeof window.appNavigate === 'function') {
      window.appNavigate({ view: 'booking' });
      return;
    }

    window.location.href = '/?view=booking';
  }

  async function logout(event) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }

    // Clear Cognito tokens
    if (window.CognitoAuth && typeof window.CognitoAuth.logout === 'function') {
      window.CognitoAuth.logout();
    }

    if (typeof runtime.clearLoginUser === 'function') {
      runtime.clearLoginUser();
    }

    if (typeof window.closeLoginPage === 'function') {
      window.closeLoginPage();
    }

    await mountHeader();

    if (typeof window.appNavigate === 'function') {
      await window.appNavigate({}, { replace: true });
    } else {
      window.location.href = '/';
      return;
    }

    alert('로그아웃 되었습니다.');
  }

  function createAuthArea(user) {
    const wrapper = document.createElement('div');
    wrapper.className = 'site-header-auth';

    if (user) {
      const topRow = document.createElement('div');
      topRow.className = 'site-header-auth-top-row';

      const nameButton = document.createElement('button');
      nameButton.type = 'button';
      nameButton.className = 'site-header-auth-name';
      nameButton.textContent = `${user.name || user.user_name || '회원'}님`;
      nameButton.addEventListener('click', openMyPage);

      const divider = document.createElement('span');
      divider.className = 'site-header-auth-divider';
      divider.textContent = '|';

      const logoutButton = document.createElement('button');
      logoutButton.type = 'button';
      logoutButton.className = 'site-header-logout-button';
      logoutButton.textContent = '로그아웃';
      logoutButton.addEventListener('click', logout);

      topRow.appendChild(nameButton);
      topRow.appendChild(divider);
      topRow.appendChild(logoutButton);
      wrapper.appendChild(topRow);
      return wrapper;
    }

    const loginButton = document.createElement('button');
    loginButton.type = 'button';
    loginButton.className = 'site-header-login-button';
    loginButton.textContent = '로그인';
    loginButton.addEventListener('click', openLogin);

    wrapper.appendChild(loginButton);
    return wrapper;
  }

  function createMenu() {
    const nav = document.createElement('nav');
    nav.className = 'site-header-nav';
    nav.setAttribute('aria-label', '메인 메뉴');

    const menuItems = [
      { label: '예매', href: '#', onClick: openBookingPage },
      { label: '영화', href: '#', onClick: openMoviePage },
      { label: '콘서트/뮤지컬', href: '#', onClick: openConcertPage },
    ];

    menuItems.forEach((item) => {
      const link = document.createElement('a');
      link.href = item.href;
      link.className = 'site-header-menu-item';
      link.textContent = item.label;
      if (typeof item.onClick === 'function') {
        link.addEventListener('click', item.onClick);
      }
      nav.appendChild(link);
    });

    return nav;
  }

  function createHeader() {
    const user = getLoginUser();
    const header = document.createElement('header');
    header.className = 'site-header';

    const top = document.createElement('div');
    top.className = 'site-header-top';

    const left = document.createElement('div');
    left.className = 'site-header-top-side';

    const center = document.createElement('div');
    center.className = 'site-header-logo-wrap';

    const logoLink = document.createElement('a');
    logoLink.href = '/';
    logoLink.className = 'site-header-logo-link';
    logoLink.setAttribute('aria-label', '홈으로 이동');
    logoLink.addEventListener('click', navigateHome);

    const logoImg = document.createElement('img');
    logoImg.src = LOGO_PATH;
    logoImg.alt = '로고';
    logoImg.className = 'site-header-logo';

    logoLink.appendChild(logoImg);
    center.appendChild(logoLink);

    const right = document.createElement('div');
    right.className = 'site-header-top-side site-header-top-side-right';
    right.appendChild(createAuthArea(user));

    const divider = document.createElement('div');
    divider.className = 'site-header-divider';

    header.appendChild(top);
    top.appendChild(left);
    top.appendChild(center);
    top.appendChild(right);
    header.appendChild(divider);
    header.appendChild(createMenu());

    return header;
  }

  async function mountHeader() {
    await ensureHeadCss();

    let siteHeader = document.getElementById('site-header');
    if (!siteHeader) {
      siteHeader = document.createElement('div');
      siteHeader.id = 'site-header';
      document.body.prepend(siteHeader);
    }
    siteHeader.innerHTML = '';
    siteHeader.style.visibility = 'visible';
    siteHeader.appendChild(createHeader());

    if (typeof window.appPrefetchScripts === 'function') {
      window.appPrefetchScripts([
        '/js/user/mypage.js',
        '/js/theaters/theaters_main.js',
        '/js/concert/concert_main.js'
      ]);
    }
  }

  window.renderSiteHeader = mountHeader;
  window.refreshSiteHeader = mountHeader;
  window.logoutUser = logout;
  window.openMoviePage = openMoviePage;
  window.openBookingPage = openBookingPage;
  window.openConcertPage = openConcertPage;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountHeader);
  } else {
    mountHeader();
  }
})();

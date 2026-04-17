(function () {
  const BODY3_CSS_PATH = '/css/main/body3.css';

  function ensureBody3Css() {
    if (window.APP_RUNTIME && typeof window.APP_RUNTIME.ensureStyle === 'function') {
      return window.APP_RUNTIME.ensureStyle(BODY3_CSS_PATH);
    }

    const exists = document.querySelector(`link[href="${BODY3_CSS_PATH}"]`);
    if (exists) return Promise.resolve(exists);

    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = BODY3_CSS_PATH;
    document.head.appendChild(link);
    return Promise.resolve(link);
  }

  function ensureMountPoint() {
    let mount = document.getElementById('main-body3');
    if (mount) return mount;

    mount = document.createElement('section');
    mount.id = 'main-body3';

    const body2 = document.getElementById('main-body2');
    if (body2 && body2.parentNode) {
      if (body2.nextSibling) {
        body2.parentNode.insertBefore(mount, body2.nextSibling);
      } else {
        body2.parentNode.appendChild(mount);
      }
    } else {
      document.body.appendChild(mount);
    }

    return mount;
  }

  function createFooter() {
    const wrapper = document.createElement('div');
    wrapper.className = 'main-body3-wrap';

    wrapper.innerHTML = `
      <div class="main-body3-top">
        <div class="main-body3-inner">
          <a href="tel:1544-0714" class="main-body3-call-button">
            <span class="main-body3-call-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" class="main-body3-call-svg" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path
                  d="M21 16.42V19.5C21 20.05 20.55 20.5 20 20.5C10.34 20.5 2.5 12.66 2.5 3C2.5 2.45 2.95 2 3.5 2H6.59C7.07 2 7.48 2.34 7.57 2.81L8.07 5.5C8.15 5.91 8.01 6.33 7.7 6.62L5.84 8.33C7.03 10.76 8.99 12.72 11.42 13.91L13.13 12.05C13.42 11.74 13.84 11.6 14.25 11.68L16.94 12.18C17.41 12.27 17.75 12.68 17.75 13.16V16.42H21Z"
                  fill="currentColor"
                />
              </svg>
            </span>
            <span>대표번호 : 1544-0714</span>
          </a>
        </div>
      </div>

      <div class="main-body3-bottom">
        <div class="main-body3-inner">
          <h3 class="main-body3-title">AWS cloud 803호 2조 team Project</h3>

          <p class="main-body3-info">
            사업자번호: 101-86-50485
            <span class="main-body3-divider">|</span>
            통신판매업신고번호: 제2009-서울종로-1141호
            <span class="main-body3-divider">|</span>
            대표이사: 강재민
          </p>

          <p class="main-body3-info">
            서울특별시 종로구 종로12길 15, 2층/5층/8~10층(관철동 13-13)
            <span class="main-body3-divider">|</span>
            대표전화 : 1544-0714
            <span class="main-body3-divider">|</span>
            강사 : 오창석 강사님
          </p>

          <div class="main-body3-links">
            <a href="#" class="main-body3-link">Terms of Use</a>
            <span class="main-body3-divider">|</span>
            <a href="#" class="main-body3-link">Privacy</a>
          </div>

          <p class="main-body3-copy">
            Hosting by Imweb | Copyright©2022 솔데스크학원 ALL RIGHTS RESERVED
          </p>
        </div>
      </div>
    `;

    return wrapper;
  }

  async function mountBody3() {
    await ensureBody3Css();

    const mount = ensureMountPoint();
    mount.innerHTML = '';
    mount.appendChild(createFooter());
  }

  window.renderMainBody3 = mountBody3;
  window.renderSiteFooter = mountBody3;

})();
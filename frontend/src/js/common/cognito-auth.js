/**
 * Cognito Auth Helper — lightweight, no AWS SDK dependency.
 * Uses fetch() against the Cognito public API endpoint.
 *
 * Token storage: localStorage keys prefixed with cognito_
 * Config: window.COGNITO_CONFIG or defaults (injected at deploy time)
 */
(function () {
  'use strict';

  /* ── configuration ─────────────────────────────────────────── */
  var cognitoConfig = (window.COGNITO_CONFIG && typeof window.COGNITO_CONFIG === 'object')
    ? window.COGNITO_CONFIG
    : {};

  var REGION     = cognitoConfig.REGION       || 'ap-northeast-2';
  var CLIENT_ID  = cognitoConfig.CLIENT_ID    || '';
  var USER_POOL_ID = cognitoConfig.USER_POOL_ID || '';

  var COGNITO_ENDPOINT = 'https://cognito-idp.' + REGION + '.amazonaws.com/';

  /* ── localStorage keys ─────────────────────────────────────── */
  var KEY_ID_TOKEN      = 'cognito_id_token';
  var KEY_ACCESS_TOKEN  = 'cognito_access_token';
  var KEY_REFRESH_TOKEN = 'cognito_refresh_token';

  /* ── low-level helpers ─────────────────────────────────────── */

  function cognitoFetch(target, payload) {
    return fetch(COGNITO_ENDPOINT, {
      method: 'POST',
      headers: {
        'Content-Type':        'application/x-amz-json-1.1',
        'X-Amz-Target':        target
      },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) {
          var err = new Error(data.message || data.Message || 'Cognito error ' + res.status);
          err.code = data.__type || '';
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  function storeTokens(authResult) {
    if (!authResult) return;
    if (authResult.IdToken)      localStorage.setItem(KEY_ID_TOKEN,      authResult.IdToken);
    if (authResult.AccessToken)  localStorage.setItem(KEY_ACCESS_TOKEN,  authResult.AccessToken);
    if (authResult.RefreshToken) localStorage.setItem(KEY_REFRESH_TOKEN, authResult.RefreshToken);

    // Also set the global bearer token so the existing requestJson picks it up
    if (authResult.AccessToken) {
      window.__TICKETING_AUTH_BEARER_TOKEN__ = authResult.AccessToken;
    }
  }

  function clearTokens() {
    localStorage.removeItem(KEY_ID_TOKEN);
    localStorage.removeItem(KEY_ACCESS_TOKEN);
    localStorage.removeItem(KEY_REFRESH_TOKEN);
    window.__TICKETING_AUTH_BEARER_TOKEN__ = '';
  }

  function decodeJwtPayload(token) {
    if (!token) return null;
    try {
      var parts = token.split('.');
      if (parts.length < 2) return null;
      var payload = parts[1].replace(/-/g, '+').replace(/_/g, '/');
      var binary = atob(payload);
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      var decoded = new TextDecoder('utf-8').decode(bytes);
      return JSON.parse(decoded);
    } catch (e) {
      return null;
    }
  }

  function isTokenExpired(token) {
    var payload = decodeJwtPayload(token);
    if (!payload || !payload.exp) return true;
    // 30-second buffer
    return (payload.exp * 1000) < (Date.now() + 30000);
  }

  /* ── public API ────────────────────────────────────────────── */

  function cognitoSignUp(email, password, name) {
    return cognitoFetch('AWSCognitoIdentityProviderService.SignUp', {
      ClientId: CLIENT_ID,
      Username: email,
      Password: password,
      UserAttributes: [
        { Name: 'email', Value: email },
        { Name: 'name',  Value: name }
      ]
    });
  }

  function cognitoLogin(email, password) {
    return cognitoFetch('AWSCognitoIdentityProviderService.InitiateAuth', {
      AuthFlow:       'USER_PASSWORD_AUTH',
      ClientId:       CLIENT_ID,
      AuthParameters: {
        USERNAME: email,
        PASSWORD: password
      }
    }).then(function (data) {
      if (data.AuthenticationResult) {
        storeTokens(data.AuthenticationResult);
      }
      return data;
    });
  }

  function cognitoRefreshToken() {
    var refreshToken = localStorage.getItem(KEY_REFRESH_TOKEN);
    if (!refreshToken) {
      return Promise.reject(new Error('No refresh token'));
    }

    return cognitoFetch('AWSCognitoIdentityProviderService.InitiateAuth', {
      AuthFlow:       'REFRESH_TOKEN_AUTH',
      ClientId:       CLIENT_ID,
      AuthParameters: {
        REFRESH_TOKEN: refreshToken
      }
    }).then(function (data) {
      if (data.AuthenticationResult) {
        storeTokens(data.AuthenticationResult);
      }
      return data;
    });
  }

  function cognitoLogout() {
    clearTokens();
    // Also clear legacy login user data
    var runtime = window.APP_RUNTIME || {};
    if (typeof runtime.clearLoginUser === 'function') {
      runtime.clearLoginUser();
    }
  }

  function getAccessToken() {
    return localStorage.getItem(KEY_ACCESS_TOKEN) || '';
  }

  function getIdToken() {
    return localStorage.getItem(KEY_ID_TOKEN) || '';
  }

  function isLoggedIn() {
    var token = getAccessToken();
    if (!token) return false;
    return !isTokenExpired(token);
  }

  function getCurrentUser() {
    var token = getIdToken();
    var payload = decodeJwtPayload(token);
    if (!payload) return null;
    return {
      sub:   payload.sub   || '',
      email: payload.email || '',
      name:  payload.name  || '',
      phone: payload.phone_number || ''
    };
  }

  /**
   * Ensures the bearer token global is set on page load
   * if we already have a valid access token in storage.
   */
  function restoreSession() {
    var token = getAccessToken();
    if (token && !isTokenExpired(token)) {
      window.__TICKETING_AUTH_BEARER_TOKEN__ = token;
      return true;
    }
    // Try refresh
    var refreshToken = localStorage.getItem(KEY_REFRESH_TOKEN);
    if (refreshToken) {
      cognitoRefreshToken().catch(function () {
        clearTokens();
      });
    }
    return false;
  }

  /* ── exports ───────────────────────────────────────────────── */
  window.CognitoAuth = {
    signUp:         cognitoSignUp,
    login:          cognitoLogin,
    refreshToken:   cognitoRefreshToken,
    logout:         cognitoLogout,
    getAccessToken: getAccessToken,
    getIdToken:     getIdToken,
    isLoggedIn:     isLoggedIn,
    getCurrentUser: getCurrentUser,
    restoreSession: restoreSession,
    clearTokens:    clearTokens,
    decodeJwt:      decodeJwtPayload
  };

  // Restore session on load
  restoreSession();
})();

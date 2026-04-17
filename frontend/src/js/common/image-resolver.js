(function () {
  function getImageConfig() {
    const appConfig = window.APP_CONFIG || {};
    return appConfig.image || {
      baseUrl: '/images/posters/',
      rewriteRootRelativePaths: false,
      localPrefixMode: 'filename',
      localPrefixes: [],
      manifest: {},
      keepAbsoluteUrls: true,
      fallbackImageUrl: '/images/posters/no-image.png'
    };
  }

  function normalizeSlash(value) {
    return String(value || '').replaceAll('\\', '/');
  }

  function trimLeadingSlash(value) {
    return String(value || '').replace(/^\/+/, '');
  }

  function trimTrailingSlash(value) {
    return String(value || '').replace(/\/+$/, '');
  }

  function isAbsoluteUrl(value) {
    return /^https?:\/\//i.test(String(value || '').trim());
  }

  function isDataLikeUrl(value) {
    return /^(data:|blob:)/i.test(String(value || '').trim());
  }

  function extractFileName(value) {
    const normalized = normalizeSlash(value).trim();
    if (!normalized) return '';
    const parts = normalized.split('/');
    return parts[parts.length - 1] || '';
  }

  function hasFileExtension(fileName) {
    const value = String(fileName || '').trim();
    if (!value) return false;
    if (value.endsWith('.')) return false;
    const lastDot = value.lastIndexOf('.');
    if (lastDot <= 0) return false;
    return lastDot < value.length - 1;
  }

  function ensureDefaultExtension(pathLike, defaultExt) {
    const raw = String(pathLike || '').trim();
    if (!raw) return raw;
    const fileName = extractFileName(raw);
    if (!fileName || hasFileExtension(fileName)) return raw;
    const ext = String(defaultExt || '').trim().replace(/^\./, '');
    if (!ext) return raw;
    return `${raw}.${ext}`;
  }

  function joinBaseUrl(baseUrl, path) {
    const base = String(baseUrl || '').trim();
    const target = trimLeadingSlash(path);

    if (!base) {
      return target ? `/${target}` : '';
    }

    if (isAbsoluteUrl(base)) {
      return target ? `${trimTrailingSlash(base)}/${target}` : trimTrailingSlash(base);
    }

    return target ? `/${trimTrailingSlash(base).replace(/^\/+/, '')}/${target}`.replace(/\/{2,}/g, '/') : base;
  }

  function getManifestMatch(rawValue, config) {
    const manifest = config.manifest || {};
    if (manifest[rawValue]) {
      return manifest[rawValue];
    }

    const fileName = extractFileName(rawValue);
    if (fileName && manifest[fileName]) {
      return manifest[fileName];
    }

    return '';
  }

  function normalizeManifestValue(value, config) {
    const normalized = String(value || '').trim();
    if (!normalized) return '';

    if (isAbsoluteUrl(normalized) || isDataLikeUrl(normalized) || normalized.startsWith('/')) {
      return normalized;
    }

    return joinBaseUrl(config.baseUrl, normalized);
  }

  function stripKnownPrefix(rawValue, config) {
    const normalized = normalizeSlash(rawValue).trim();
    const prefixes = Array.isArray(config.localPrefixes) ? config.localPrefixes : [];

    for (const prefix of prefixes) {
      const normalizedPrefix = normalizeSlash(prefix).trim();
      if (!normalizedPrefix) continue;
      if (normalized.startsWith(normalizedPrefix)) {
        return normalized.slice(normalizedPrefix.length);
      }
    }

    return '';
  }

  function resolveImageUrl(rawValue) {
    if (rawValue === null || rawValue === undefined) return '';

    const config = getImageConfig();
    const value = normalizeSlash(String(rawValue).trim());

    if (!value) return '';

    // allow DB values like "no-image" (no extension) to work with static assets
    const normalizedBare = value.replace(/^\/+/, '').trim().toLowerCase();
    if (normalizedBare === 'no-image' || normalizedBare === 'noimage') {
      return String(config.fallbackImageUrl || '/images/posters/no-image.png').trim();
    }

    const manifestMatch = getManifestMatch(value, config);
    if (manifestMatch) {
      return normalizeManifestValue(manifestMatch, config);
    }

    if (config.keepAbsoluteUrls && isAbsoluteUrl(value)) {
      return value;
    }

    if (isDataLikeUrl(value)) {
      return value;
    }

    if (value.startsWith('/')) {
      if (config.rewriteRootRelativePaths) {
        return joinBaseUrl(config.baseUrl, trimLeadingSlash(value));
      }
      return value;
    }

    const stripped = stripKnownPrefix(value, config);
    if (stripped) {
      if (config.localPrefixMode === 'relative') {
        return joinBaseUrl(config.baseUrl, ensureDefaultExtension(stripped, 'png'));
      }

      const fileName = extractFileName(stripped);
      return joinBaseUrl(config.baseUrl, ensureDefaultExtension(fileName || stripped, 'png'));
    }

    return joinBaseUrl(config.baseUrl, ensureDefaultExtension(value, 'png'));
  }

  function getFallbackImageUrl() {
    const config = getImageConfig();
    return String(config.fallbackImageUrl || '/images/posters/no-image.png').trim();
  }

  window.getImageConfig = getImageConfig;
  window.resolveImageUrl = resolveImageUrl;
  window.getFallbackImageUrl = getFallbackImageUrl;
})();
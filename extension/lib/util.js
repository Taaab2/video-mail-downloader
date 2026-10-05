/* הגדרות (עם טוקן מוטמע) ועזרי ממשק קטנים. */
(function (root) {
  'use strict';

  const FALLBACK = {
    TOKEN: '',
    REPO: '',
    WORKFLOW: 'downloader.yml',
    COOKIES_SECRET: 'YT_COOKIES_B64',
    DEFAULT_QUALITY: 'best',
    DEFAULT_TARGET: 'github',
    INCLUDE_GOOGLE_COOKIES: true,
  };

  const KEYS = Object.keys(FALLBACK).concat(['LAST_RUN_ID', 'COOKIE_INFO']);

  const hasExtensionStorage =
    typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local;

  function storageGet(keys) {
    if (hasExtensionStorage) {
      return new Promise((resolve) => chrome.storage.local.get(keys, (v) => resolve(v || {})));
    }
    const out = {};
    for (const key of keys) {
      const raw = localStorage.getItem('dl.' + key);
      if (raw !== null) {
        try {
          out[key] = JSON.parse(raw);
        } catch (err) {
          out[key] = raw;
        }
      }
    }
    return Promise.resolve(out);
  }

  function storageSet(values) {
    if (hasExtensionStorage) {
      return new Promise((resolve) => chrome.storage.local.set(values, resolve));
    }
    for (const [key, value] of Object.entries(values)) {
      localStorage.setItem('dl.' + key, JSON.stringify(value));
    }
    return Promise.resolve();
  }

  /** הטוקן/המאגר מגיעים מוטמעים בקובץ config.js, ואפשר לעקוף אותם מההגדרות. */
  async function loadSettings() {
    const embedded = Object.assign({}, FALLBACK, root.EXT_CONFIG || {});
    const stored = await storageGet(KEYS);
    const merged = Object.assign({}, embedded, stored);
    // ערך ריק בהגדרות לא ידרוס ערך מוטמע
    for (const key of Object.keys(FALLBACK)) {
      if (!merged[key] && embedded[key]) merged[key] = embedded[key];
    }
    return merged;
  }

  function saveSettings(patch) {
    return storageSet(patch);
  }

  function client(settings) {
    return new root.GitHub(settings.TOKEN, settings.REPO);
  }

  // ------------------------------------------------------------------ //
  const $ = (sel, scope) => (scope || document).querySelector(sel);
  const $$ = (sel, scope) => Array.from((scope || document).querySelectorAll(sel));

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function humanSize(bytes) {
    if (bytes == null) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let n = Number(bytes);
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
  }

  function fmtDate(value) {
    if (!value) return '';
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    return d.toLocaleString('he-IL', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  let toastTimer = null;
  function toast(message, isError) {
    const el = document.getElementById('toast');
    if (!el) {
      if (isError) console.warn(message);
      return;
    }
    el.textContent = message;
    el.className = 'toast show' + (isError ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.className = 'toast';
    }, 4200);
  }

  function copyText(text, label) {
    const done = () => toast((label || 'הקישור') + ' הועתק');
    const failed = () => toast('לא הצלחתי להעתיק – העתיקו ידנית', true);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, failed);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
        done();
      } catch (err) {
        failed();
      }
      ta.remove();
    }
  }

  async function activeTabUrl() {
    if (typeof chrome === 'undefined' || !chrome.tabs) return '';
    const tabs = await new Promise((resolve) => chrome.tabs.query({ active: true, currentWindow: true }, resolve));
    return (tabs && tabs[0] && tabs[0].url) || '';
  }

  root.util = {
    loadSettings,
    saveSettings,
    client,
    $,
    $$,
    esc,
    humanSize,
    fmtDate,
    toast,
    copyText,
    activeTabUrl,
    hasExtensionStorage,
  };
})(typeof self !== 'undefined' ? self : this);

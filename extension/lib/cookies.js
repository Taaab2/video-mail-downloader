/* שליפת עוגיות יוטיוב מהדפדפן והמרתן לקובץ cookies.txt של yt-dlp.
 *
 * הפורמט הנשלח ל-GitHub הוא base64 של קובץ cookies.txt (אם הדפדפן תומך –
 * דחוס ב-gzip). ה-Workflow מזהה לבד אם זה gzip לפי החתימה 1f 8b.
 */
(function (root) {
  'use strict';

  const YT_DOMAINS = ['https://www.youtube.com', 'https://youtube.com', 'https://m.youtube.com'];
  const GOOGLE_DOMAINS = ['https://www.google.com', 'https://accounts.google.com'];
  const HEADER = [
    '# Netscape HTTP Cookie File',
    '# נוצר על ידי תוסף "הורדות מהמייל" – לשימוש yt-dlp בלבד',
    '',
  ].join('\n');

  function hasCookieApi() {
    return typeof chrome !== 'undefined' && chrome.cookies && chrome.cookies.getAll;
  }

  function getAll(filter) {
    return new Promise((resolve, reject) => {
      chrome.cookies.getAll(filter, (list) => {
        const err = chrome.runtime && chrome.runtime.lastError;
        if (err) return reject(new Error(err.message));
        resolve(list || []);
      });
    });
  }

  /** מחזיר את כל העוגיות הרלוונטיות ליוטיוב (כולל google.com כברירת מחדל). */
  async function collect(includeGoogle) {
    if (!hasCookieApi()) throw new Error('אין הרשאת cookies לדפדפן הזה');
    const filters = [{ domain: '.youtube.com' }, { domain: 'youtube.com' }];
    if (includeGoogle) filters.push({ domain: '.google.com' }, { domain: 'google.com' });
    const results = await Promise.all(filters.map((f) => getAll(f).catch(() => [])));
    const seen = new Set();
    const cookies = [];
    for (const list of results) {
      for (const c of list) {
        const key = c.domain + '|' + c.path + '|' + c.name;
        if (seen.has(key)) continue;
        seen.add(key);
        cookies.push(c);
      }
    }
    return cookies;
  }

  /** האם יש בכלל עוגיות התחברות ליוטיוב. */
  function looksLoggedIn(cookies) {
    const important = ['SID', 'HSID', 'SSID', 'SAPISID', '__Secure-1PSID', '__Secure-3PSID', 'LOGIN_INFO'];
    return cookies.some((c) => important.includes(c.name));
  }

  /** ממיר עוגיות של הדפדפן לפורמט cookies.txt של Netscape. */
  function toNetscape(cookies) {
    const rows = [];
    for (const c of cookies) {
      const domain = c.domain || '';
      const hostOnly = c.hostOnly || !domain.startsWith('.');
      const expiry = c.expirationDate ? Math.floor(c.expirationDate) : 0;
      const value = String(c.value == null ? '' : c.value).replace(/[\t\n\r]/g, '');
      rows.push(
        [
          domain,
          hostOnly ? 'FALSE' : 'TRUE',
          c.path || '/',
          c.secure ? 'TRUE' : 'FALSE',
          String(expiry),
          c.name,
          value,
        ].join('\t')
      );
    }
    return HEADER + rows.join('\n') + '\n';
  }

  function bytesToBase64(bytes) {
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  }

  /** base64 של gzip (אם נתמך) – כדי לחסוך מקום ב-Secret. */
  async function packForSecret(text) {
    const bytes = new TextEncoder().encode(text);
    if (typeof CompressionStream === 'undefined') {
      return { payload: bytesToBase64(bytes), compressed: false };
    }
    const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream('gzip'));
    const gz = new Uint8Array(await new Response(stream).arrayBuffer());
    return { payload: bytesToBase64(gz), compressed: true };
  }

  /** מחזיר מזהה קצר של העוגיות כדי לדעת אם הן התחלפו. */
  function fingerprint(cookies) {
    const text = cookies.map((c) => c.name + '=' + c.value).sort().join('\n');
    // FNV-1a מספיק כאן – זו בדיקת "האם השתנה", לא הצפנה
    let h = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    return (h >>> 0).toString(16);
  }

  root.ytCookies = {
    collect,
    toNetscape,
    packForSecret,
    fingerprint,
    looksLoggedIn,
    hasCookieApi,
    YT_DOMAINS,
    GOOGLE_DOMAINS,
  };
})(typeof self !== 'undefined' ? self : this);

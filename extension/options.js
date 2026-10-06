/* לוגיקת עמוד ההגדרות של התוסף. */
(function () {
  'use strict';

  const u = window.util;
  const $ = u.$;
  const toast = u.toast;

  let settings = null;
  let gh = null;

  async function init() {
    settings = await u.loadSettings();
    gh = u.client(settings);
    bind();
    fillConnection();
    renderDefaults();
    await checkCookies();
    await testConnection(false);
  }

  function bind() {
    $('saveConnectionBtn').addEventListener('click', saveConnection);
    $('testConnectionBtn').addEventListener('click', () => testConnection(true));
    $('saveDefaultsBtn').addEventListener('click', saveDefaults);
    $('sendCookiesBtn').addEventListener('click', sendCookies);
    $('qualitySelect').addEventListener('change', () => {
      settings.DEFAULT_QUALITY = $('qualitySelect').value;
    });
  }

  function fillConnection() {
    const embedded = (window.EXT_CONFIG || {}).TOKEN || '';
    const tokenIsEmbedded = embedded && settings.TOKEN === embedded;
    $('tokenInput').placeholder = tokenIsEmbedded ? 'הטוקן מוטמע בתוסף ✓' : 'ghp_…';
    $('repoInput').value = settings.REPO || '';
    $('workflowInput').value = settings.WORKFLOW || 'downloader.yml';
    $('cookiesSecretInput').value = settings.COOKIES_SECRET || 'YT_COOKIES_B64';
    $('secretNameSpan').textContent = settings.COOKIES_SECRET || 'YT_COOKIES_B64';
  }

  function renderDefaults() {
    $('qualitySelect').innerHTML = window.qualities.QUALITIES.map(
      (q) => `<option value="${q.id}"${q.id === settings.DEFAULT_QUALITY ? ' selected' : ''}>${u.esc(q.label)}</option>`
    ).join('');
    $('googleCookiesChk').checked = settings.INCLUDE_GOOGLE_COOKIES !== false;
  }

  // ------------------------------------------------------------------ //
  async function saveConnection() {
    const patch = {
      REPO: $('repoInput').value.trim(),
      WORKFLOW: $('workflowInput').value.trim() || 'downloader.yml',
      COOKIES_SECRET: $('cookiesSecretInput').value.trim() || 'YT_COOKIES_B64',
    };
    const token = $('tokenInput').value.trim();
    if (token) patch.TOKEN = token;
    await u.saveSettings(patch);
    settings = await u.loadSettings();
    gh = u.client(settings);
    $('tokenInput').value = '';
    toast('נשמר ✓');
    await testConnection(false);
  }

  async function saveDefaults() {
    const patch = {
      DEFAULT_QUALITY: $('qualitySelect').value,
      INCLUDE_GOOGLE_COOKIES: $('googleCookiesChk').checked,
    };
    await u.saveSettings(patch);
    settings = Object.assign(settings, patch);
    toast('ברירות המחדל נשמרו ✓');
  }

  async function testConnection(verbose) {
    const pill = $('statePill');
    const box = $('alertBox');
    if (!settings.TOKEN || !settings.REPO) {
      pill.className = 'pill bad';
      pill.textContent = 'חסר חיבור';
      box.innerHTML = '<div class="alert warn">⚠️ חסרים טוקן או מאגר – מלאו אותם בכרטיס "חיבור ל-GitHub".</div>';
      return;
    }
    pill.className = 'pill busy';
    pill.textContent = 'בודק…';
    try {
      const info = await gh.repoInfo();
      const state = await gh.workflowState(settings.WORKFLOW);
      const secrets = await gh.secretNames();
      const problems = [];
      if (state !== 'active') problems.push('קובץ ה-Workflow לא נמצא או לא פעיל במאגר.');
      if (info.private) problems.push('המאגר פרטי – "קישור ישיר" לא יעבוד בלי התחברות. "הורדה מאובטחת" (עם הטוקן) יעבוד.');
      if (secrets.indexOf(settings.COOKIES_SECRET) < 0) {
        problems.push('עוגיות יוטיוב עוד לא נשלחו ל-Secret (יישלחו אוטומטית בהורדה הבאה).');
      }
      pill.className = 'pill good';
      pill.textContent = info.full_name || 'מחובר';
      box.innerHTML = problems.length
        ? problems.map((p) => `<div class="alert warn">⚠️ ${u.esc(p)}</div>`).join('')
        : '<div class="alert ok">✓ החיבור תקין</div>';
      if (verbose) toast('החיבור עובד ✓');
    } catch (err) {
      pill.className = 'pill bad';
      pill.textContent = 'שגיאה';
      box.innerHTML = `<div class="alert err">${u.esc(err.message)}</div>`;
      if (verbose) toast(err.message, true);
    }
  }

  // ------------------------------------------------------------------ //
  async function sendCookies() {
    const btn = $('sendCookiesBtn');
    btn.disabled = true;
    btn.textContent = 'שולח…';
    try {
      const cookies = await window.ytCookies.collect(settings.INCLUDE_GOOGLE_COOKIES);
      if (!cookies.length) throw new Error('לא נמצאו עוגיות של יוטיוב בדפדפן הזה');
      const text = window.ytCookies.toNetscape(cookies);
      const packed = await window.ytCookies.packForSecret(text);
      await gh.setSecret(settings.COOKIES_SECRET, packed.payload);
      const info = {
        count: cookies.length,
        fingerprint: window.ytCookies.fingerprint(cookies),
        at: new Date().toISOString(),
        sizeKb: Math.round(packed.payload.length / 1024),
        compressed: packed.compressed,
        loggedIn: window.ytCookies.looksLoggedIn(cookies),
      };
      settings.COOKIE_INFO = info;
      await u.saveSettings({ COOKIE_INFO: info });
      renderCookieInfo(info, true);
      toast('העוגיות רועננו ונשלחו ✓');
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = 'רענן עוגיות מהדפדפן עכשיו';
    }
  }

  async function checkCookies() {
    const info = settings.COOKIE_INFO || null;
    let exists = false;
    try {
      if (settings.TOKEN && settings.REPO) {
        const names = await gh.secretNames();
        exists = names.indexOf(settings.COOKIES_SECRET) >= 0;
      }
    } catch (err) {
      /* מתעלמים */
    }
    renderCookieInfo(info, exists);
  }

  function renderCookieInfo(info, exists) {
    const box = $('cookieInfo');
    if (!info && !exists) {
      box.textContent = 'עוד לא נשלחו עוגיות מהדפדפן הזה.';
      return;
    }
    const parts = [];
    if (exists) parts.push('<span class="pill good">Secret קיים במאגר</span>');
    else if (info) parts.push('<span class="pill bad">העוגיות לא נמצאו ב-Secret</span>');
    if (info) {
      parts.push(
        `<div style="margin-top:6px">רענון אחרון: ${u.esc(u.fmtDate(info.at))} · ${info.count} עוגיות · ${info.sizeKb}KB` +
        `${info.loggedIn ? ' · כולל התחברות ✓' : ' · ⚠️ נראה שאינכם מחוברים ליוטיוב'}</div>`
      );
    }
    box.innerHTML = parts.join('');
  }

  init().catch((err) => toast('שגיאה בטעינה: ' + err.message, true));
})();

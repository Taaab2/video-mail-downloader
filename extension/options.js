/* לוגיקת עמוד ההגדרות של התוסף. */
(function () {
  'use strict';

  const u = window.util;
  const $ = u.$;
  const toast = u.toast;

  let settings = null;
  let gh = null;
  let whitelist = { allow_all: false, users: [] };

  async function init() {
    settings = await u.loadSettings();
    gh = u.client(settings);
    bind();
    fillConnection();
    renderDefaults();
    await Promise.all([loadRemote(), checkCookies()]);
    await testConnection(false);
  }

  function bind() {
    $('saveConnectionBtn').addEventListener('click', saveConnection);
    $('testConnectionBtn').addEventListener('click', () => testConnection(true));
    $('saveDefaultsBtn').addEventListener('click', saveDefaults);
    $('sendCookiesBtn').addEventListener('click', sendCookies);
    $('wlAddBtn').addEventListener('click', () => {
      whitelist.users.push({ email: '', name: '', active: true });
      renderWhitelist();
    });
    $('wlSaveBtn').addEventListener('click', saveWhitelist);
    $('reloadBtn').addEventListener('click', async () => {
      await loadRemote();
      toast('נטען מחדש מהמאגר');
    });
    $('cfgSaveBtn').addEventListener('click', saveConfig);
    $('qualitySelect').addEventListener('change', () => {
      settings.DEFAULT_QUALITY = $('qualitySelect').value;
    });
    $('targetSelect').addEventListener('change', () => {
      settings.DEFAULT_TARGET = $('targetSelect').value;
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
    $('targetSelect').innerHTML = window.qualities.TARGETS.map(
      (t) => `<option value="${t.id}"${t.id === settings.DEFAULT_TARGET ? ' selected' : ''}>${u.esc(t.label)}</option>`
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
      DEFAULT_TARGET: $('targetSelect').value,
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
      if (info.private) problems.push('המאגר פרטי – קישורי ההורדה של GitHub לא יעבדו בלי התחברות. כדאי מאגר ציבורי.');
      if (secrets.indexOf(settings.COOKIES_SECRET) < 0) {
        problems.push('עוגיות יוטיוב עוד לא נשלחו ל-Secret.');
      }
      if (secrets.indexOf('MAIL_USER') < 0) {
        problems.push('פרטי המייל (MAIL_USER / MAIL_PASSWORD) לא הוגדרו – תהליך המייל לא יעבוד עד שיוגדרו.');
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
      toast('העוגיות נשלחו ל-Secret ✓');
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = 'שלח עוגיות מהדפדפן ל-Secret';
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
      /* מתעלמים – מוצג בהמשך */
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
        `<div style="margin-top:6px">נשלחו: ${u.esc(u.fmtDate(info.at))} · ${info.count} עוגיות · ${info.sizeKb}KB` +
        `${info.loggedIn ? ' · כולל התחברות ✓' : ' · ⚠️ נראה שאינכם מחוברים ליוטיוב'}</div>`
      );
    }
    box.innerHTML = parts.join('');
  }

  // ------------------------------------------------------------------ //
  // רשימה לבנה + הגדרות מהמאגר
  // ------------------------------------------------------------------ //
  async function loadRemote() {
    if (!settings.TOKEN || !settings.REPO) return;
    try {
      const wl = await gh.getFile('whitelist.json');
      if (wl.text) {
        const parsed = JSON.parse(wl.text);
        whitelist = {
          allow_all: !!parsed.allow_all,
          users: Array.isArray(parsed.users) ? parsed.users : [],
        };
      }
      renderWhitelist();

      const cfg = await gh.getFile('config.json');
      if (cfg.text) renderConfig(JSON.parse(cfg.text));
    } catch (err) {
      toast('לא הצלחתי לקרוא את קבצי המאגר: ' + err.message, true);
    }
  }

  function renderWhitelist() {
    $('allowAllChk').checked = !!whitelist.allow_all;
    const rows = (whitelist.users || []).map((entry, index) => `
      <tr>
        <td><input type="text" dir="ltr" value="${u.esc(entry.email)}" data-field="email" data-i="${index}" placeholder="name@example.com"></td>
        <td><input type="text" value="${u.esc(entry.name || '')}" data-field="name" data-i="${index}"></td>
        <td style="text-align:center"><input type="checkbox" data-field="active" data-i="${index}" ${entry.active === false ? '' : 'checked'}></td>
        <td><button class="btn ghost sm" data-remove="${index}">מחק</button></td>
      </tr>`).join('');
    $('wlTable').querySelector('tbody').innerHTML =
      rows || '<tr><td colspan="4" class="muted tiny">אין כתובות ברשימה.</td></tr>';
    u.$$('[data-remove]').forEach((btn) => {
      btn.addEventListener('click', () => {
        whitelist.users.splice(Number(btn.getAttribute('data-remove')), 1);
        renderWhitelist();
      });
    });
  }

  function collectWhitelist() {
    const users = [];
    u.$$('#wlTable tbody tr').forEach((tr) => {
      const email = tr.querySelector('[data-field="email"]');
      if (!email) return;
      const name = tr.querySelector('[data-field="name"]');
      const active = tr.querySelector('[data-field="active"]');
      const value = email.value.trim();
      if (!value) return;
      users.push({ email: value, name: name ? name.value.trim() : '', active: active ? active.checked : true });
    });
    return { allow_all: $('allowAllChk').checked, users };
  }

  async function saveWhitelist() {
    const data = collectWhitelist();
    try {
      const current = await gh.getFile('whitelist.json');
      await gh.putFile(
        'whitelist.json',
        JSON.stringify(data, null, 2) + '\n',
        'עדכון הרשימה הלבנה מהתוסף',
        current.sha
      );
      whitelist = data;
      renderWhitelist();
      toast('הרשימה הלבנה נשמרה במאגר ✓');
    } catch (err) {
      toast(err.message, true);
    }
  }

  function renderConfig(cfg) {
    $('keywordsInput').value = Array.isArray(cfg.subject_keywords) ? cfg.subject_keywords.join(', ') : '';
    $('maxSizeInput').value = cfg.max_file_size_mb != null ? cfg.max_file_size_mb : '';
    $('maxLinksInput').value = cfg.max_links_per_email != null ? cfg.max_links_per_email : '';
    $('localScanInput').value = cfg.local_scan_limit != null ? cfg.local_scan_limit : '';
    $('notifyFailChk').checked = !!cfg.notify_on_failure;
    $('notifyUnauthChk').checked = !!cfg.notify_unauthorized;
    $('playlistsChk').checked = !!cfg.allow_playlists;
    $('asciiChk').checked = !!cfg.ascii_filenames;
  }

  async function saveConfig() {
    try {
      const current = await gh.getFile('config.json');
      const cfg = current.text ? JSON.parse(current.text) : {};
      cfg.subject_keywords = $('keywordsInput').value.split(',').map((s) => s.trim()).filter(Boolean);
      cfg.max_file_size_mb = Number($('maxSizeInput').value) || 1900;
      cfg.max_links_per_email = Number($('maxLinksInput').value) || 3;
      cfg.local_scan_limit = Number($('localScanInput').value) || 50;
      cfg.notify_on_failure = $('notifyFailChk').checked;
      cfg.notify_unauthorized = $('notifyUnauthChk').checked;
      cfg.allow_playlists = $('playlistsChk').checked;
      cfg.ascii_filenames = $('asciiChk').checked;
      await gh.putFile('config.json', JSON.stringify(cfg, null, 2) + '\n', 'עדכון הגדרות מהתוסף', current.sha);
      toast('ההגדרות נשמרו במאגר ✓');
    } catch (err) {
      toast(err.message, true);
    }
  }

  init().catch((err) => toast('שגיאה בטעינה: ' + err.message, true));
})();

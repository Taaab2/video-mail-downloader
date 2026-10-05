/* לוגיקת החלון הקופץ של התוסף. */
(function () {
  'use strict';

  const u = window.util;
  const $ = u.$;
  const toast = u.toast;

  let settings = null;
  let gh = null;
  let pollTimer = null;

  // ------------------------------------------------------------------ //
  // אתחול
  // ------------------------------------------------------------------ //
  async function init() {
    settings = await u.loadSettings();
    gh = u.client(settings);

    $('repoPill').textContent = settings.REPO || 'לא הוגדר מאגר';
    renderSelects();
    await prefillUrl();
    await refreshRecentRuns();
    await renderReleases();
    await checkCookies(false);
    renderConnectionState();

    $('downloadBtn').addEventListener('click', () => download());
    $('useTabBtn').addEventListener('click', async () => {
      await prefillUrl(true);
    });
    $('cookieBtn').addEventListener('click', () => sendCookies());
    $('cookieCheckBtn').addEventListener('click', () => checkCookies(true));
    $('refreshReleasesBtn').addEventListener('click', () => renderReleases());
    $('openOptions').addEventListener('click', (event) => {
      if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.openOptionsPage) {
        event.preventDefault();
        chrome.runtime.openOptionsPage();
      }
    });
    for (const id of ['qualitySelect', 'targetSelect']) {
      $(id).addEventListener('change', () => {
        const patch = {};
        patch[id === 'qualitySelect' ? 'DEFAULT_QUALITY' : 'DEFAULT_TARGET'] = $(id).value;
        settings = Object.assign(settings, patch);
        u.saveSettings(patch);
      });
    }
  }

  function renderSelects() {
    $('qualitySelect').innerHTML = window.qualities.QUALITIES.map(
      (q) => `<option value="${q.id}"${q.id === settings.DEFAULT_QUALITY ? ' selected' : ''}>${u.esc(q.label)}</option>`
    ).join('');
    $('targetSelect').innerHTML = window.qualities.TARGETS.map(
      (t) => `<option value="${t.id}"${t.id === settings.DEFAULT_TARGET ? ' selected' : ''}>${u.esc(t.label)}</option>`
    ).join('');
  }

  function renderConnectionState() {
    const alertBox = $('alertBox');
    const problems = [];
    if (!settings.TOKEN) problems.push('לא הוגדר טוקן GitHub – פתחו "הגדרות" והדביקו טוקן.');
    if (!settings.REPO || settings.REPO.indexOf('/') < 0) problems.push('לא הוגדר מאגר (owner/repo) בהגדרות.');
    if (!problems.length) {
      alertBox.innerHTML = '';
      $('repoPill').className = 'pill good';
      return;
    }
    alertBox.innerHTML = problems.map((p) => `<div class="alert warn">⚠️ ${u.esc(p)}</div>`).join('');
    $('repoPill').className = 'pill bad';
  }

  async function prefillUrl(force) {
    const input = $('urlInput');
    const current = await u.activeTabUrl();
    const isVideo = /youtube\.com|youtu\.be|vimeo|facebook|instagram|tiktok|twitter|x\.com/i.test(current || '');
    if (isVideo && (force || !input.value)) input.value = current;
  }

  // ------------------------------------------------------------------ //
  // הורדה
  // ------------------------------------------------------------------ //
  function normalizeUrl(value) {
    let url = String(value || '').trim();
    if (!url) return '';
    if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
    return url;
  }

  async function download() {
    const url = normalizeUrl($('urlInput').value);
    if (!url) {
      toast('הדביקו קישור לסרטון', true);
      return;
    }
    if (!settings.TOKEN || !settings.REPO) {
      toast('קודם הגדירו טוקן ומאגר בהגדרות', true);
      return;
    }
    const quality = $('qualitySelect').value;
    const target = $('targetSelect').value;

    setRunPill('busy', 'שולח…');
    $('downloadBtn').disabled = true;
    try {
      // זוכרים איזו הרצה הייתה האחרונה, כדי לא להתבלבל איתה כשהחדשה עוד לא הופיעה
      const before = await gh.listRuns(settings.WORKFLOW, 1).catch(() => []);
      const beforeId = before.length ? before[0].id : null;
      await u.saveSettings({ BEFORE_RUN_ID: beforeId, PENDING: true });
      settings.BEFORE_RUN_ID = beforeId;
      settings.PENDING = true;

      await gh.dispatch(settings.WORKFLOW, {
        url,
        quality,
        target,
        dry_run: false,
        max_messages: '0',
      });
      toast('ההרצה נשלחה ל-GitHub');
      setRunPill('busy', 'ממתין להתחלה…');
      startPolling();
      await pollRuns(true);
    } catch (err) {
      await u.saveSettings({ PENDING: false });
      settings.PENDING = false;
      setRunPill('bad', 'שגיאה');
      toast(err.message, true);
      $('downloadBtn').disabled = false;
    }
  }

  function setRunPill(kind, text) {
    const pill = $('runPill');
    pill.style.display = 'inline-block';
    pill.className = 'pill ' + (kind || '');
    pill.textContent = text;
  }

  async function refreshRecentRuns() {
    if (!settings.TOKEN || !settings.REPO) return;
    try {
      await pollRuns(false);
    } catch (err) {
      /* שקט – לא מפריעים */
    }
  }

  /** מנטר את ההרצה האחרונה. כשמסתיימת בהצלחה – מציג קישורים מדווחים. */
  async function pollRuns(fromDispatch) {
    const runs = await gh.listRuns(settings.WORKFLOW, 3);
    if (!runs.length) {
      if (fromDispatch) setRunPill('busy', 'ממתין להופעת ההרצה…');
      return;
    }
    const run = runs[0];

    // ממתינים שההרצה שהפעלנו תופיע ברשימה, ולא מתייחסים להרצה קודמת
    if (settings.PENDING && settings.BEFORE_RUN_ID && run.id === settings.BEFORE_RUN_ID) {
      setRunPill('busy', 'ממתין להתחלה…');
      startPolling();
      return;
    }

    if (run.status !== 'completed') {
      setRunPill('busy', run.status === 'queued' ? 'בתור…' : 'מוריד…');
      $('resultBox').innerHTML =
        '<div class="tiny muted">ההרצה רצה בענן. ' +
        `<a class="link" href="${u.esc(run.html_url)}" target="_blank">צפייה בהתקדמות ב-GitHub</a></div>`;
      startPolling();
      return;
    }

    stopPolling();
    $('downloadBtn').disabled = false;
    settings.PENDING = false;
    u.saveSettings({ PENDING: false });
    if (run.conclusion === 'success') {
      setRunPill('good', 'הסתיים ✓');
      await showLatestDownload(run);
    } else {
      setRunPill('bad', 'נכשל');
      $('resultBox').innerHTML =
        `<div class="alert err">ההרצה נכשלה. <a class="link" href="${u.esc(run.html_url)}" target="_blank">פתחו את הלוג ב-GitHub</a> ` +
        'כדי לראות את סיבת הכשל (לרוב קישור שאינו נתמך או עוגיות חסרות).</div>';
      if (fromDispatch) toast('ההרצה נכשלה – ראו לוג ב-GitHub', true);
    }
  }

  async function showLatestDownload(run) {
    try {
      const releases = await gh.listReleases(1);
      const latest = releases[0];
      if (!latest || !latest.assets || !latest.assets.length) {
        $('resultBox').innerHTML =
          `<div class="alert warn">ההרצה הסתיימה אבל לא נמצא קובץ חדש. ` +
          `<a class="link" href="${u.esc(run.html_url)}" target="_blank">בדקו את הלוג</a>.</div>`;
        return;
      }
      const rows = latest.assets.map((a) => `
        <tr>
          <td>${u.esc(a.name)}<br><span class="tiny muted">${u.esc(u.humanSize(a.size))}</span></td>
          <td style="width:120px;white-space:nowrap">
            <a class="link" href="${u.esc(a.url)}" target="_blank">הורדה</a>
            <span class="link" style="margin-inline-start:8px" data-copy="${u.esc(a.url)}">העתק</span>
          </td>
        </tr>`).join('');
      $('resultBox').innerHTML =
        `<div class="result"><strong>מוכן להורדה ✓</strong><table style="margin-top:6px">${rows}</table></div>`;
      bindCopyButtons($('resultBox'));
      await renderReleases();
    } catch (err) {
      toast(err.message, true);
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(() => {
      pollRuns(false).catch(() => {});
    }, 5000);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function bindCopyButtons(scope) {
    u.$$('[data-copy]', scope).forEach((el) => {
      el.addEventListener('click', () => u.copyText(el.getAttribute('data-copy')));
    });
  }

  // ------------------------------------------------------------------ //
  // עוגיות
  // ------------------------------------------------------------------ //
  async function sendCookies() {
    const btn = $('cookieBtn');
    btn.disabled = true;
    btn.textContent = 'שולח…';
    try {
      const cookies = await window.ytCookies.collect(settings.INCLUDE_GOOGLE_COOKIES);
      if (!cookies.length) {
        throw new Error('לא נמצאו עוגיות של יוטיוב. פתחו את יוטיוב בדפדפן ונסו שוב.');
      }
      const loggedIn = window.ytCookies.looksLoggedIn(cookies);
      const text = window.ytCookies.toNetscape(cookies);
      const packed = await window.ytCookies.packForSecret(text);
      const approxKb = Math.round(packed.payload.length / 1024);
      if (approxKb > 32) {
        throw new Error(`העוגיות גדולות מדי ל-Secret (${approxKb}KB). נסו לכבות עוגיות google.com בהגדרות.`);
      }
      await gh.setSecret(settings.COOKIES_SECRET, packed.payload);
      const info = {
        count: cookies.length,
        fingerprint: window.ytCookies.fingerprint(cookies),
        at: new Date().toISOString(),
        sizeKb: approxKb,
        compressed: packed.compressed,
        loggedIn,
      };
      settings.COOKIE_INFO = info;
      await u.saveSettings({ COOKIE_INFO: info });
      renderCookieInfo(info, true);
      toast('העוגיות נשלחו ל-Secret ✓');
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = 'שלח עוגיות ל-Secret';
    }
  }

  async function checkCookies(verbose) {
    try {
      const info = settings.COOKIE_INFO || null;
      const names = settings.TOKEN && settings.REPO ? await gh.secretNames() : [];
      const exists = names.indexOf(settings.COOKIES_SECRET) >= 0;
      let current = null;
      let currentCount = 0;
      if (window.ytCookies.hasCookieApi()) {
        try {
          const cookies = await window.ytCookies.collect(settings.INCLUDE_GOOGLE_COOKIES);
          currentCount = cookies.length;
          current = window.ytCookies.fingerprint(cookies);
        } catch (err) {
          current = null;
        }
      }
      renderCookieInfo(info, exists, current, currentCount);
      if (verbose) {
        toast(exists ? 'העוגיות מוגדרות ב-Secret ✓' : 'עוד לא נשלחו עוגיות', !exists);
      }
    } catch (err) {
      $('cookieInfo').textContent = err.message;
      if (verbose) toast(err.message, true);
    }
  }

  function renderCookieInfo(info, exists, currentFingerprint, currentCount) {
    const box = $('cookieInfo');
    if (!info && !exists) {
      box.innerHTML = 'עוד לא נשלחו עוגיות מהדפדפן הזה.';
      return;
    }
    const parts = [];
    if (exists) parts.push('<span class="pill good">Secret קיים במאגר</span>');
    else if (info) parts.push('<span class="pill bad">העוגיות לא נמצאו ב-Secret</span>');
    if (info) {
      parts.push(
        `<div style="margin-top:6px">נשלחו: ${u.esc(u.fmtDate(info.at))} · ${info.count} עוגיות · ${info.sizeKb}KB` +
        `${info.loggedIn ? ' · כולל עוגיות התחברות ✓' : ' · ⚠️ נראה שאינכם מחוברים ליוטיוב'}${info.compressed ? ' · דחוס' : ''}</div>`
      );
    }
    if (currentFingerprint && info && currentFingerprint !== info.fingerprint) {
      parts.push('<div style="margin-top:6px" class="pill busy">העוגיות בדפדפן השתנו – כדאי לשלוח שוב</div>');
    }
    box.innerHTML = parts.join('');
  }

  // ------------------------------------------------------------------ //
  // הורדות אחרונות
  // ------------------------------------------------------------------ //
  async function renderReleases() {
    const box = $('releasesBox');
    if (!settings.TOKEN || !settings.REPO) {
      box.textContent = 'לא הוגדר חיבור ל-GitHub.';
      return;
    }
    box.textContent = 'טוען…';
    try {
      const releases = await gh.listReleases(5);
      if (!releases.length) {
        box.textContent = 'עדיין אין הורדות.';
        return;
      }
      const html = releases.map((rel) => `
        <div style="border:1px solid var(--line);border-radius:9px;padding:8px;margin-bottom:8px">
          <div class="tiny muted">${u.esc(u.fmtDate(rel.created_at))}</div>
          <table>${rel.assets.map((a) => `
            <tr>
              <td>${u.esc(a.name)}<br><span class="tiny muted">${u.esc(u.humanSize(a.size))}</span></td>
              <td style="width:110px;white-space:nowrap">
                <a class="link" href="${u.esc(a.url)}" target="_blank">הורדה</a>
                <span class="link" style="margin-inline-start:8px" data-copy="${u.esc(a.url)}">העתק</span>
              </td>
            </tr>`).join('')}</table>
        </div>`).join('');
      box.innerHTML = html;
      bindCopyButtons(box);
    } catch (err) {
      box.textContent = err.message;
    }
  }

  init().catch((err) => {
    toast('שגיאה בטעינה: ' + err.message, true);
  });
})();

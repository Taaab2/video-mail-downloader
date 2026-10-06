/* לוגיקת לוח הבקרה (נפתח כעמוד מלא). */
(function () {
  'use strict';

  const u = window.util;
  const $ = u.$;
  const toast = u.toast;

  let settings = null;
  let gh = null;
  let pollTimer = null;
  let busy = false;

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
    $('pasteBtn').addEventListener('click', pasteFromClipboard);
    $('useTabBtn').addEventListener('click', async () => {
      await prefillUrl(true);
    });
    $('cookieBtn').addEventListener('click', () => refreshCookies(true));
    $('cookieCheckBtn').addEventListener('click', () => checkCookies(true));
    $('refreshReleasesBtn').addEventListener('click', () => renderReleases());
    $('openOptions').addEventListener('click', (event) => {
      if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.openOptionsPage) {
        event.preventDefault();
        chrome.runtime.openOptionsPage();
      }
    });
    $('urlInput').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') download();
    });
    $('qualitySelect').addEventListener('change', () => {
      settings.DEFAULT_QUALITY = $('qualitySelect').value;
      u.saveSettings({ DEFAULT_QUALITY: settings.DEFAULT_QUALITY });
    });
  }

  function renderSelects() {
    $('qualitySelect').innerHTML = window.qualities.QUALITIES.map(
      (q) => `<option value="${q.id}"${q.id === settings.DEFAULT_QUALITY ? ' selected' : ''}>${u.esc(q.label)}</option>`
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

  /** כפתור ההדבק: קורא את הלוח דרך ה-API של התוסף, ואם אין – דרך הדבקה רגילה. */
  async function pasteFromClipboard() {
    const input = $('urlInput');
    try {
      let text = '';
      if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.readText) {
        text = await navigator.clipboard.readText();
      }
      if (!text) {
        input.focus();
        input.select();
        toast('רשמו Ctrl+V להדבקה', true);
        return;
      }
      input.value = text.trim();
      toast('הודבק ✓');
    } catch (err) {
      input.focus();
      toast('אין הרשאה לקרוא מהלוח – הדביקו ידנית (Ctrl+V)', true);
    }
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
    if (busy) return;
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

    busy = true;
    $('downloadBtn').disabled = true;
    // תמיד שולחים עוגיות טריות ל-GitHub לפני ההורדה – בלי שהמשתמש צריך
    // ללחוץ על שום כפתור. אם אין עוגיות או שיש שגיאה – ממשיכים בכל זאת.
    setRunPill('busy', 'מרענן עוגיות…');
    try {
      const cookieStatus = await refreshCookies(false);
      if (cookieStatus === 'error') {
        toast('רענון העוגיות נכשל – ממשיך בכל זאת', true);
      }

      // מפעילים את ההורדה
      setRunPill('busy', 'שולח…');
      const before = await gh.listRuns(settings.WORKFLOW, 1).catch(() => []);
      const beforeId = before.length ? before[0].id : null;
      await u.saveSettings({ BEFORE_RUN_ID: beforeId, PENDING: true });
      settings.BEFORE_RUN_ID = beforeId;
      settings.PENDING = true;

      await gh.dispatch(settings.WORKFLOW, { url, quality });
      toast('ההרצה נשלחה ל-GitHub');
      setRunPill('busy', 'ממתין להתחלה…');
      startPolling();
      await pollRuns(true);
    } catch (err) {
      await u.saveSettings({ PENDING: false });
      settings.PENDING = false;
      setRunPill('bad', 'שגיאה');
      toast(err.message, true);
    } finally {
      busy = false;
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
      /* שקט */
    }
  }

  async function pollRuns(fromDispatch) {
    const runs = await gh.listRuns(settings.WORKFLOW, 3);
    if (!runs.length) {
      if (fromDispatch) setRunPill('busy', 'ממתין להופעת ההרצה…');
      return;
    }
    const run = runs[0];

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
          <td>${u.esc(a.label || a.name)}<br><span class="tiny muted">${u.esc(u.humanSize(a.size))}</span></td>
          <td style="width:230px;white-space:nowrap">
            <button class="btn sm" data-token-dl="${a.id != null ? a.id : ''}" data-name="${u.esc(a.label || a.name)}" data-url="${u.esc(a.url)}">הורדה מאובטחת</button>
            <button class="btn ghost sm" data-copy="${u.esc(a.url)}">קישור ישיר</button>
          </td>
        </tr>`).join('');
      $('resultBox').innerHTML =
        `<div class="result"><strong>מוכן ✓ – בחרו איך להוריד</strong>` +
        `<table style="margin-top:6px">${rows}</table>` +
        `<div class="tiny muted" style="margin-top:6px">"הורדה מאובטחת" מורידה דרך הטוקן (עובד גם במאגר פרטי). ` +
        `"קישור ישיר" מעתיק קישור GitHub שאפשר לפתוח בכל דפדפן.</div></div>`;
      bindResultButtons($('resultBox'));
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

  function bindResultButtons(scope) {
    u.$$('[data-copy]', scope).forEach((el) => {
      el.addEventListener('click', () => u.copyText(el.getAttribute('data-copy'), 'הקישור'));
    });
    u.$$('[data-token-dl]', scope).forEach((el) => {
      el.addEventListener('click', async () => {
        const id = el.getAttribute('data-token-dl');
        const name = el.getAttribute('data-name');
        const directUrl = el.getAttribute('data-url');
        el.disabled = true;
        const label = el.textContent;
        el.textContent = 'מוריד…';
        try {
          await gh.downloadAsset(id, name);
          toast('ההורדה החלה ✓');
        } catch (err) {
          // נפילה חיננית: אם ההורדה עם הטוקן לא זמינה בדפדפן הזה – לוקחים את הקישור הישיר
          if (directUrl) {
            u.copyText(directUrl, 'הקישור הישיר (ההורדה המאובטחת לא זמינה כאן)');
          } else {
            toast(err.message, true);
          }
        } finally {
          el.disabled = false;
          el.textContent = label;
        }
      });
    });
  }

  // ------------------------------------------------------------------ //
  // עוגיות
  // ------------------------------------------------------------------ //
  /**
   * אוסף עוגיות יוטיוב טריות מהדפדפן ושולח אותן ל-Secret.
   * נקרא אוטומטית לפני כל הורדה. מחזיר 'ok' | 'empty' | 'error' | 'noconn'.
   */
  async function refreshCookies(verbose) {
    if (!settings.TOKEN || !settings.REPO) {
      if (verbose) toast('קודם הגדירו טוקן ומאגר', true);
      return 'noconn';
    }
    try {
      const cookies = await window.ytCookies.collect(settings.INCLUDE_GOOGLE_COOKIES);
      if (!cookies.length) {
        if (verbose) toast('לא נמצאו עוגיות של יוטיוב בדפדפן', true);
        return 'empty';
      }
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
      if (verbose) toast('העוגיות רועננו ונשלחו ✓');
      return 'ok';
    } catch (err) {
      if (verbose) toast('רענון העוגיות נכשל: ' + err.message, true);
      return 'error';
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
        `<div style="margin-top:6px">רענון אחרון: ${u.esc(u.fmtDate(info.at))} · ${info.count} עוגיות · ${info.sizeKb}KB` +
        `${info.loggedIn ? ' · כולל התחברות ✓' : ' · ⚠️ נראה שאינכם מחוברים ליוטיוב'}${info.compressed ? ' · דחוס' : ''}</div>`
      );
    }
    if (currentFingerprint && info && currentFingerprint !== info.fingerprint) {
      parts.push('<div style="margin-top:6px" class="pill busy">העוגיות בדפדפן השתנו – כדאי לרענן</div>');
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
              <td>${u.esc(a.label || a.name)}<br><span class="tiny muted">${u.esc(u.humanSize(a.size))}</span></td>
              <td style="width:210px;white-space:nowrap">
                <button class="btn sm" data-token-dl="${a.id != null ? a.id : ''}" data-name="${u.esc(a.label || a.name)}" data-url="${u.esc(a.url)}">הורדה מאובטחת</button>
                <button class="btn ghost sm" data-copy="${u.esc(a.url)}">קישור ישיר</button>
              </td>
            </tr>`).join('')}</table>
        </div>`).join('');
      box.innerHTML = html;
      bindResultButtons(box);
    } catch (err) {
      box.textContent = err.message;
    }
  }

  init().catch((err) => {
    toast('שגיאה בטעינה: ' + err.message, true);
  });
})();

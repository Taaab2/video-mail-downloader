#!/usr/bin/env node
/* בדיקות לתוסף – רצות ב-Node בלי דפדפן:
 *   node extension/dev/tests.js          # בדיקות מקומיות
 *   node extension/dev/tests.js --live   # כולל כתיבת Secret אמיתי ל-GitHub (מוודא תאימות ל-libsodium)
 */
'use strict';

const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
global.self = global;
global.window = global;

// הספריות נטענות כמו בדפדפן
global.nacl = require(path.join(ROOT, 'lib/vendor/nacl-fast.min.js'));
if (!global.nacl || !global.nacl.box) throw new Error('nacl לא נטען');
require(path.join(ROOT, 'lib/vendor/blake2b.js'));
if (!global.blakejs) throw new Error('blakejs לא נטען');
require(path.join(ROOT, 'lib/sealedbox.js'));
require(path.join(ROOT, 'lib/cookies.js'));
require(path.join(ROOT, 'lib/quality.js'));

let passed = 0;
const failed = [];
function check(condition, label) {
  if (condition) {
    passed++;
    console.log('  ✓ ' + label);
  } else {
    failed.push(label);
    console.log('  ✗ ' + label);
  }
}

// --------------------------------------------------------------------- //
function testSealedBox() {
  console.log('הצפנת sealed-box:');
  const recipient = global.nacl.box.keyPair();
  const pk = global.sealedbox.bytesToB64(recipient.publicKey);
  const sk = global.sealedbox.bytesToB64(recipient.secretKey);

  const message = 'cookies.txt – שלום עולם! 🍪';
  const sealed = global.sealedbox.sealToBase64(message, pk);
  const opened = global.sealedbox.openFromBase64(sealed, pk, sk);
  check(opened === message, 'טקסט בעברית ואמוג׳י חוזר בדיוק אחרי הצפנה/פענוח');

  const size = global.sealedbox.b64ToBytes(sealed).length;
  const rawLen = new TextEncoder().encode(message).length;
  check(size === 32 + rawLen + 16, 'גודל הקופסה = 32 + טקסט + 16 (מבנה libsodium)');

  const again = global.sealedbox.sealToBase64(message, pk);
  check(again !== sealed, 'כל הצפנה יוצרת מפתח זמני אחר');

  // המפתח הזמני חייב להיות שונה מזה של הנמען (אחרת אין אטימות)
  const ephemeral = global.sealedbox.b64ToBytes(sealed).subarray(0, 32);
  check(Buffer.from(ephemeral).toString('hex') !== Buffer.from(recipient.publicKey).toString('hex'),
    'המפתח הזמני שונה ממפתח הנמען');

  let threw = false;
  try {
    global.sealedbox.sealToBase64('x', 'not-a-key');
  } catch (err) {
    threw = true;
  }
  check(threw, 'מפתח לא תקין נדחה בשגיאה');
}

function testCookies() {
  console.log('המרת עוגיות ל-cookies.txt:');
  const cookies = [
    { domain: '.youtube.com', name: 'SID', value: 'abc123', path: '/', secure: true, hostOnly: false, expirationDate: 1800000000.5 },
    { domain: 'www.youtube.com', name: 'PREF', value: 'tz=Asia%2FJerusalem', path: '/', secure: false, hostOnly: true },
    { domain: '.google.com', name: 'NID', value: 'with\ttab', path: '/', secure: true, hostOnly: false, expirationDate: 1800000001 },
  ];
  const text = global.ytCookies.toNetscape(cookies);
  const lines = text.split('\n').filter((l) => l && !l.startsWith('#'));

  check(text.startsWith('# Netscape HTTP Cookie File'), 'כותרת הפורמט של Netscape');
  check(lines.length === 3, 'כל עוגייה בשורה נפרדת');
  const first = lines[0].split('\t');
  check(first.length === 7, 'לכל שורה 7 שדות מופרדים בטאב');
  check(first[0] === '.youtube.com' && first[1] === 'TRUE', 'דומיין עם נקודה מסומן כ-TRUE');
  check(first[3] === 'TRUE', 'עוגייה מאובטחת מסומנת כ-TRUE');
  check(first[4] === '1800000000', 'תוקף מומר לשניות שלמות');
  check(first[5] === 'SID' && first[6] === 'abc123', 'שם העוגייה והערך נשמרים');
  const second = lines[1].split('\t');
  check(second[1] === 'FALSE' && second[4] === '0', 'עוגיית סשן ללא תוקף מסומנת כ-0');
  check(!text.includes('with\ttab'), 'טאבים בתוך הערך מנוקים');

  const fp1 = global.ytCookies.fingerprint(cookies);
  const fp2 = global.ytCookies.fingerprint(cookies.concat([{ domain: '.youtube.com', name: 'X', value: '1' }]));
  check(fp1 !== fp2, 'טביעת האצבע משתנה כשמתווספת עוגייה');
  check(global.ytCookies.fingerprint(cookies) === fp1, 'טביעת האצבע יציבה');
}

async function testPacking() {
  console.log('אריזת העוגיות ל-Secret:');
  const big = global.ytCookies.toNetscape(
    Array.from({ length: 200 }, (_, i) => ({
      domain: '.youtube.com', name: 'C' + i, value: 'v'.repeat(40), path: '/', secure: true, hostOnly: false,
    }))
  );
  const packed = await global.ytCookies.packForSecret(big);
  check(packed.compressed === true, 'נדחס ב-gzip בדפדפן תומך');
  const raw = Buffer.from(packed.payload, 'base64');
  check(raw[0] === 0x1f && raw[1] === 0x8b, 'הבתים מתחילים בחתימת gzip (1f 8b) – מה שהעובד מחפש');

  const zlib = require('zlib');
  const decoded = zlib.gunzipSync(raw).toString('utf8');
  check(decoded === big, 'אחרי פענוח gzip מתקבל אותו קובץ cookies.txt');
  check(packed.payload.length < big.length, 'הדחיסה מקטינה את הנפח שנשלח');
}

function testQualityParity() {
  console.log('התאמת האיכויות בין התוסף לעובד:');
  const jsIds = global.qualities.QUALITIES.map((q) => q.id).sort();
  const pyIds = execFileSync('python', ['-c',
    'import sys, json; sys.path.insert(0, "scripts"); from process_inbox import QUALITY_PRESETS; print(json.dumps(sorted(QUALITY_PRESETS)))'
  ], { cwd: path.resolve(ROOT, '..'), encoding: 'utf8', env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' }) });
  const parsed = JSON.parse(pyIds.trim());
  check(JSON.stringify(jsIds) === JSON.stringify(parsed),
    'אותם שמות איכות בשני הצדדים: ' + jsIds.join(', '));

  const targets = global.qualities.TARGETS.map((t) => t.id).sort();
  check(JSON.stringify(targets) === JSON.stringify(['both', 'drive', 'github']), 'יעדי העלאה תואמים לעובד');
}

async function testLiveSecret() {
  console.log('כתיבת Secret אמיתי ל-GitHub (תאימות ל-libsodium):');
  try {
    require(path.join(ROOT, 'lib/config.js'));
  } catch (err) {
    console.log('  (אין קובץ config.js מקומי – משתמשים במשתני סביבה)');
  }
  const token = process.env.GITHUB_TOKEN || (global.EXT_CONFIG || {}).TOKEN;
  const repo = process.env.GITHUB_REPO || (global.EXT_CONFIG || {}).REPO;
  if (!token || !repo) {
    console.log('  (דולג – אין טוקן או מאגר בסביבה)');
    return;
  }
  const keyResp = await fetch(`https://api.github.com/repos/${repo}/actions/secrets/public-key`, {
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
  });
  if (!keyResp.ok) throw new Error('שליפת המפתח הציבורי נכשלה: ' + keyResp.status);
  const key = await keyResp.json();

  const sample = global.ytCookies.toNetscape([
    { domain: '.youtube.com', name: 'E2E_TEST', value: 'test-only', path: '/', secure: true, hostOnly: false, expirationDate: 1900000000 },
  ]);
  const packed = await global.ytCookies.packForSecret(sample);
  const encrypted_value = global.sealedbox.sealToBase64(packed.payload, key.key);

  const put = await fetch(`https://api.github.com/repos/${repo}/actions/secrets/YT_COOKIES_B64`, {
    method: 'PUT',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ encrypted_value, key_id: key.key_id }),
  });
  check(put.status === 201 || put.status === 204,
    `GitHub קיבל את הקופסה האטומה מהתוסף (HTTP ${put.status})`);
  const names = await (await fetch(`https://api.github.com/repos/${repo}/actions/secrets`, {
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
  })).json();
  check((names.secrets || []).some((s) => s.name === 'YT_COOKIES_B64'),
    'הסקרוט YT_COOKIES_B64 מופיע ברשימת הסודות של המאגר');
}

(async function main() {
  testSealedBox();
  testCookies();
  await testPacking();
  testQualityParity();
  if (process.argv.includes('--live')) await testLiveSecret();

  console.log('');
  console.log(`עברו ${passed} בדיקות, נכשלו ${failed.length}`);
  failed.forEach((label) => console.log('  ✗ ' + label));
  process.exit(failed.length ? 1 : 0);
})().catch((err) => {
  console.error('שגיאה בהרצת הבדיקות:', err);
  process.exit(1);
});

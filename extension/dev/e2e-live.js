#!/usr/bin/env node
/* בדיקת קצה-לקצה חיה: משתמש בלקוח GitHub של התוסף עצמו (lib/github.js) כדי
 * להפעיל הרצה בענן עם קישור + איכות, וממתין לקישור ההורדה הסופי.
 *
 *   GITHUB_TOKEN=... GITHUB_REPO=owner/repo node extension/dev/e2e-live.js "<url>" [quality]
 */
'use strict';

const path = require('path');
const ROOT = path.resolve(__dirname, '..');
global.self = global;
global.window = global;

global.nacl = require(path.join(ROOT, 'lib/vendor/nacl-fast.min.js'));
require(path.join(ROOT, 'lib/vendor/blake2b.js'));
require(path.join(ROOT, 'lib/sealedbox.js'));
require(path.join(ROOT, 'lib/github.js'));

const { GitHub } = global;

const url = process.argv[2] || 'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4';
const quality = process.argv[3] || 'best';
const token = process.env.GITHUB_TOKEN || '';
const repo = process.env.GITHUB_REPO || '';
const workflow = process.env.GITHUB_WORKFLOW_FILE || 'downloader.yml';

if (!token || !repo) {
  console.error('צריך GITHUB_TOKEN ו-GITHUB_REPO');
  process.exit(2);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async function main() {
  const gh = new GitHub(token, repo);

  const state = await gh.workflowState(workflow);
  console.log(`Workflow "${workflow}": ${state}`);
  if (state !== 'active') {
    console.error('ה-Workflow לא פעיל במאגר – אין טעם להפעיל הרצה');
    process.exit(1);
  }

  const before = await gh.listReleases(1);
  const beforeTag = before.length ? before[0].tag : null;
  console.log(`Release אחרון לפני הבדיקה: ${beforeTag || '(אין)'}`);

  console.log(`מפעיל הרצה: url=${url} quality=${quality}`);
  await gh.dispatch(workflow, { url, quality, target: 'github', dry_run: false, max_messages: '0' });
  console.log('ההרצה נשלחה.');

  let run = null;
  const deadline = Date.now() + 11 * 60 * 1000;
  while (Date.now() < deadline) {
    await sleep(8000);
    const runs = await gh.listRuns(workflow, 3);
    if (!runs.length) {
      process.stdout.write('.');
      continue;
    }
    run = runs[0];
    process.stdout.write(`\n  #${run.number} ${run.status}${run.conclusion ? '/' + run.conclusion : ''}\n`);
    if (run.status === 'completed') break;
  }

  if (!run || run.status !== 'completed') {
    console.error('ההרצה לא הסתיימה בזמן');
    process.exit(1);
  }
  if (run.conclusion !== 'success') {
    console.error(`ההרצה נכשלה (${run.conclusion}): ${run.html_url}`);
    process.exit(1);
  }

  const releases = await gh.listReleases(3);
  const fresh = releases.find((r) => r.tag !== beforeTag && r.assets.length);
  if (!fresh) {
    console.error('לא נמצא Release חדש עם קובץ');
    process.exit(1);
  }
  console.log(`\nRelease חדש: ${fresh.tag} (${fresh.assets.length} קבצים)`);
  for (const asset of fresh.assets) {
    console.log(`  • ${asset.name} (${asset.size} בייט)`);
    console.log(`    ${asset.url}`);
  }
  console.log('E2E_OK');
})().catch((err) => {
  console.error('שגיאה:', err.message);
  process.exit(1);
});

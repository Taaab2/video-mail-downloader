/* לקוח GitHub API של התוסף. */
(function (root) {
  'use strict';

  const API = 'https://api.github.com';

  class GitHubError extends Error {}

  class GitHub {
    constructor(token, repo) {
      this.token = token || '';
      this.repo = repo || '';
    }

    async request(method, path, body, options) {
      const opts = options || {};
      const url = path.startsWith('http') ? path : API + path;
      const headers = {
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      };
      if (this.token) headers.Authorization = 'Bearer ' + this.token;
      if (body !== undefined) headers['Content-Type'] = 'application/json';

      let resp;
      try {
        resp = await fetch(url, {
          method,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
          redirect: 'follow',
        });
      } catch (err) {
        throw new GitHubError('אין חיבור ל-GitHub: ' + err.message);
      }
      if (opts.expect && !opts.expect.includes(resp.status)) {
        throw new GitHubError(await this.describeError(resp, method, path));
      }
      if (resp.status === 204) return null;
      const text = await resp.text();
      if (!text) return null;
      try {
        return JSON.parse(text);
      } catch (err) {
        return text;
      }
    }

    async describeError(resp, method, path) {
      let detail = '';
      try {
        const data = await resp.json();
        detail = data.message || '';
      } catch (err) {
        detail = '';
      }
      const map = {
        401: 'הטוקן לא תקין או שפג תוקפו',
        403: 'אין הרשאה לפעולה הזו (בדקו את הרשאות הטוקן)',
        404: 'לא נמצא – בדקו את שם המאגר וההרשאות',
        422: 'הנתונים לא התקבלו ב-GitHub',
      };
      const hint = map[resp.status] ? ' – ' + map[resp.status] : '';
      return `${method} ${path} נכשל (${resp.status})${hint}${detail ? ': ' + detail : ''}`;
    }

    requireRepo() {
      if (!this.token) throw new GitHubError('לא הוגדר טוקן GitHub');
      if (!this.repo || this.repo.indexOf('/') < 0) throw new GitHubError('לא הוגדר מאגר (owner/repo)');
    }

    // ---------------------------------------------------------------- //
    whoami() {
      return this.request('GET', '/user', undefined, { expect: [200] });
    }

    repoInfo() {
      return this.request('GET', `/repos/${this.repo}`, undefined, { expect: [200] });
    }

    async getFile(path) {
      const data = await this.request('GET', `/repos/${this.repo}/contents/${path}`, undefined, {
        expect: [200, 404],
      });
      if (!data || !data.content) return { text: null, sha: null };
      const clean = data.content.replace(/\s/g, '');
      const bytes = root.sealedbox.b64ToBytes(clean);
      return { text: new TextDecoder().decode(bytes), sha: data.sha };
    }

    async putFile(path, text, message, sha) {
      const body = { message, content: root.sealedbox.bytesToB64(new TextEncoder().encode(text)) };
      if (sha) body.sha = sha;
      return this.request('PUT', `/repos/${this.repo}/contents/${path}`, body, { expect: [200, 201] });
    }

    // ---------------------------------------------------------------- //
    async secretNames() {
      const data = await this.request('GET', `/repos/${this.repo}/actions/secrets`, undefined, {
        expect: [200, 404],
      });
      return data && data.secrets ? data.secrets.map((s) => s.name) : [];
    }

    async variableNames() {
      const data = await this.request('GET', `/repos/${this.repo}/actions/variables`, undefined, {
        expect: [200, 404],
      });
      return data && data.variables ? data.variables.map((v) => v.name) : [];
    }

    /** כותב Secret מוצפן (sealed box) – הטוקן צריך הרשאת Secrets: write. */
    async setSecret(name, value) {
      this.requireRepo();
      const key = await this.request('GET', `/repos/${this.repo}/actions/secrets/public-key`, undefined, {
        expect: [200],
      });
      const encrypted_value = root.sealedbox.sealToBase64(value, key.key);
      return this.request(
        'PUT',
        `/repos/${this.repo}/actions/secrets/${name}`,
        { encrypted_value, key_id: key.key_id },
        { expect: [201, 204] }
      );
    }

    async setVariable(name, value) {
      this.requireRepo();
      const list = await this.request('GET', `/repos/${this.repo}/actions/variables`, undefined, {
        expect: [200, 404],
      });
      const exists = list && list.variables ? list.variables.some((v) => v.name === name) : false;
      if (exists) {
        return this.request('PATCH', `/repos/${this.repo}/actions/variables/${name}`,
          { name, value }, { expect: [204] });
      }
      return this.request('POST', `/repos/${this.repo}/actions/variables`,
        { name, value }, { expect: [201, 204, 409] });
    }

    // ---------------------------------------------------------------- //
    async workflowState(workflow) {
      const data = await this.request('GET', `/repos/${this.repo}/actions/workflows/${workflow}`, undefined, {
        expect: [200, 404],
      });
      return data && data.state ? data.state : 'missing';
    }

    /** ענף ברירת המחדל של המאגר. GitHub דורש ref מפורש בהפעלת Workflow. */
    async defaultBranch() {
      if (this._defaultBranch) return this._defaultBranch;
      const info = await this.repoInfo();
      this._defaultBranch = info.default_branch || 'main';
      return this._defaultBranch;
    }

    async dispatch(workflow, inputs, ref) {
      this.requireRepo();
      const branch = ref || (await this.defaultBranch());
      return this.request(
        'POST',
        `/repos/${this.repo}/actions/workflows/${workflow}/dispatches`,
        { ref: branch, inputs: inputs || {} },
        { expect: [204] }
      );
    }

    async listRuns(workflow, limit) {
      const data = await this.request(
        'GET',
        `/repos/${this.repo}/actions/workflows/${workflow}/runs?per_page=${limit || 5}`,
        undefined,
        { expect: [200, 404] }
      );
      if (!data || !data.workflow_runs) return [];
      return data.workflow_runs.map((r) => ({
        id: r.id,
        number: r.run_number,
        status: r.status,
        conclusion: r.conclusion,
        event: r.event,
        created_at: r.created_at,
        html_url: r.html_url,
        title: r.display_title || '',
      }));
    }

    async runLogs(runId) {
      const url = `https://api.github.com/repos/${this.repo}/actions/runs/${runId}/logs`;
      const resp = await fetch(url, { headers: { Authorization: 'Bearer ' + this.token } });
      if (!resp.ok) throw new GitHubError('לא ניתן להוריד לוגים (' + resp.status + ')');
      const buf = new Uint8Array(await resp.arrayBuffer());
      // ה-zip מכיל טקסט דחוס; שולפים רק את ההודעה הכללית
      return buf.length + ' בייט (הורידו את הקובץ המלא מדף ההרצה ב-GitHub)';
    }

    // ---------------------------------------------------------------- //
    async listReleases(limit) {
      const data = await this.request('GET', `/repos/${this.repo}/releases?per_page=${limit || 5}`, undefined, {
        expect: [200, 404],
      });
      if (!data) return [];
      return data.map((rel) => ({
        tag: rel.tag_name,
        name: rel.name,
        created_at: rel.created_at,
        url: rel.html_url,
        assets: (rel.assets || []).map((a) => ({
          id: a.id,
          name: a.name,
          label: a.label || '',
          size: a.size,
          downloads: a.download_count,
          url: a.browser_download_url,
        })),
      }));
    }

    /** מחזיר Release בודד לפי תג (או null אם לא קיים). */
    async releaseByTag(tag) {
      const rel = await this.request(
        'GET',
        `/repos/${this.repo}/releases/tags/${encodeURIComponent(tag)}`,
        undefined,
        { expect: [200, 404] }
      );
      if (!rel || !rel.tag_name) return null;
      return {
        tag: rel.tag_name,
        name: rel.name,
        created_at: rel.created_at,
        url: rel.html_url,
        assets: (rel.assets || []).map((a) => ({
          id: a.id,
          name: a.name,
          label: a.label || '',
          size: a.size,
          downloads: a.download_count,
          url: a.browser_download_url,
        })),
      };
    }

    /** קורא את תוכן האסימון כטקסט (למשל רשימת סרטונים ב-JSON). */
    async fetchAssetText(assetId) {
      this.requireRepo();
      const url = `${API}/repos/${this.repo}/releases/assets/${assetId}`;
      let resp;
      try {
        resp = await fetch(url, {
          headers: { Authorization: 'Bearer ' + this.token, Accept: 'application/octet-stream' },
          redirect: 'follow',
        });
      } catch (err) {
        throw new GitHubError('אין חיבור ל-GitHub: ' + err.message);
      }
      if (!resp.ok) {
        throw new GitHubError(await this.describeError(resp, 'GET', `/releases/assets/${assetId}`));
      }
      return resp.text();
    }

    deleteRelease(tag) {
      return this.request('DELETE', `/repos/${this.repo}/releases/tags/${tag}`, undefined, {
        expect: [204, 404],
      });
    }

    /** הורדת קובץ Release דרך ה-API עם הטוקן – עובד גם במאגר פרטי. */
    async downloadAsset(assetId, name) {
      this.requireRepo();
      const url = `${API}/repos/${this.repo}/releases/assets/${assetId}`;
      let resp;
      try {
        resp = await fetch(url, {
          headers: { Authorization: 'Bearer ' + this.token, Accept: 'application/octet-stream' },
          redirect: 'follow',
        });
      } catch (err) {
        throw new GitHubError('אין חיבור ל-GitHub: ' + err.message);
      }
      if (!resp.ok) {
        throw new GitHubError(await this.describeError(resp, 'GET', `/releases/assets/${assetId}`));
      }
      const blob = await resp.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = name || 'download.zip';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 15000);
    }
  }

  root.GitHub = GitHub;
  root.GitHubError = GitHubError;
})(typeof self !== 'undefined' ? self : this);

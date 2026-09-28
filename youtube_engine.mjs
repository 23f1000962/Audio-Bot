import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import { Innertube, UniversalCache } from 'youtubei.js';

const HOST = '127.0.0.1';
const PORT = Number(process.env.YOUTUBE_ENGINE_PORT || 8765);
const DATA_DIR = process.env.YOUTUBE_ENGINE_DATA || '/tmp/audio-bot-youtube';
const TOKEN_FILE = path.join(DATA_DIR, 'oauth.json');
const MAX_BODY = 64 * 1024;

fs.mkdirSync(DATA_DIR, { recursive: true });

let yt = null;
let authPromise = null;
let authPending = null;
let authError = null;

function json(res, code, data) {
  const body = JSON.stringify(data);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store'
  });
  res.end(body);
}

async function body(req) {
  return await new Promise((resolve, reject) => {
    let data = '';
    req.on('data', chunk => {
      data += chunk;
      if (Buffer.byteLength(data) > MAX_BODY) {
        reject(new Error('request too large'));
        req.destroy();
      }
    });
    req.on('end', () => {
      try { resolve(data ? JSON.parse(data) : {}); }
      catch { reject(new Error('invalid JSON')); }
    });
    req.on('error', reject);
  });
}

function readTokens() {
  try {
    return JSON.parse(fs.readFileSync(TOKEN_FILE, 'utf8'));
  } catch { return null; }
}

function saveTokens(tokens) {
  const tmp = `${TOKEN_FILE}.${crypto.randomUUID()}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(tokens), { mode: 0o600 });
  fs.renameSync(tmp, TOKEN_FILE);
  try { fs.chmodSync(TOKEN_FILE, 0o600); } catch {}
}

function removeTokens() {
  try { fs.rmSync(TOKEN_FILE, { force: true }); } catch {}
}

async function getYouTube() {
  if (yt) return yt;
  const tokens = readTokens();
  yt = await Innertube.create({
    cache: new UniversalCache(true, DATA_DIR),
    retrieve_player: true,
    enable_session_cache: true,
    generate_session_locally: true,
    client_type: 'WEB',
    device_category: 'desktop',
    ...(tokens ? {} : {})
  });
  if (tokens) {
    await yt.signIn(tokens);
  }
  return yt;
}

function textValue(v) {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  if (typeof v.text === 'string') return v.text;
  if (Array.isArray(v.runs)) return v.runs.map(x => x.text || '').join('');
  return String(v);
}

function parseVideoId(input) {
  const s = String(input || '').trim();
  if (/^[A-Za-z0-9_-]{11}$/.test(s)) return s;
  try {
    const u = new URL(s);
    if (u.hostname === 'youtu.be') return u.pathname.slice(1).split('/')[0];
    if (u.searchParams.get('v')) return u.searchParams.get('v');
    const parts = u.pathname.split('/').filter(Boolean);
    const i = parts.indexOf('shorts');
    if (i >= 0 && parts[i + 1]) return parts[i + 1];
    const e = parts.indexOf('embed');
    if (e >= 0 && parts[e + 1]) return parts[e + 1];
  } catch {}
  return null;
}

async function startAuth() {
  if (readTokens()) return { status: 'active' };
  if (authPending) return { status: 'pending', ...authPending };
  authError = null;

  const instance = await getYouTube();
  authPromise = new Promise((resolve, reject) => {
    const onPending = data => {
      authPending = {
        verification_url: data.verification_url,
        user_code: data.user_code,
        expires_in: data.expires_in,
        interval: data.interval
      };
    };
    const onAuth = ({ credentials }) => {
      try { saveTokens(credentials); } catch (e) { console.error(e); }
      authPending = null;
      authError = null;
      resolve();
    };
    const onError = err => {
      authPending = null;
      authError = String(err?.message || err);
      reject(err);
    };
    instance.once('auth-pending', onPending);
    instance.once('auth', onAuth);
    instance.once('auth-error', onError);
    instance.signIn().catch(onError);
  }).catch(() => {});

  // Allow the device-code event to arrive before responding.
  await new Promise(r => setTimeout(r, 700));
  if (authPending) return { status: 'pending', ...authPending };
  if (readTokens()) return { status: 'active' };
  if (authError) return { status: 'error', message: authError };
  return { status: 'starting' };
}

async function authStatus() {
  if (readTokens()) return { status: 'active' };
  if (authPending) return { status: 'pending', ...authPending };
  if (authError) return { status: 'error', message: authError };
  return { status: 'not_configured' };
}

async function logout() {
  removeTokens();
  authPending = null;
  authError = null;
  yt = null;
  return { status: 'logged_out' };
}

async function search(query) {
  const instance = await getYouTube();
  const result = await instance.search(query);
  const videos = (result.videos || []).slice(0, 8).map(v => ({
    id: v.id,
    title: textValue(v.title),
    author: textValue(v.author),
    duration: textValue(v.duration),
    url: `https://www.youtube.com/watch?v=${v.id}`,
    thumbnail: v.thumbnails?.[0]?.url || ''
  }));
  return { results: videos };
}

async function download(input, outputPath) {
  const id = parseVideoId(input);
  if (!id) throw new Error('Could not determine a YouTube video ID.');
  const instance = await getYouTube();
  const info = await instance.getBasicInfo(id);
  const title = textValue(info.basic_info?.title) || id;
  const author = textValue(info.basic_info?.author) || '';
  const duration = Number(info.basic_info?.duration || 0) || null;
  const stream = await instance.download(id, {
    type: 'audio',
    quality: 'best',
    format: 'any'
  });
  await pipeline(Readable.fromWeb(stream), fs.createWriteStream(outputPath));
  return { id, title, artist: author, duration, path: outputPath };
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/health') return json(res, 200, { ok: true });
    if (req.method === 'POST' && req.url === '/auth/start') return json(res, 200, await startAuth());
    if (req.method === 'GET' && req.url === '/auth/status') return json(res, 200, await authStatus());
    if (req.method === 'POST' && req.url === '/auth/logout') return json(res, 200, await logout());
    if (req.method === 'POST' && req.url === '/search') {
      const b = await body(req); return json(res, 200, await search(b.query));
    }
    if (req.method === 'POST' && req.url === '/download') {
      const b = await body(req);
      if (!b.input || !b.output_path) return json(res, 400, { error: 'input and output_path are required' });
      return json(res, 200, await download(b.input, b.output_path));
    }
    return json(res, 404, { error: 'not found' });
  } catch (e) {
    console.error('[youtube-engine]', e);
    return json(res, 500, { error: String(e?.message || e) });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`YouTube.js engine listening on http://${HOST}:${PORT}`);
});

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import { Innertube, UniversalCache } from 'youtubei.js';

const HOST = '127.0.0.1';
const PORT = Number(process.env.YOUTUBE_ENGINE_PORT || 8765);
const DATA_DIR =
  process.env.YOUTUBE_ENGINE_DATA || '/tmp/audio-bot-youtube';

const TOKEN_FILE = path.join(DATA_DIR, 'oauth.json');
const MAX_BODY = 64 * 1024;

fs.mkdirSync(DATA_DIR, { recursive: true });

let yt = null;
let authClient = null;
let authPromise = null;
let authPending = null;
let authError = null;

function json(res, code, data) {
  const body = JSON.stringify(data);

  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });

  res.end(body);
}

async function body(req) {
  return await new Promise((resolve, reject) => {
    let data = '';

    req.on('data', (chunk) => {
      data += chunk;

      if (Buffer.byteLength(data) > MAX_BODY) {
        reject(new Error('request too large'));
        req.destroy();
      }
    });

    req.on('end', () => {
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch {
        reject(new Error('invalid JSON'));
      }
    });

    req.on('error', reject);
  });
}

function readTokens() {
  try {
    return JSON.parse(
      fs.readFileSync(TOKEN_FILE, 'utf8'),
    );
  } catch {
    return null;
  }
}

function saveTokens(tokens) {
  if (!tokens) return;

  const tmp =
    `${TOKEN_FILE}.${crypto.randomUUID()}.tmp`;

  fs.writeFileSync(
    tmp,
    JSON.stringify(tokens),
    { mode: 0o600 },
  );

  fs.renameSync(tmp, TOKEN_FILE);

  try {
    fs.chmodSync(TOKEN_FILE, 0o600);
  } catch {}
}

function removeTokens() {
  try {
    fs.rmSync(TOKEN_FILE, { force: true });
  } catch {}
}

/*
 * Normal YouTube client.
 *
 * If OAuth credentials exist, use the TV client because current YouTube.js
 * OAuth2 support is restricted to the TV client.
 *
 * When no OAuth credentials exist, use WEB for ordinary public requests.
 */
async function getYouTube() {
  if (yt) return yt;

  const tokens = readTokens();
  const clientType = tokens ? 'TV' : 'WEB';

  console.log(
    `[youtube-engine] Creating ${clientType} client...`,
  );

  yt = await Innertube.create({
    cache: new UniversalCache(true, DATA_DIR),
    retrieve_player: true,
    enable_session_cache: true,
    generate_session_locally: true,
    client_type: clientType,
    device_category: tokens ? 'tv' : 'desktop',
  });

  if (tokens) {
    console.log(
      '[youtube-engine] Restoring saved OAuth credentials...',
    );

    await yt.session.signIn(tokens);
  }

  return yt;
}

/*
 * OAuth gets its own lightweight TV client.
 *
 * This is important: /auth/start no longer waits for the normal WEB
 * downloader client/player initialization.
 */
async function getAuthClient() {
  if (authClient) return authClient;

  console.log(
    '[youtube-engine] Creating TV OAuth client...',
  );

  authClient = await Innertube.create({
    cache: new UniversalCache(true, DATA_DIR),
    retrieve_player: false,
    enable_session_cache: true,
    generate_session_locally: true,
    client_type: 'TV',
    device_category: 'tv',
  });

  return authClient;
}

function textValue(v) {
  if (v == null) return '';

  if (typeof v === 'string') return v;

  if (typeof v.text === 'string') {
    return v.text;
  }

  if (Array.isArray(v.runs)) {
    return v.runs
      .map((x) => x.text || '')
      .join('');
  }

  return String(v);
}

function parseVideoId(input) {
  const s = String(input || '').trim();

  if (/^[A-Za-z0-9_-]{11}$/.test(s)) {
    return s;
  }

  try {
    const u = new URL(s);

    if (u.hostname === 'youtu.be') {
      return u.pathname
        .slice(1)
        .split('/')[0];
    }

    if (u.searchParams.get('v')) {
      return u.searchParams.get('v');
    }

    const parts = u.pathname
      .split('/')
      .filter(Boolean);

    const shortsIndex = parts.indexOf('shorts');

    if (
      shortsIndex >= 0 &&
      parts[shortsIndex + 1]
    ) {
      return parts[shortsIndex + 1];
    }

    const embedIndex = parts.indexOf('embed');

    if (
      embedIndex >= 0 &&
      parts[embedIndex + 1]
    ) {
      return parts[embedIndex + 1];
    }
  } catch {}

  return null;
}

/*
 * Start the YouTube.js TV OAuth device flow.
 *
 * Important API detail:
 * OAuth events and signIn() belong to `instance.session`, not the
 * Innertube instance itself.
 */
async function startAuth() {
  if (readTokens()) {
    return { status: 'active' };
  }

  if (authPending) {
    return {
      status: 'pending',
      ...authPending,
    };
  }

  if (authPromise) {
    return {
      status: 'starting',
    };
  }

  authError = null;

  try {
    const instance = await getAuthClient();
    const session = instance.session;

    const onPending = (data) => {
      authPending = {
        verification_url: data.verification_url,
        user_code: data.user_code,
        expires_in: data.expires_in,
        interval: data.interval,
      };

      console.log(
        '[youtube-engine] OAuth device code received.',
      );
    };

    const onAuth = ({ credentials }) => {
      try {
        saveTokens(credentials);
        console.log(
          '[youtube-engine] OAuth credentials saved.',
        );
      } catch (error) {
        console.error(
          '[youtube-engine] Failed to save OAuth credentials:',
          error,
        );
      }

      authPending = null;
      authError = null;

      // The authenticated client will be recreated on the next request
      // so that it uses the saved TV OAuth session.
      yt = null;
    };

    const onUpdateCredentials = ({ credentials }) => {
      try {
        saveTokens(credentials);
      } catch (error) {
        console.error(
          '[youtube-engine] Failed to update OAuth credentials:',
          error,
        );
      }
    };

    const onError = (error) => {
      authPending = null;
      authError = String(
        error?.message || error,
      );

      console.error(
        '[youtube-engine] OAuth error:',
        authError,
      );
    };

    // Remove previous listeners from this auth client before attaching
    // fresh listeners.
    session.removeAllListeners('auth-pending');
    session.removeAllListeners('auth');
    session.removeAllListeners('update-credentials');
    session.removeAllListeners('auth-error');

    session.once('auth-pending', onPending);
    session.once('auth', onAuth);
    session.on(
      'update-credentials',
      onUpdateCredentials,
    );
    session.once('auth-error', onError);

    authPromise = session
      .signIn()
      .then(() => {
        console.log(
          '[youtube-engine] OAuth signIn() completed.',
        );
      })
      .catch((error) => {
        onError(error);
      })
      .finally(() => {
        authPromise = null;
      });

    /*
     * Give the OAuth client a few seconds to emit auth-pending.
     * We deliberately do NOT wait for the entire signIn() promise.
     */
    const deadline =
      Date.now() + 5000;

    while (
      Date.now() < deadline &&
      !authPending &&
      !readTokens() &&
      !authError
    ) {
      await new Promise((resolve) =>
        setTimeout(resolve, 100),
      );
    }

    if (readTokens()) {
      return { status: 'active' };
    }

    if (authPending) {
      return {
        status: 'pending',
        ...authPending,
      };
    }

    if (authError) {
      return {
        status: 'error',
        message: authError,
      };
    }

    return { status: 'starting' };
  } catch (error) {
    authPromise = null;
    authError = String(
      error?.message || error,
    );

    console.error(
      '[youtube-engine] Could not initialize OAuth:',
      authError,
    );

    return {
      status: 'error',
      message: authError,
    };
  }
}

async function authStatus() {
  if (readTokens()) {
    return { status: 'active' };
  }

  if (authPending) {
    return {
      status: 'pending',
      ...authPending,
    };
  }

  if (authPromise) {
    return { status: 'starting' };
  }

  if (authError) {
    return {
      status: 'error',
      message: authError,
    };
  }

  return {
    status: 'not_configured',
  };
}

async function logout() {
  try {
    if (authClient?.session) {
      try {
        await authClient.session.signOut();
      } catch (error) {
        console.warn(
          '[youtube-engine] OAuth revoke warning:',
          error?.message || error,
        );
      }
    }
  } finally {
    removeTokens();

    authPending = null;
    authError = null;
    authPromise = null;

    yt = null;
    authClient = null;
  }

  return { status: 'logged_out' };
}

async function search(query) {
  const instance = await getYouTube();

  const result = await instance.search(query);

  const videos =
    (result.videos || [])
      .slice(0, 8)
      .map((v) => ({
        id: v.id,
        title: textValue(v.title),
        author: textValue(v.author),
        duration: textValue(v.duration),
        url:
          `https://www.youtube.com/watch?v=${v.id}`,
        thumbnail:
          v.thumbnails?.[0]?.url || '',
      }));

  return { results: videos };
}

async function download(input, outputPath) {
  const id = parseVideoId(input);

  if (!id) {
    throw new Error(
      'Could not determine a YouTube video ID.',
    );
  }

  const instance = await getYouTube();

  const info =
    await instance.getBasicInfo(id);

  const title =
    textValue(info.basic_info?.title) || id;

  const author =
    textValue(info.basic_info?.author) || '';

  const duration =
    Number(
      info.basic_info?.duration || 0,
    ) || null;

  const stream =
    await instance.download(id, {
      type: 'audio',
      quality: 'best',
      format: 'any',
    });

  await pipeline(
    Readable.fromWeb(stream),
    fs.createWriteStream(outputPath),
  );

  return {
    id,
    title,
    artist: author,
    duration,
    path: outputPath,
  };
}

const server = http.createServer(
  async (req, res) => {
    try {
      if (
        req.method === 'GET' &&
        req.url === '/health'
      ) {
        return json(
          res,
          200,
          {
            ok: true,
            oauth: Boolean(readTokens()),
            auth_pending: Boolean(authPending),
          },
        );
      }

      if (
        req.method === 'POST' &&
        req.url === '/auth/start'
      ) {
        return json(
          res,
          200,
          await startAuth(),
        );
      }

      if (
        req.method === 'GET' &&
        req.url === '/auth/status'
      ) {
        return json(
          res,
          200,
          await authStatus(),
        );
      }

      if (
        req.method === 'POST' &&
        req.url === '/auth/logout'
      ) {
        return json(
          res,
          200,
          await logout(),
        );
      }

      if (
        req.method === 'POST' &&
        req.url === '/search'
      ) {
        const b = await body(req);

        return json(
          res,
          200,
          await search(b.query),
        );
      }

      if (
        req.method === 'POST' &&
        req.url === '/download'
      ) {
        const b = await body(req);

        if (
          !b.input ||
          !b.output_path
        ) {
          return json(
            res,
            400,
            {
              error:
                'input and output_path are required',
            },
          );
        }

        return json(
          res,
          200,
          await download(
            b.input,
            b.output_path,
          ),
        );
      }

      return json(
        res,
        404,
        { error: 'not found' },
      );
    } catch (error) {
      console.error(
        '[youtube-engine]',
        error,
      );

      return json(
        res,
        500,
        {
          error: String(
            error?.message || error,
          ),
        },
      );
    }
  },
);

server.listen(
  PORT,
  HOST,
  () => {
    console.log(
      `YouTube.js engine listening on ` +
      `http://${HOST}:${PORT}`,
    );
  },
);

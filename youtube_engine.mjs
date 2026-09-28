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

/*
|--------------------------------------------------------------------------
| Client state
|--------------------------------------------------------------------------
*/

let yt = null;
let webClient = null;

let authClient = null;
let authPromise = null;
let authPending = null;
let authError = null;

/*
|--------------------------------------------------------------------------
| HTTP helpers
|--------------------------------------------------------------------------
*/

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

/*
|--------------------------------------------------------------------------
| OAuth token storage
|--------------------------------------------------------------------------
*/

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
    {
      mode: 0o600,
    },
  );

  fs.renameSync(tmp, TOKEN_FILE);

  try {
    fs.chmodSync(TOKEN_FILE, 0o600);
  } catch {}
}

function removeTokens() {
  try {
    fs.rmSync(TOKEN_FILE, {
      force: true,
    });
  } catch {}
}

/*
|--------------------------------------------------------------------------
| Utility
|--------------------------------------------------------------------------
*/

function textValue(v) {
  if (v == null) return '';

  if (typeof v === 'string') {
    return v;
  }

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

function is403(error) {
  const message = String(
    error?.message ||
    error ||
    '',
  ).toLowerCase();

  return (
    message.includes('status code 403') ||
    message.includes('status: 403') ||
    message.includes('403 forbidden') ||
    message.includes('http 403') ||
    message.includes('code": 403')
  );
}

function isPlayerError(error) {
  const message = String(
    error?.message ||
    error ||
    '',
  ).toLowerCase();

  return (
    message.includes('player') ||
    message.includes('decipher') ||
    message.includes('signature') ||
    message.includes('n parameter')
  );
}

/*
|--------------------------------------------------------------------------
| YouTube client creation
|--------------------------------------------------------------------------
*/

/*
 * Authenticated TV client.
 *
 * OAuth2 is currently supported by YouTube.js through the TV client.
 */
async function createAuthenticatedClient() {
  const tokens = readTokens();

  if (!tokens) {
    throw new Error(
      'No saved YouTube OAuth credentials.',
    );
  }

  console.log(
    '[youtube-engine] Creating authenticated TV client...',
  );

  const instance = await Innertube.create({
    cache: new UniversalCache(
      false,
      DATA_DIR,
    ),

    retrieve_player: true,

    enable_session_cache: false,

    generate_session_locally: true,

    client_type: 'TV',

    device_category: 'tv',
  });

  console.log(
    '[youtube-engine] Restoring OAuth credentials...',
  );

  await instance.session.signIn(tokens);

  return instance;
}

/*
 * Public WEB client.
 *
 * This is deliberately separate from the OAuth TV client.
 *
 * A public YouTube video does not necessarily need OAuth, and using a
 * separate client gives us a second route if the authenticated TV player
 * returns a 403.
 *
 * If YOUTUBE_COOKIE is configured, it will also be used here.
 */
async function createWebClient() {
  console.log(
    '[youtube-engine] Creating fresh WEB client...',
  );

  const options = {
    cache: new UniversalCache(
      false,
      DATA_DIR,
    ),

    retrieve_player: true,

    enable_session_cache: false,

    generate_session_locally: true,

    client_type: 'WEB',

    device_category: 'desktop',
  };

  const cookie = process.env.YOUTUBE_COOKIE;

  if (cookie) {
    console.log(
      '[youtube-engine] Using YOUTUBE_COOKIE for WEB client.',
    );

    options.cookie = cookie;
  }

  return await Innertube.create(options);
}

/*
 * Return the main YouTube client.
 *
 * If OAuth exists, authenticated TV is preferred.
 */
async function getYouTube() {
  if (yt) {
    return yt;
  }

  if (readTokens()) {
    try {
      yt = await createAuthenticatedClient();

      return yt;
    } catch (error) {
      console.error(
        '[youtube-engine] Failed to create authenticated TV client:',
        error?.message || error,
      );

      /*
       * Do not permanently block downloading if OAuth is broken.
       * Public WEB fallback will be attempted by download().
       */
      yt = null;
    }
  }

  /*
   * No OAuth or OAuth client failed.
   *
   * Use the public WEB client.
   */
  if (!webClient) {
    webClient = await createWebClient();
  }

  return webClient;
}

/*
 * Force a completely fresh client.
 *
 * This is important when YouTube returns a 403 caused by a bad player
 * configuration or stale player state.
 */
async function resetClient(type) {
  if (type === 'tv') {
    yt = null;
  }

  if (type === 'web') {
    webClient = null;
  }
}

/*
|--------------------------------------------------------------------------
| Video ID parser
|--------------------------------------------------------------------------
*/

function parseVideoId(input) {
  const s = String(
    input || '',
  ).trim();

  if (
    /^[A-Za-z0-9_-]{11}$/.test(s)
  ) {
    return s;
  }

  try {
    const u = new URL(s);

    if (
      u.hostname === 'youtu.be' ||
      u.hostname === 'www.youtu.be'
    ) {
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

    const shortsIndex =
      parts.indexOf('shorts');

    if (
      shortsIndex >= 0 &&
      parts[shortsIndex + 1]
    ) {
      return parts[shortsIndex + 1];
    }

    const embedIndex =
      parts.indexOf('embed');

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
|--------------------------------------------------------------------------
| OAuth
|--------------------------------------------------------------------------
*/

async function getAuthClient() {
  if (authClient) {
    return authClient;
  }

  console.log(
    '[youtube-engine] Creating TV OAuth client...',
  );

  authClient = await Innertube.create({
    cache: new UniversalCache(
      false,
      DATA_DIR,
    ),

    /*
     * Do not initialize the player just to start OAuth.
     */
    retrieve_player: false,

    enable_session_cache: false,

    generate_session_locally: true,

    client_type: 'TV',

    device_category: 'tv',
  });

  return authClient;
}

async function startAuth() {
  if (readTokens()) {
    return {
      status: 'active',
    };
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
    const instance =
      await getAuthClient();

    const session =
      instance.session;

    const onPending = (data) => {
      authPending = {
        verification_url:
          data.verification_url,

        user_code:
          data.user_code,

        expires_in:
          data.expires_in,

        interval:
          data.interval,
      };

      console.log(
        '[youtube-engine] OAuth device code received.',
      );

      console.log(
        `[youtube-engine] Verification URL: ${data.verification_url}`,
      );

      console.log(
        `[youtube-engine] User code: ${data.user_code}`,
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

      /*
       * Force the next download to create a fresh authenticated
       * TV client with the new credentials.
       */
      yt = null;
    };

    const onUpdateCredentials = ({
      credentials,
    }) => {
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
        error?.message ||
        error,
      );

      console.error(
        '[youtube-engine] OAuth error:',
        authError,
      );
    };

    /*
     * Clean up listeners if supported.
     */
    if (
      typeof session.removeAllListeners ===
      'function'
    ) {
      session.removeAllListeners(
        'auth-pending',
      );

      session.removeAllListeners(
        'auth',
      );

      session.removeAllListeners(
        'update-credentials',
      );

      session.removeAllListeners(
        'auth-error',
      );
    }

    session.once(
      'auth-pending',
      onPending,
    );

    session.once(
      'auth',
      onAuth,
    );

    session.on(
      'update-credentials',
      onUpdateCredentials,
    );

    session.once(
      'auth-error',
      onError,
    );

    /*
     * Start device OAuth.
     *
     * IMPORTANT:
     * We do not wait for the entire signIn() promise because it waits
     * for the user to complete authentication.
     */
    authPromise =
      session
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
     * Wait only for the device code to appear.
     */
    const deadline =
      Date.now() + 10000;

    while (
      Date.now() < deadline &&
      !authPending &&
      !readTokens() &&
      !authError
    ) {
      await new Promise(
        (resolve) =>
          setTimeout(resolve, 100),
      );
    }

    if (readTokens()) {
      return {
        status: 'active',
      };
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

    return {
      status: 'starting',
    };
  } catch (error) {
    authPromise = null;

    authError = String(
      error?.message ||
      error,
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
    return {
      status: 'active',
    };
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
    if (
      authClient &&
      authClient.session
    ) {
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
    webClient = null;
    authClient = null;
  }

  return {
    status: 'logged_out',
  };
}

/*
|--------------------------------------------------------------------------
| Search
|--------------------------------------------------------------------------
*/

async function search(query) {
  let instance;

  try {
    instance =
      await getYouTube();

    const result =
      await instance.search(query);

    const videos =
      (result.videos || [])
        .slice(0, 8)
        .map((v) => ({
          id: v.id,

          title:
            textValue(v.title),

          author:
            textValue(v.author),

          duration:
            textValue(v.duration),

          url:
            `https://www.youtube.com/watch?v=${v.id}`,

          thumbnail:
            v.thumbnails?.[0]?.url || '',
        }));

    return {
      results: videos,
    };
  } catch (error) {
    console.error(
      '[youtube-engine] Search failed:',
      error?.message || error,
    );

    /*
     * If authenticated client failed, try fresh WEB.
     */
    if (instance === yt) {
      await resetClient('tv');

      const fallback =
        await createWebClient();

      webClient = fallback;

      const result =
        await fallback.search(query);

      const videos =
        (result.videos || [])
          .slice(0, 8)
          .map((v) => ({
            id: v.id,

            title:
              textValue(v.title),

            author:
              textValue(v.author),

            duration:
              textValue(v.duration),

            url:
              `https://www.youtube.com/watch?v=${v.id}`,

            thumbnail:
              v.thumbnails?.[0]?.url || '',
          }));

      return {
        results: videos,
      };
    }

    throw error;
  }
}

/*
|--------------------------------------------------------------------------
| Download helpers
|--------------------------------------------------------------------------
*/

async function performDownload(
  instance,
  id,
  outputPath,
  clientName,
) {
  console.log(
    `[youtube-engine] Download attempt using ${clientName} client for ${id}`,
  );

  const info =
    await instance.getBasicInfo(id);

  const title =
    textValue(
      info.basic_info?.title,
    ) || id;

  const author =
    textValue(
      info.basic_info?.author,
    ) || '';

  const duration =
    Number(
      info.basic_info?.duration || 0,
    ) || null;

  console.log(
    `[youtube-engine] Video: ${title}`,
  );

  console.log(
    `[youtube-engine] Requesting best audio stream...`,
  );

  const stream =
    await instance.download(
      id,
      {
        type: 'audio',
        quality: 'best',
        format: 'any',
      },
    );

  if (!stream) {
    throw new Error(
      'YouTube returned an empty audio stream.',
    );
  }

  await pipeline(
    Readable.fromWeb(stream),
    fs.createWriteStream(
      outputPath,
    ),
  );

  console.log(
    `[youtube-engine] Download completed using ${clientName} client.`,
  );

  return {
    id,
    title,
    artist: author,
    duration,
    path: outputPath,
    client: clientName,
  };
}

/*
|--------------------------------------------------------------------------
| Main download
|--------------------------------------------------------------------------
*/

async function download(
  input,
  outputPath,
) {
  const id =
    parseVideoId(input);

  if (!id) {
    throw new Error(
      'Could not determine a YouTube video ID.',
    );
  }

  console.log(
    `[youtube-engine] Download requested: ${id}`,
  );

  /*
   * ---------------------------------------------------------------
   * ATTEMPT 1
   * ---------------------------------------------------------------
   *
   * Use OAuth TV client when available.
   */
  if (readTokens()) {
    try {
      const instance =
        await getYouTube();

      return await performDownload(
        instance,
        id,
        outputPath,
        'TV OAuth',
      );
    } catch (error) {
      console.error(
        '[youtube-engine] TV OAuth download failed:',
        error?.message || error,
      );

      /*
       * A 403/player problem is exactly where we want to try a
       * completely fresh client.
       */
      if (
        is403(error) ||
        isPlayerError(error)
      ) {
        console.warn(
          '[youtube-engine] TV client returned a 403/player error. Resetting TV client...',
        );

        await resetClient('tv');
      } else {
        /*
         * Even non-403 failures can sometimes be transient.
         */
        await resetClient('tv');
      }
    }
  }

  /*
   * ---------------------------------------------------------------
   * ATTEMPT 2
   * ---------------------------------------------------------------
   *
   * Fresh WEB client.
   *
   * This is especially useful for public YouTube videos where OAuth
   * is not actually required.
   */
  try {
    const instance =
      await createWebClient();

    webClient = instance;

    return await performDownload(
      instance,
      id,
      outputPath,
      'WEB fallback',
    );
  } catch (error) {
    console.error(
      '[youtube-engine] WEB fallback failed:',
      error?.message || error,
    );

    await resetClient('web');

    /*
     * -------------------------------------------------------------
     * ATTEMPT 3
     * -------------------------------------------------------------
     *
     * One more completely fresh WEB client.
     *
     * This is intentionally limited to one retry so a broken YouTube
     * player does not create an endless request loop.
     */
    try {
      console.warn(
        '[youtube-engine] Retrying with a second fresh WEB client...',
      );

      const retryClient =
        await createWebClient();

      return await performDownload(
        retryClient,
        id,
        outputPath,
        'WEB retry',
      );
    } catch (retryError) {
      console.error(
        '[youtube-engine] WEB retry failed:',
        retryError?.message || retryError,
      );

      /*
       * Return a useful error to the Python bot.
       */
      const originalMessage =
        String(
          error?.message ||
          error ||
          'Unknown error',
        );

      const retryMessage =
        String(
          retryError?.message ||
          retryError ||
          'Unknown retry error',
        );

      throw new Error(
        `YouTube download failed. ` +
        `First error: ${originalMessage} ` +
        `| Retry error: ${retryMessage}`,
      );
    }
  }
}

/*
|--------------------------------------------------------------------------
| HTTP server
|--------------------------------------------------------------------------
*/

const server =
  http.createServer(
    async (req, res) => {
      try {
        /*
         * -----------------------------------------------------------
         * HEALTH
         * -----------------------------------------------------------
         */
        if (
          req.method === 'GET' &&
          req.url === '/health'
        ) {
          return json(
            res,
            200,
            {
              ok: true,

              oauth:
                Boolean(
                  readTokens(),
                ),

              auth_pending:
                Boolean(
                  authPending,
                ),

              tv_client:
                Boolean(yt),

              web_client:
                Boolean(webClient),
            },
          );
        }

        /*
         * -----------------------------------------------------------
         * AUTH START
         * -----------------------------------------------------------
         */
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

        /*
         * -----------------------------------------------------------
         * AUTH STATUS
         * -----------------------------------------------------------
         */
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

        /*
         * -----------------------------------------------------------
         * LOGOUT
         * -----------------------------------------------------------
         */
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

        /*
         * -----------------------------------------------------------
         * SEARCH
         * -----------------------------------------------------------
         */
        if (
          req.method === 'POST' &&
          req.url === '/search'
        ) {
          const b =
            await body(req);

          if (!b.query) {
            return json(
              res,
              400,
              {
                error:
                  'query is required',
              },
            );
          }

          return json(
            res,
            200,
            await search(
              b.query,
            ),
          );
        }

        /*
         * -----------------------------------------------------------
         * DOWNLOAD
         * -----------------------------------------------------------
         */
        if (
          req.method === 'POST' &&
          req.url === '/download'
        ) {
          const b =
            await body(req);

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

        /*
         * -----------------------------------------------------------
         * NOT FOUND
         * -----------------------------------------------------------
         */
        return json(
          res,
          404,
          {
            error:
              'not found',
          },
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
            error:
              String(
                error?.message ||
                error,
              ),
          },
        );
      }
    },
  );

/*
|--------------------------------------------------------------------------
| Start server
|--------------------------------------------------------------------------
*/

server.listen(
  PORT,
  HOST,
  () => {
    console.log(
      '==================================================',
    );

    console.log(
      `[youtube-engine] Listening on http://${HOST}:${PORT}`,
    );

    console.log(
      `[youtube-engine] Data directory: ${DATA_DIR}`,
    );

    console.log(
      `[youtube-engine] OAuth token file: ${TOKEN_FILE}`,
    );

    console.log(
      '[youtube-engine] Multi-client download fallback enabled.',
    );

    console.log(
      '==================================================',
    );
  },
);
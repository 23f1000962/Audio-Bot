import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import { Innertube, UniversalCache, ClientType } from 'youtubei.js';

const HOST = '127.0.0.1';
const PORT = Number(process.env.YOUTUBE_ENGINE_PORT || 8765);
const DATA_DIR = process.env.YOUTUBE_ENGINE_DATA || '/tmp/audio-bot-youtube';
const TOKEN_FILE = path.join(DATA_DIR, 'oauth.json');
const COOKIE_FILE = process.env.YOUTUBE_COOKIE_FILE || '/app/cookies.txt';
const MAX_BODY = 64 * 1024;
fs.mkdirSync(DATA_DIR, { recursive: true });

let authClient = null, authPromise = null, authPending = null, authError = null;

function json(res, code, data) { const body = JSON.stringify(data); res.writeHead(code, {'Content-Type':'application/json; charset=utf-8','Content-Length':Buffer.byteLength(body),'Cache-Control':'no-store'}); res.end(body); }
async function body(req) { return await new Promise((resolve,reject)=>{ let data=''; req.on('data',c=>{data+=c;if(Buffer.byteLength(data)>MAX_BODY){reject(new Error('request too large'));req.destroy();}}); req.on('end',()=>{try{resolve(data?JSON.parse(data):{});}catch{reject(new Error('invalid JSON'));}}); req.on('error',reject); }); }
function readTokens(){try{return JSON.parse(fs.readFileSync(TOKEN_FILE,'utf8'));}catch{return null;}}
function saveTokens(tokens){if(!tokens)return;const tmp=`${TOKEN_FILE}.${crypto.randomUUID()}.tmp`;fs.writeFileSync(tmp,JSON.stringify(tokens),{mode:0o600});fs.renameSync(tmp,TOKEN_FILE);try{fs.chmodSync(TOKEN_FILE,0o600);}catch{}}
function removeTokens(){try{fs.rmSync(TOKEN_FILE,{force:true});}catch{}}
function textValue(v){if(v==null)return '';if(typeof v==='string')return v;if(typeof v.text==='string')return v.text;if(Array.isArray(v.runs))return v.runs.map(x=>x.text||'').join('');return String(v);}
function errorText(e){return String(e?.message||e||'');}
function is403(e){const m=errorText(e).toLowerCase();return m.includes('status code 403')||m.includes('status: 403')||m.includes('403 forbidden')||m.includes('http 403');}
function isPlayerError(e){const m=errorText(e).toLowerCase();return ['player','decipher','signature','n parameter','playability'].some(x=>m.includes(x));}

function readCookieHeader(){
  const files=[process.env.YOUTUBE_COOKIE_FILE,COOKIE_FILE,path.join(process.cwd(),'cookies.txt'),'/app/cookies.txt'].filter(Boolean);
  for(const file of [...new Set(files)]){
    try{
      if(!fs.existsSync(file)) continue;
      const raw=fs.readFileSync(file,'utf8'); const jar=new Map();
      for(const line of raw.split(/\r?\n/)){
        if(!line || (line.startsWith('#') && !line.startsWith('#HttpOnly_'))) continue;
        const f=line.split('\t'); if(f.length<7) continue;
        const name=f[5]?.trim(), value=f.slice(6).join('\t').trim();
        if(name && value) jar.set(name,value);
      }
      if(jar.size){console.log(`[youtube-engine] Loaded ${jar.size} cookies from ${file}`);return [...jar].map(([k,v])=>`${k}=${v}`).join('; ');}
    }catch(e){console.warn(`[youtube-engine] Cookie read failed: ${file}: ${errorText(e)}`);}
  }
  const env=process.env.YOUTUBE_COOKIE||process.env.YOUTUBE_COOKIES;
  if(env){console.log('[youtube-engine] Using YOUTUBE_COOKIE from environment.');return env;}
  return '';
}

function common(){return {cache:new UniversalCache(false,DATA_DIR),retrieve_player:true,enable_session_cache:false,generate_session_locally:true};}
async function createWebCookie(){const cookie=readCookieHeader();if(!cookie)throw new Error('No YouTube cookies found. Put cookies.txt at /app/cookies.txt or set YOUTUBE_COOKIE.');return Innertube.create({...common(),client_type:ClientType.WEB,device_category:'desktop',cookie});}
async function createEmbedded(){const cookie=readCookieHeader();return Innertube.create({...common(),client_type:ClientType.WEB_EMBEDDED,device_category:'desktop',...(cookie?{cookie}:{})});}
async function createAndroid(){const cookie=readCookieHeader();return Innertube.create({...common(),client_type:ClientType.ANDROID,device_category:'mobile',...(cookie?{cookie}:{})});}
async function createTV(){const tokens=readTokens();if(!tokens)throw new Error('No saved YouTube OAuth credentials.');const instance=await Innertube.create({...common(),client_type:ClientType.TV,device_category:'tv'});await instance.session.signIn(tokens);return instance;}
function parseVideoId(input){const s=String(input||'').trim();if(/^[A-Za-z0-9_-]{11}$/.test(s))return s;try{const u=new URL(s);if(['youtu.be','www.youtu.be'].includes(u.hostname))return u.pathname.slice(1).split('/')[0];if(u.searchParams.get('v'))return u.searchParams.get('v');const p=u.pathname.split('/').filter(Boolean);for(const type of ['shorts','embed']){const i=p.indexOf(type);if(i>=0&&p[i+1])return p[i+1];}}catch{}return null;}

async function getAuthClient(){if(authClient)return authClient;authClient=await Innertube.create({cache:new UniversalCache(false,DATA_DIR),retrieve_player:false,enable_session_cache:false,generate_session_locally:true,client_type:ClientType.TV,device_category:'tv'});return authClient;}
async function startAuth(){if(readTokens())return{status:'active'};if(authPending)return{status:'pending',...authPending};if(authPromise)return{status:'starting'};authError=null;try{const session=(await getAuthClient()).session;const onPending=d=>{authPending={verification_url:d.verification_url,user_code:d.user_code,expires_in:d.expires_in,interval:d.interval};console.log('[youtube-engine] OAuth device code received.');};const onAuth=({credentials})=>{saveTokens(credentials);authPending=null;authError=null;console.log('[youtube-engine] OAuth credentials saved.');};const onUpdate=({credentials})=>saveTokens(credentials);const onError=e=>{authPending=null;authError=errorText(e);console.error('[youtube-engine] OAuth error:',authError);};if(session.removeAllListeners){for(const x of ['auth-pending','auth','update-credentials','auth-error'])session.removeAllListeners(x);}session.once('auth-pending',onPending);session.once('auth',onAuth);session.on('update-credentials',onUpdate);session.once('auth-error',onError);authPromise=session.signIn().catch(onError).finally(()=>{authPromise=null;});const deadline=Date.now()+10000;while(Date.now()<deadline&&!authPending&&!readTokens()&&!authError)await new Promise(r=>setTimeout(r,100));if(readTokens())return{status:'active'};if(authPending)return{status:'pending',...authPending};if(authError)return{status:'error',message:authError};return{status:'starting'};}catch(e){authPromise=null;authError=errorText(e);return{status:'error',message:authError};}}
async function authStatus(){if(readTokens())return{status:'active'};if(authPending)return{status:'pending',...authPending};if(authPromise)return{status:'starting'};if(authError)return{status:'error',message:authError};return{status:'not_configured'};}
async function logout(){try{if(authClient?.session){try{await authClient.session.signOut();}catch{}}}finally{removeTokens();authPending=null;authError=null;authPromise=null;authClient=null;}return{status:'logged_out'};}

async function search(query){const attempts=[['WEB+cookies',createWebCookie],['WEB_EMBEDDED',createEmbedded],['ANDROID',createAndroid]];let last=null;for(const [name,factory] of attempts){try{const r=await (await factory()).search(query);return{results:(r.videos||[]).slice(0,8).map(v=>({id:v.id,title:textValue(v.title),author:textValue(v.author),duration:textValue(v.duration),url:`https://www.youtube.com/watch?v=${v.id}`,thumbnail:v.thumbnails?.[0]?.url||''}))};}catch(e){last=e;console.error(`[youtube-engine] Search ${name} failed:`,errorText(e));}}throw last||new Error('YouTube search failed.');}

async function performDownload(instance,id,outputPath,label){console.log(`[youtube-engine] ${label}: ${id}`);const info=await instance.getBasicInfo(id);const title=textValue(info.basic_info?.title)||id;const artist=textValue(info.basic_info?.author)||'';const duration=Number(info.basic_info?.duration||0)||null;const stream=await instance.download(id,{type:'audio',quality:'best',format:'any'});if(!stream)throw new Error('YouTube returned an empty audio stream.');await pipeline(Readable.fromWeb(stream),fs.createWriteStream(outputPath));return{id,title,artist,duration,path:outputPath,client:label};}

async function download(input,outputPath){const id=parseVideoId(input);if(!id)throw new Error('Could not determine a YouTube video ID.');console.log(`[youtube-engine] Download requested: ${id}`);const attempts=[];if(readCookieHeader())attempts.push(['WEB + cookies',createWebCookie]);if(readTokens())attempts.push(['TV OAuth',createTV]);attempts.push(['WEB_EMBEDDED',createEmbedded],['ANDROID',createAndroid]);const errors=[];for(const [label,factory] of attempts){try{return await performDownload(await factory(),id,outputPath,label);}catch(e){const msg=errorText(e);errors.push(`${label}: ${msg}`);console.error(`[youtube-engine] ${label} failed:`,msg);if(is403(e))console.warn(`[youtube-engine] ${label} returned HTTP 403; trying next client.`);else if(isPlayerError(e))console.warn(`[youtube-engine] ${label} returned a player/decipher error; trying next client.`);}}throw new Error(`YouTube download failed after ${attempts.length} client attempts.\n${errors.join('\n')}`);}

const server=http.createServer(async(req,res)=>{try{if(req.method==='GET'&&req.url==='/health')return json(res,200,{ok:true,oauth:Boolean(readTokens()),cookies:Boolean(readCookieHeader()),auth_pending:Boolean(authPending)});if(req.method==='POST'&&req.url==='/auth/start')return json(res,200,await startAuth());if(req.method==='GET'&&req.url==='/auth/status')return json(res,200,await authStatus());if(req.method==='POST'&&req.url==='/auth/logout')return json(res,200,await logout());if(req.method==='POST'&&req.url==='/search'){const b=await body(req);if(!b.query)return json(res,400,{error:'query is required'});return json(res,200,await search(b.query));}if(req.method==='POST'&&req.url==='/download'){const b=await body(req);if(!b.input||!b.output_path)return json(res,400,{error:'input and output_path are required'});return json(res,200,await download(b.input,b.output_path));}return json(res,404,{error:'not found'});}catch(e){console.error('[youtube-engine]',e);return json(res,500,{error:errorText(e)});}});
server.listen(PORT,HOST,()=>{console.log('==================================================');console.log(`[youtube-engine] Listening on http://${HOST}:${PORT}`);console.log(`[youtube-engine] Cookie file: ${COOKIE_FILE}`);console.log('[youtube-engine] Cookie/client fallback enabled.');console.log('==================================================');});

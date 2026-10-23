"""Content worker for n8n: turns a script JSON into a finished video (or pin image).

n8n does the planning (trends, script, captions, uploads); this service does the heavy,
free, local work: images (Gemini free tier, Pollinations fallback), Kokoro voice,
ffmpeg assembly, automatic QC. One render at a time (CPU bound).

Env:
  WORKER_TOKEN       required header X-Worker-Token on every POST
  GEMINI_API_KEY     Google AI Studio key (free tier)
  IMAGE_MODEL        default gemini-2.5-flash-image
  QC_MODEL           default gemini-2.5-flash
  POLLINATIONS_TOKEN optional, raises Pollinations rate limit
  PUBLIC_BASE_URL    public https URL where /files is reachable (Instagram/Facebook/Pinterest fetch media from it)
  DATA_DIR           default /data
"""
import base64, glob, json, os, re, secrets, shutil, subprocess, threading, time, urllib.parse, urllib.request, urllib.error

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = f'{HERE}/scripts'
FONTS = f'{HERE}/fonts'
DATA = os.environ.get('DATA_DIR', '/data')
PUBLIC = os.environ.get('PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')
TOKEN = os.environ.get('WORKER_TOKEN', '')
GKEY = os.environ.get('GEMINI_API_KEY', '')
IMAGE_MODEL = os.environ.get('IMAGE_MODEL', 'gemini-2.5-flash-image')
QC_MODEL = os.environ.get('QC_MODEL', 'gemini-2.5-flash')
POLL_TOKEN = os.environ.get('POLLINATIONS_TOKEN', '')
os.makedirs(f'{DATA}/files', exist_ok=True)

app = FastAPI(title='content-worker')
app.mount('/files', StaticFiles(directory=f'{DATA}/files'), name='files')
LOCK = threading.Lock()
SIZES = {'9:16': (1080, 1920), '16:9': (2560, 1440), '2:3': (1000, 1500), '1:1': (1080, 1080), '4:5': (1080, 1350)}


ERRORS = []  # non-fatal problems, reported back to n8n


class CreditError(Exception):
    pass


def auth(tok):
    if not TOKEN or tok != TOKEN:
        raise HTTPException(401, 'bad X-Worker-Token')


def run(cmd, timeout=3600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f'{os.path.basename(cmd[1] if cmd[0].startswith("python") else cmd[0])}: {r.stderr[-1500:]}')
    return r.stdout


def gemini(model, body, timeout=300):
    req = urllib.request.Request(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
        json.dumps(body).encode(), {'Content-Type': 'application/json', 'x-goog-api-key': GKEY})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        msg = e.read().decode()[:400]
        if e.code in (402, 403, 429):
            raise CreditError(f'gemini {e.code}: {msg}')
        raise RuntimeError(f'gemini {e.code}: {msg}')


def gemini_image(prompt, aspect, out):
    body = {'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'responseModalities': ['IMAGE'], 'imageConfig': {'aspectRatio': aspect}}}
    data = gemini(IMAGE_MODEL, body)
    for p in data['candidates'][0]['content'].get('parts', []):
        if 'inlineData' in p:
            open(out, 'wb').write(base64.b64decode(p['inlineData']['data']))
            return 'gemini'
    raise RuntimeError('gemini returned no image')


def pollinations_image(prompt, aspect, out, seed):
    w, h = SIZES[aspect]
    # Pollinations caps the long side; ffmpeg upscales with lanczos later
    f = 1440 / max(w, h)
    q = urllib.parse.urlencode({'width': int(w * f) // 8 * 8, 'height': int(h * f) // 8 * 8, 'model': 'flux',
                                'nologo': 'true', 'private': 'true', 'enhance': 'false', 'seed': seed})
    url = f'https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt[:1500])}?{q}'
    hdr = {'User-Agent': 'content-worker/1.0'}
    if POLL_TOKEN:
        hdr['Authorization'] = f'Bearer {POLL_TOKEN}'
    for attempt in range(4):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=180).read()
            if len(raw) > 20000:
                open(out, 'wb').write(raw)
                return 'pollinations'
        except Exception:
            pass
        time.sleep(15 * (attempt + 1))
    raise RuntimeError('pollinations failed')


def make_image(prompt, aspect, out, seed=0):
    """Gemini first (best quality); on quota/credit errors fall back to Pollinations FLUX (free)."""
    full = prompt + ', no text, no letters, no numbers, no logos, no watermark'
    if GKEY:
        try:
            return gemini_image(full, aspect, out)
        except Exception as e:  # quota or safety block: fall back, never pay
            print('gemini image fallback:', e)
            ERRORS.append(f'gemini image: {str(e)[:300]}')
    src = pollinations_image(full, aspect, out, seed)
    # Pollinations stamps a small mark in the bottom corner on anonymous calls: crop it away
    im = Image.open(out).convert('RGB')
    im.crop((0, 0, im.width, int(im.height * 0.9))).save(out, quality=95)
    return src


def vision_qc(imgs, rules):
    """Ask Gemini (free tier) which scene images break the rules. Returns list of bad scene numbers."""
    if not GKEY:
        return []
    parts = [{'text': 'You are a strict video QC reviewer. For each numbered image, reply with JSON '
                      '{"bad":[{"n":<scene number>,"why":"..."}]}. Mark an image bad if it has: visible text or '
                      f'letters, logos, watermarks, real recognisable faces, broken hands or extra limbs, wrong subject. {rules}'}]
    for n, p in imgs:
        im = Image.open(p).convert('RGB'); im.thumbnail((384, 384))
        tmp = p + '.qc.jpg'; im.save(tmp, quality=80)
        parts += [{'text': f'Image scene {n}:'},
                  {'inlineData': {'mimeType': 'image/jpeg', 'data': base64.b64encode(open(tmp, 'rb').read()).decode()}}]
        os.remove(tmp)
    try:
        data = gemini(QC_MODEL, {'contents': [{'parts': parts}],
                                 'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0}})
        txt = data['candidates'][0]['content']['parts'][0]['text']
        return [int(b['n']) for b in json.loads(txt).get('bad', [])]
    except Exception as e:
        print('vision qc skipped:', e)
        ERRORS.append(f'vision qc: {str(e)[:300]}')
        return []


def font(size, weight='Black'):
    return ImageFont.truetype(f'{FONTS}/InterDisplay-{weight}.otf', size)


def wrap(draw, text, fnt, width):
    lines, cur = [], ''
    for w in text.split():
        t = (cur + ' ' + w).strip()
        if draw.textlength(t, font=fnt) <= width:
            cur = t
        else:
            lines.append(cur); cur = w
    return lines + [cur] if cur else lines


def text_card(src, out, size, headline, sub='', accent=(255, 214, 0)):
    """Image + bold headline overlay: thumbnails and Pinterest pins."""
    W, H = size
    im = Image.open(src).convert('RGB')
    s = max(W / im.width, H / im.height)
    im = im.resize((int(im.width * s + 1), int(im.height * s + 1)), Image.LANCZOS)
    im = im.crop(((im.width - W) // 2, (im.height - H) // 2, (im.width - W) // 2 + W, (im.height - H) // 2 + H))
    d = ImageDraw.Draw(im)
    fs = int(W * (0.11 if W > H else 0.095))
    f1 = font(fs)
    lines = wrap(d, headline.upper(), f1, W * 0.86)
    while len(lines) > 3 and fs > 40:
        fs -= 6; f1 = font(fs); lines = wrap(d, headline.upper(), f1, W * 0.86)
    lh = int(fs * 1.08)
    top = int(H * (0.07 if H > W else 0.08))
    shade = Image.new('L', (W, H), 0)
    ImageDraw.Draw(shade).rectangle([0, 0, W, top + lh * len(lines) + int(fs * 0.9)], fill=150)
    im.paste((0, 0, 0), mask=shade.filter(ImageFilter.GaussianBlur(40)))
    d = ImageDraw.Draw(im)
    y = top
    for i, ln in enumerate(lines):
        x = (W - d.textlength(ln, font=f1)) / 2
        d.text((x, y), ln, font=f1, fill=accent if i == len(lines) - 1 else (255, 255, 255),
               stroke_width=max(3, fs // 14), stroke_fill=(0, 0, 0))
        y += lh
    if sub:
        f2 = font(int(fs * 0.42), 'Bold')
        for ln in wrap(d, sub, f2, W * 0.84)[:3]:
            d.text(((W - d.textlength(ln, font=f2)) / 2, H - int(H * 0.14)), ln, font=f2, fill=(255, 255, 255),
                   stroke_width=3, stroke_fill=(0, 0, 0))
            break
    im.save(out, quality=92)


def job_dir(vid):
    safe = re.sub(r'[^A-Za-z0-9_-]', '', vid)[:60] or 'job'
    rel = f'{time.strftime("%Y%m%d")}/{safe}_{secrets.token_hex(4)}'
    d = f'{DATA}/files/{rel}'
    os.makedirs(d, exist_ok=True)
    return d, rel


def qc_probe(video):
    out = run(['bash', f'{SCRIPTS}/qc_video.sh', video, video.replace('.mp4', '_sheet.jpg')])
    dur = float(re.search(r'dur=([\d.]+)', out).group(1))
    black = int(re.search(r'black: (\d+)', out).group(1))
    silence = int(re.search(r'silence: (\d+)', out).group(1))
    m = re.search(r'Input Integrated:\s*(-?[\d.]+)', out)
    return {'duration': dur, 'black_frames': black, 'silences': silence,
            'lufs': float(m.group(1)) if m else None, 'raw': out.strip()}


def produce(req, kind):
    script = req['script']
    vid = req.get('video_id') or script.get('video_id', 'video')
    aspect = '9:16' if kind == 'short' else '16:9'
    voice = req.get('voice') or ('am_fenrir' if kind == 'short' else 'am_michael')
    style = script.get('style', '')
    d, rel = job_dir(vid)
    assets = f'{d}/assets'; os.makedirs(assets, exist_ok=True)
    sp = f'{d}/script.json'; json.dump(script, open(sp, 'w'), indent=1)
    log = {'images': {}}

    def gen(s, seed=0):
        p = f'{assets}/img_{s["n"]:02d}.jpg'
        log['images'][s['n']] = make_image(f'{s["img"]}, {style}', aspect, p, seed)
        return p

    for s in script['scenes']:
        gen(s)
    bad = vision_qc([(s['n'], f'{assets}/img_{s["n"]:02d}.jpg') for s in script['scenes']], req.get('qc_rules', ''))
    for s in script['scenes']:
        if s['n'] in bad:
            gen(s, seed=7)
    log['qc_regenerated'] = bad
    log['warnings'] = ERRORS

    run(['python3', f'{SCRIPTS}/tts_kokoro.py', sp, assets, voice, str(req.get('speed', 1.0))])
    if kind == 'short':
        run(['python3', f'{SCRIPTS}/assemble_short.py', sp, assets, assets, d, vid])
    else:
        run(['python3', f'{SCRIPTS}/assemble_longform_chunked.py', sp, assets, d, vid])
    video = f'{d}/{vid}_Final.mp4'
    qc = qc_probe(video)
    lim = (35, 62) if kind == 'short' else (300, 1500)
    qc['pass'] = lim[0] <= qc['duration'] <= lim[1] and qc['black_frames'] == 0 and qc['silences'] <= (0 if kind == 'short' else 3)

    thumb_prompt = req.get('thumbnail_prompt') or script['scenes'][0]['img']
    th_src = f'{assets}/thumb_src.jpg'
    if kind == 'long':
        make_image(f'{thumb_prompt}, {style}, bold simple composition, high contrast', '16:9', th_src, 3)
        text_card(th_src, f'{d}/thumbnail.jpg', (1280, 720), req.get('thumbnail_text') or script['title'][:28])
    else:
        text_card(f'{assets}/img_01.jpg', f'{d}/thumbnail.jpg', (1080, 1920),
                  req.get('thumbnail_text') or script['scenes'][0].get('hook') or script['title'][:24])
    # 2:3 cover for Pinterest from the strongest image
    text_card(f'{assets}/img_01.jpg', f'{d}/pin.jpg', (1000, 1500), req.get('pin_text') or script['title'][:60])
    shutil.rmtree(f'{d}/_tmp_{vid}', ignore_errors=True); shutil.rmtree(f'{d}/_tmp', ignore_errors=True)

    base = f'{PUBLIC}/files/{rel}'
    return {'ok': True, 'video_id': vid, 'dir': rel, 'qc': qc, 'log': log,
            'video_url': f'{base}/{vid}_Final.mp4', 'srt_url': f'{base}/{vid}_Captions.srt',
            'thumbnail_url': f'{base}/thumbnail.jpg', 'pin_url': f'{base}/pin.jpg',
            'sheet_url': f'{base}/{vid}_Final_sheet.jpg',
            'internal_video_path': video}


def guarded(fn, *a):
    if not LOCK.acquire(timeout=3600):
        raise HTTPException(503, 'worker busy')
    try:
        return fn(*a)
    except CreditError as e:
        raise HTTPException(402, f'BLOCKED: credits ({e})')
    except Exception as e:
        raise HTTPException(500, str(e)[:2000])
    finally:
        LOCK.release()


@app.get('/health')
def health():
    return {'ok': True, 'gemini': bool(GKEY), 'busy': LOCK.locked(), 'public': PUBLIC}


@app.post('/render/short')
def render_short(req: dict, x_worker_token: str = Header('')):
    auth(x_worker_token)
    return guarded(produce, req, 'short')


@app.post('/render/long')
def render_long(req: dict, x_worker_token: str = Header('')):
    auth(x_worker_token)
    return guarded(produce, req, 'long')


@app.post('/image')
def image(req: dict, x_worker_token: str = Header('')):
    """Stand-alone social image: {id, prompt, aspect (2:3|1:1|4:5|9:16), headline?, sub?} -> public URL."""
    auth(x_worker_token)

    def go():
        d, rel = job_dir(req.get('id', 'img'))
        src = f'{d}/src.jpg'
        make_image(req['prompt'], req.get('aspect', '2:3'), src, req.get('seed', 0))
        out = f'{d}/image.jpg'
        if req.get('headline'):
            text_card(src, out, SIZES[req.get('aspect', '2:3')], req['headline'], req.get('sub', ''))
        else:
            shutil.copy(src, out)
        return {'ok': True, 'url': f'{PUBLIC}/files/{rel}/image.jpg'}
    return guarded(go)


@app.post('/cleanup')
def cleanup(req: dict, x_worker_token: str = Header('')):
    """Delete render folders older than N days (default 7) so the disk never fills."""
    auth(x_worker_token)
    keep = time.time() - 86400 * int(req.get('days', 7))
    gone = 0
    for p in glob.glob(f'{DATA}/files/*'):
        if os.path.getmtime(p) < keep:
            shutil.rmtree(p, ignore_errors=True); gone += 1
    return {'ok': True, 'removed_days': gone}

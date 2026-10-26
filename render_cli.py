#!/usr/bin/env python3
"""GitHub Actions entry point: render one job, publish the files as a GitHub release, call n8n back.

Usage: render_cli.py JOB_JSON_FILE
Job: {"kind": "short"|"long"|"image", "tag": "...", ...request fields of app.py}
Env: GITHUB_REPOSITORY, GH_TOKEN (for gh release), CALLBACK_URL (n8n Wait-node resume URL), GEMINI_API_KEY.
Always POSTs a result to CALLBACK_URL, also on failure, so the n8n run never hangs.
"""
import json, os, re, subprocess, sys, time, traceback, urllib.request

os.environ.setdefault('WORKER_TOKEN', 'actions')
os.environ.setdefault('DATA_DIR', os.path.abspath('out'))
import app  # noqa: E402

repo = os.environ['GITHUB_REPOSITORY']
cb = os.environ.get('CALLBACK_URL', '')


def callback(obj):
    print(json.dumps(obj)[:3000])
    if not cb:
        return
    req = urllib.request.Request(cb, json.dumps(obj).encode(), {'Content-Type': 'application/json'})
    for i in range(5):
        try:
            urllib.request.urlopen(req, timeout=60).read()
            return
        except Exception as e:
            print('callback retry', i, e); time.sleep(10 * (i + 1))


def publish(tag, files):
    """Upload files to release TAG and return {local path: public download URL}."""
    subprocess.run(['gh', 'release', 'create', tag, '--repo', repo, '--title', tag, '--notes', 'auto render',
                    '--latest=false', *files], check=True)
    base = os.environ.get('RELEASE_BASE', f'https://github.com/{repo}/releases/download')
    return {f: f'{base}/{tag}/{os.path.basename(f)}' for f in files}


def main():
    job = json.load(open(sys.argv[1]))
    kind = job.get('kind', 'short')
    tag = re.sub(r'[^A-Za-z0-9._-]', '-', job.get('tag') or f'{kind}-{int(time.time())}')
    try:
        if kind == 'image':
            d, rel = app.job_dir(job.get('id', 'img'))
            src = f'{d}/src.jpg'
            app.make_image(job['prompt'], job.get('aspect', '2:3'), src, job.get('seed', 0))
            out = f'{d}/{tag}.jpg'
            if job.get('headline'):
                app.text_card(src, out, app.SIZES[job.get('aspect', '2:3')], job['headline'], job.get('sub', ''))
            else:
                os.rename(src, out)
            lg = f'{d}/{tag}_log.json'; json.dump({'warnings': app.ERRORS}, open(lg, 'w'))
            urls = publish(tag, [out, lg])
            callback({'ok': True, 'url': urls[out], 'warnings': app.ERRORS})
            return
        res = app.produce(job, kind)
        d = f"{app.DATA}/files/{res['dir']}"
        vid = res['video_id']
        names = {'video_url': f'{d}/{vid}_Final.mp4', 'srt_url': f'{d}/{vid}_Captions.srt',
                 'thumbnail_url': f'{d}/thumbnail.jpg', 'pin_url': f'{d}/pin.jpg', 'sheet_url': f'{d}/{vid}_Final_sheet.jpg'}
        # unique asset names so a release never mixes files
        for k in ('thumbnail_url', 'pin_url'):
            nn = f'{d}/{vid}_{k.split("_")[0]}.jpg'; os.rename(names[k], nn); names[k] = nn
        json.dump(res.get('log', {}), open(f'{d}/{vid}_log.json', 'w'), indent=1)
        urls = publish(tag, list(names.values()) + [f'{d}/{vid}_log.json'])
        res.update({k: urls[v] for k, v in names.items()})
        res.pop('internal_video_path', None)
        res['release'] = f'https://github.com/{repo}/releases/tag/{tag}'
        callback(res)
    except app.CreditError as e:
        callback({'ok': False, 'blocked': 'credits', 'error': app.redact(e)[:1500], 'qc': {'pass': False, 'raw': app.redact(e)[:500]}})
        sys.exit(1)
    except Exception as e:
        traceback.print_exc()
        callback({'ok': False, 'error': app.redact(e)[:1500], 'qc': {'pass': False, 'raw': app.redact(e)[:500]}})
        sys.exit(1)


if __name__ == '__main__':
    main()

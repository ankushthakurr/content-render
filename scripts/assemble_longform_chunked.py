#!/usr/bin/env python3
"""Assemble a narrated long-form video from per-scene images + voiceover.

Usage: assemble_longform.py SCRIPT_JSON ASSET_DIR OUT_DIR VIDEO_ID
ASSET_DIR holds img_NN.(png|jpg) and vo_NN.mp3 for each scene n.
Outputs: VIDEO_ID_Final.mp4 (2560x1440, 30fps), VIDEO_ID_Captions.srt
"""
import json, os, re, subprocess, sys, glob

W, H, FPS = 2560, 1440, 30
XF = 0.5          # crossfade seconds
PAD_BEFORE = 0.3  # silence before line within a scene
PAD_AFTER = 0.45  # breath after line

script, adir, odir, vid = sys.argv[1:5]
os.makedirs(odir, exist_ok=True)
tmp = os.path.join(odir, '_tmp'); os.makedirs(tmp, exist_ok=True)
D = json.load(open(script))
scenes = D['scenes']

def dur(p):
    return float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', p]))

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr[-3000:])

# 1. per-scene clips with slow camera motion
motions = ['in', 'right', 'out', 'left']
starts, clips, vo_d = [], [], []
t = 0.0
for i, s in enumerate(scenes):
    n = s['n']
    img = (glob.glob(f'{adir}/img_{n:02d}.*') or [None])[0]
    vo = f'{adir}/vo_{n:02d}.mp3'
    d = dur(vo); vo_d.append(d)
    L = PAD_BEFORE + d + PAD_AFTER + (XF if i < len(scenes) - 1 else 0.8)
    frames = int(round(L * FPS))
    m = motions[i % 4]
    # upscale source to 2x output width so zoompan motion stays smooth
    if m == 'in':
        z, x, y = "'1+0.08*on/{f}'".format(f=frames), "'iw/2-(iw/zoom/2)'", "'ih/2-(ih/zoom/2)'"
    elif m == 'out':
        z, x, y = "'1.08-0.08*on/{f}'".format(f=frames), "'iw/2-(iw/zoom/2)'", "'ih/2-(ih/zoom/2)'"
    elif m == 'right':
        z, x, y = "'1.08'", "'(iw-iw/zoom)*on/{f}'".format(f=frames), "'ih/2-(ih/zoom/2)'"
    else:
        z, x, y = "'1.08'", "'(iw-iw/zoom)*(1-on/{f})'".format(f=frames), "'ih/2-(ih/zoom/2)'"
    out = f'{tmp}/clip_{n:02d}.mp4'
    vf = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
          f"zoompan=z={z}:x={x}:y={y}:d={frames}:s={W}x{H}:fps={FPS},"
          f"eq=saturation=1.03,format=yuv420p")
    if not (os.path.exists(out) and os.path.getsize(out) > 0): run(['ffmpeg', '-y', '-loglevel', 'error', '-loop', '1', '-i', img, '-vf', vf, '-frames:v', str(frames),
         '-c:v', 'libx264', '-preset', 'medium', '-crf', '14', out])
    clips.append((out, L)); starts.append(t)
    t += L - XF
total = t + XF - (0 if len(scenes) == 1 else 0)
total = starts[-1] + clips[-1][1]

# 2. crossfade chain, in chunks to limit memory
def xchain(items, outp, crf):
    inputs, fc, prev, acc = [], [], '0:v', items[0][1]
    for c, L in items: inputs += ['-i', c]
    for i in range(1, len(items)):
        off = acc - XF
        fc.append(f'[{prev}][{i}:v]xfade=transition=fade:duration={XF}:offset={off:.3f}[v{i}]')
        prev = f'v{i}'; acc = off + items[i][1]
    try:
        if abs(dur(outp) - acc) < 0.2: return acc
    except Exception: pass
    run(['ffmpeg', '-y', '-loglevel', 'error', *inputs, '-filter_complex', ';'.join(fc), '-map', f'[{prev}]',
         '-c:v', 'libx264', '-preset', 'slow', '-crf', str(crf), '-pix_fmt', 'yuv420p', '-r', str(FPS), outp])
    return acc
CH = 8
segs = []
for k in range(0, len(clips), CH):
    part = clips[k:k+CH]; sp = f'{tmp}/seg_{k//CH:02d}.mp4'
    if len(part) == 1:
        segs.append((part[0][0], part[0][1])); continue
    acc = xchain(part, sp, 12)
    segs.append((sp, acc)); print('seg', k//CH, round(acc, 2), flush=True)
video = f'{tmp}/video.mp4'
xchain(segs, video, 16)
# 3. audio: VO placed per scene, soft music pad, paper-whoosh at transitions
ai, af, mix = [], [], []
for i, s in enumerate(scenes):
    ai += ['-i', f'{adir}/vo_{s["n"]:02d}.mp3']
    ms = int((starts[i] + PAD_BEFORE) * 1000)
    af.append(f'[{i}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{i}]')
    mix.append(f'[a{i}]')
k = len(scenes)
# music bed: slow 4-chord pad (Am F C G) built from sines, lowpassed, very quiet
chords = [(220.0, 261.63, 329.63), (174.61, 220.0, 261.63), (130.81, 164.81, 196.0), (196.0, 246.94, 293.66)]
seg = 8.0
expr_parts = []
for ci, ch in enumerate(chords):
    tone = '+'.join(f'sin(2*PI*{f}*t)+0.5*sin(2*PI*{f*2}*t)' for f in ch)
    expr_parts.append(f'(between(mod(t,{seg*4}),{ci*seg},{(ci+1)*seg})*({tone}))')
expr = '0.05*(' + '+'.join(expr_parts) + ')*(0.75+0.25*sin(2*PI*0.25*t))'
ai += ['-f', 'lavfi', '-i', f"aevalsrc='{expr}':s=48000:d={total:.2f}"]
af.append(f'[{k}:a]lowpass=f=900,aecho=0.8:0.7:60:0.3,afade=t=in:d=2,afade=t=out:st={total-3:.2f}:d=3,aformat=channel_layouts=stereo,volume=0.55[mus]')
# whooshes
ai += ['-f', 'lavfi', '-i', f'anoisesrc=c=pink:r=48000:d={total:.2f}:a=0.5']
gates = '+'.join(f'between(t,{st-0.25:.2f},{st+0.25:.2f})*sin(PI*(t-{st-0.25:.2f})/0.5)' for st in starts[1:])
af.append(f"[{k+1}:a]bandpass=f=2500:w=1800,volume='0.10*({gates})':eval=frame,aformat=channel_layouts=stereo[sfx]")
af.append(''.join(mix) + f'amix=inputs={k}:normalize=0:duration=longest[vo]')
af.append('[vo]asplit=2[vo1][vosc]')
af.append('[mus][vosc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[duck]')
af.append('[vo1][duck][sfx]amix=inputs=3:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=9[aout]')
audio = f'{tmp}/audio.wav'
run(['ffmpeg', '-y', '-loglevel', 'error', *ai, '-filter_complex', ';'.join(af), '-map', '[aout]',
     '-t', f'{total:.2f}', '-ar', '48000', audio])

# 4. mux
final = f'{odir}/{vid}_Final.mp4'
run(['ffmpeg', '-y', '-loglevel', 'error', '-i', video, '-i', audio, '-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k',
     '-movflags', '+faststart', '-shortest', final])

# 5. captions: split each line into sentences, time by character share
def ts(x):
    h = int(x // 3600); m = int(x % 3600 // 60); s = x % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')
srt, idx = [], 1
for i, s in enumerate(scenes):
    parts = [p for p in re.split(r'(?<=[.?!])\s+', s['text']) if p]
    chunks = []
    for p in parts:  # keep caption lines short
        words = p.split()
        while len(words) > 14:
            chunks.append(' '.join(words[:12])); words = words[12:]
        chunks.append(' '.join(words))
    tot = sum(len(c) for c in chunks); cur = starts[i] + PAD_BEFORE
    for c in chunks:
        d = vo_d[i] * len(c) / tot
        srt.append(f'{idx}\n{ts(cur)} --> {ts(cur + d)}\n{c}\n'); idx += 1; cur += d
open(f'{odir}/{vid}_Captions.srt', 'w').write('\n'.join(srt))

# chapters helper output
json.dump({'starts': starts, 'total': total}, open(f'{tmp}/timeline.json', 'w'))
print(f'OK {final} {total:.1f}s')

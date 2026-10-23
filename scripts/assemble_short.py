#!/usr/bin/env python3
"""Assemble a narrated vertical Short (9:16) from per-scene images + voiceover.

Usage: assemble_short.py SCRIPT_JSON IMG_DIR VO_DIR OUT_DIR VIDEO_ID
IMG_DIR holds img_NN.(png|jpg), VO_DIR holds vo_NN.mp3, one per scene n.
Script scenes may carry "hook": short ALL-CAPS text shown at the top for the first 2 s.
Outputs: VIDEO_ID_Final.mp4 (1080x1920, 30fps, burned word-group captions),
         VIDEO_ID_Captions.srt
"""
import glob, json, os, re, subprocess, sys

W, H, FPS = 1080, 1920, 30
PAD_BEFORE = 0.05  # Shorts: no dead air at the start of a line
PAD_AFTER = 0.18
FONT = 'Inter Display'

script, idir, vdir, odir, vid = sys.argv[1:6]
os.makedirs(odir, exist_ok=True)
tmp = os.path.join(odir, f'_tmp_{vid}'); os.makedirs(tmp, exist_ok=True)
D = json.load(open(script))
scenes = D['scenes']


def dur(p):
    return float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', p]))


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr[-3000:])


# 1. per-scene clips: hard cuts, constant gentle motion (alternating push-in / drift)
starts, clips, vo_d = [], [], []
t = 0.0
for i, s in enumerate(scenes):
    n = s['n']
    img = (glob.glob(f'{idir}/img_{n:02d}.*') or [None])[0]
    d = dur(f'{vdir}/vo_{n:02d}.mp3'); vo_d.append(d)
    L = PAD_BEFORE + d + PAD_AFTER + (0.6 if i == len(scenes) - 1 else 0)
    f = int(round(L * FPS))
    m = i % 3
    if m == 0:
        z, x, y = f"'1+0.12*on/{f}'", "'iw/2-(iw/zoom/2)'", "'ih/2-(ih/zoom/2)'"
    elif m == 1:
        z, x, y = "'1.10'", f"'(iw-iw/zoom)*on/{f}'", "'ih/2-(ih/zoom/2)'"
    else:
        z, x, y = f"'1.12-0.10*on/{f}'", "'iw/2-(iw/zoom/2)'", f"'(ih-ih/zoom)*(0.6-0.2*on/{f})'"
    out = f'{tmp}/clip_{n:02d}.mp4'
    vf = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
          f"zoompan=z={z}:x={x}:y={y}:d={f}:s={W}x{H}:fps={FPS},"
          f"eq=saturation=1.06:contrast=1.03,unsharp=5:5:0.4,format=yuv420p")
    run(['ffmpeg', '-y', '-loglevel', 'error', '-loop', '1', '-i', img, '-vf', vf, '-frames:v', str(f),
         '-c:v', 'libx264', '-preset', 'medium', '-crf', '14', out])
    clips.append((out, f / FPS)); starts.append(t)
    t += f / FPS
total = t

# 2. captions: 1-3 word groups, timed by character share within each scene's VO
def ts(x, ass=False):
    h = int(x // 3600); m = int(x % 3600 // 60); s = x % 60
    return f'{h:d}:{m:02d}:{s:05.2f}' if ass else f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')

groups = []  # (start, end, text)
srt, idx = [], 1
for i, s in enumerate(scenes):
    words = s['text'].split()
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) == 3 or re.search(r'[.?!,]$', w):
            chunks.append(' '.join(cur)); cur = []
    if cur:
        chunks.append(' '.join(cur))
    tot = sum(len(c) + 2 for c in chunks); c0 = starts[i] + PAD_BEFORE
    for c in chunks:
        d = vo_d[i] * (len(c) + 2) / tot
        groups.append((c0, c0 + d, c)); c0 += d
    # SRT keeps whole sentences
    sent = [p for p in re.split(r'(?<=[.?!])\s+', s['text']) if p]
    tot2 = sum(len(p) for p in sent); c1 = starts[i] + PAD_BEFORE
    for p in sent:
        d = vo_d[i] * len(p) / tot2
        srt.append(f'{idx}\n{ts(c1)} --> {ts(c1 + d)}\n{p}\n'); idx += 1; c1 += d
open(f'{odir}/{vid}_Captions.srt', 'w').write('\n'.join(srt))

ass = [
    '[Script Info]', 'ScriptType: v4.00+', f'PlayResX: {W}', f'PlayResY: {H}', '',
    '[V4+ Styles]',
    'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
    f'Style: Cap,{FONT},96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,80,80,620,1',
    f'Style: Hook,{FONT},104,&H0000E5FF,&H0000E5FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,3,8,60,60,260,1',
    '', '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
]
for a, b, c in groups:
    c = c.replace('{', '').replace('}', '').upper()
    ass.append(f'Dialogue: 0,{ts(a, 1)},{ts(b, 1)},Cap,,0,0,0,,{{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}}{c}')
hook = scenes[0].get('hook')
if hook:
    ass.append(f'Dialogue: 1,{ts(0, 1)},{ts(min(2.2, total), 1)},Hook,,0,0,0,,{{\\fad(0,200)}}{hook}')
assf = f'{tmp}/caps.ass'
open(assf, 'w').write('\n'.join(ass))

# 3. concat (hard cuts) + burn captions
lst = f'{tmp}/list.txt'
open(lst, 'w').write(''.join(f"file '{os.path.abspath(c)}'\n" for c, _ in clips))
video = f'{tmp}/video.mp4'
run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', lst,
     '-vf', f"ass={assf}", '-c:v', 'libx264', '-preset', 'slow', '-crf', '16', '-pix_fmt', 'yuv420p', '-r', str(FPS), video])

# 4. audio: VO per scene, light pulsing music bed, whoosh on each cut, ducking, -14 LUFS
ai, af, mix = [], [], []
for i, s in enumerate(scenes):
    ai += ['-i', f'{vdir}/vo_{s["n"]:02d}.mp3']
    ms = int((starts[i] + PAD_BEFORE) * 1000)
    af.append(f'[{i}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{i}]')
    mix.append(f'[a{i}]')
k = len(scenes)
chords = [(220.0, 261.63, 329.63), (174.61, 220.0, 261.63), (130.81, 164.81, 196.0), (196.0, 246.94, 293.66)]
seg = 4.0
parts = []
for ci, ch in enumerate(chords):
    tone = '+'.join(f'sin(2*PI*{fq}*t)+0.4*sin(2*PI*{fq*2}*t)' for fq in ch)
    parts.append(f'(between(mod(t,{seg*4}),{ci*seg},{(ci+1)*seg})*({tone}))')
expr = '0.05*(' + '+'.join(parts) + ')*(0.6+0.4*abs(sin(2*PI*1.0*t)))'
ai += ['-f', 'lavfi', '-i', f"aevalsrc='{expr}':s=48000:d={total:.2f}"]
af.append(f'[{k}:a]lowpass=f=1400,aecho=0.8:0.6:45:0.25,afade=t=in:d=0.5,afade=t=out:st={max(total-1.5,0):.2f}:d=1.5,aformat=channel_layouts=stereo,volume=0.6[mus]')
ai += ['-f', 'lavfi', '-i', f'anoisesrc=c=pink:r=48000:d={total:.2f}:a=0.5']
gates = '+'.join(f'between(t,{st-0.18:.2f},{st+0.18:.2f})*sin(PI*(t-{st-0.18:.2f})/0.36)' for st in starts[1:]) or '0'
af.append(f"[{k+1}:a]bandpass=f=3000:w=2200,volume='0.12*({gates})':eval=frame,aformat=channel_layouts=stereo[sfx]")
af.append(''.join(mix) + f'amix=inputs={k}:normalize=0:duration=longest[vo]')
af.append('[vo]asplit=2[vo1][vosc]')
af.append('[mus][vosc]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=300[duck]')
af.append('[vo1][duck][sfx]amix=inputs=3:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=9[aout]')
audio = f'{tmp}/audio.wav'
run(['ffmpeg', '-y', '-loglevel', 'error', *ai, '-filter_complex', ';'.join(af), '-map', '[aout]',
     '-t', f'{total:.2f}', '-ar', '48000', audio])

final = f'{odir}/{vid}_Final.mp4'
run(['ffmpeg', '-y', '-loglevel', 'error', '-i', video, '-i', audio, '-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k',
     '-movflags', '+faststart', '-shortest', final])
print(f'OK {final} {total:.1f}s')

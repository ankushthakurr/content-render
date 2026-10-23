#!/usr/bin/env python3
"""Free, commercial-use-OK narration with Kokoro-82M (Apache 2.0), run locally on CPU.

Usage: tts_kokoro.py SCRIPT_JSON OUT_DIR [VOICE] [SPEED]
Writes OUT_DIR/vo_NN.mp3 for every scene. Default voice am_fenrir (user's pick), speed 1.0.
Needs: pip install kokoro-onnx soundfile; model files in ./kokoro_model/ next to this script.
"""
import json, os, subprocess, sys, tempfile

import soundfile as sf
from kokoro_onnx import Kokoro

here = os.path.dirname(os.path.abspath(__file__))
script, out = sys.argv[1:3]
voice = sys.argv[3] if len(sys.argv) > 3 else 'am_fenrir'
speed = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
os.makedirs(out, exist_ok=True)
k = Kokoro(f'{here}/kokoro_model/kokoro-v1.0.onnx', f'{here}/kokoro_model/voices-v1.0.bin')
for s in json.load(open(script))['scenes']:
    samples, sr = k.create(s['text'], voice=voice, speed=speed, lang='en-us')
    with tempfile.NamedTemporaryFile(suffix='.wav') as w:
        sf.write(w.name, samples, sr)
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', w.name, '-ar', '44100', '-b:a', '192k',
                        f"{out}/vo_{s['n']:02d}.mp3"], check=True)
    print('ok', s['n'])

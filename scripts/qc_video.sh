#!/usr/bin/env bash
# Usage: qc_video.sh VIDEO.mp4 SHEET.jpg [N_TILES]
# Prints resolution/fps/duration, black frames, silences, loudness, and writes a contact sheet.
v=$1; sheet=$2; n=${3:-9}
echo "probe: $(ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate -of csv=p=0 "$v") dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$v")"
echo "black: $(ffmpeg -hide_banner -i "$v" -vf blackdetect=d=0.3 -an -f null - 2>&1 | grep -c black_start)"
echo "silence: $(ffmpeg -hide_banner -i "$v" -af silencedetect=n=-45dB:d=1.5 -vn -f null - 2>&1 | grep -c silence_start)"
ffmpeg -hide_banner -i "$v" -af loudnorm=print_format=summary -vn -f null - 2>&1 | grep -E "Input Integrated|Input True Peak" | tr -s ' ' | paste -sd' '
d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$v")
cols=$(( n>9 ? 6 : n )); rows=$(( (n+cols-1)/cols ))
ffmpeg -y -loglevel error -i "$v" -vf "fps=$n/$d,scale=270:-1,tile=${cols}x${rows}" -frames:v 1 "$sheet" && echo "sheet: $sheet"

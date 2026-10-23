# content-render

Free render farm for the daily n8n content automation. n8n starts `.github/workflows/render.yml`
with a job (script JSON); the run makes images (Gemini free tier, Pollinations fallback), Kokoro voice,
ffmpeg video with captions and music, automatic QC, publishes the files as a release, and calls n8n back.
Releases older than 7 days are deleted automatically.

Repo secret needed: `GEMINI_API_KEY` (free key from Google AI Studio). Optional: `POLLINATIONS_TOKEN`.
Fonts: Inter (SIL Open Font License). Voice: Kokoro-82M (Apache 2.0).

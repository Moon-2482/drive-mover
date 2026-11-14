# Drive Mover

Web app: paste 1..N links, one click, everything lands in your Google Drive.

- One combined folder in My Drive per batch (name it, or auto-named).
- Inside it, one numbered subfolder per link: `01 - <name>`, `02 - <name>`, ...
- Link types: Google Drive folders/files, MEGA folders, direct download links
  (Dropbox direct links too). Structure preserved, duplicates renamed.
- Live progress per link, pause / resume / retry-failed, SQLite job store.

## Run (on the machine that holds the Drive connection)

```
./run.sh
```

Then open http://127.0.0.1:8766/ — port 8766.

The Drive connection is reused from the already-authorised `hatch_gws_cli`
session on that machine. No tokens or secrets live in this repo.

## How it works

- `server.py` — stdlib HTTP server + JSON API + batch worker.
- Reuses the proven migration engine from `drive-migration`
  (server-side copy, export/upload fallback, md5 checks).
- MEGA folders are downloaded with async-mega (`mega_dl.py`) then uploaded.
- One batch runs at a time (quota-safe); jobs survive restarts.

## Notes

- This repo is code only. The app needs a running Python host with Drive
  access — static hosting alone cannot run the backend.
- Test batch (2026-11-14): 10/10 files copied and verified in Drive.

## Hosts

- **Hugging Face Spaces (Docker): NOT free** — verified 2026-11-14: the Hub API
  returns 402, Docker/Gradio Spaces on free cpu-basic need a PRO subscription.
  Do not retry without the user's explicit paid approval.
- **Render (free tier):** Blueprint deploy — `render.yaml` is in this repo.
  Free instances sleep after 15 min without traffic and have no persistent
  disk (job history resets on restart; files already in Drive are unaffected).
  Set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REDIRECT_URI in the
  Render dashboard, then open the app and tap "Connect Google Drive".

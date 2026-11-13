#!/usr/bin/env python3
"""
Video Transcription — Supabase edition
---------------------------------------
Downloads recently-synced reels and transcribes them with local Whisper,
writing transcripts to the Supabase `posts` table.

A transcript is saved in its own step: if Whisper succeeded, a failed write
retries next run instead of being buried under a failure mark. Only videos
the CDN reports gone (4xx) are marked permanently; transient errors keep
their retry window (IG URLs live ~days, and each weekly sync refreshes them).

Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
     WHISPER_MODEL (default small), MAX_AGE_DAYS (14), VIDEO_LIMIT (40),
     TIME_BUDGET_MIN (300)
"""

import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
MODEL_NAME = os.environ.get("WHISPER_MODEL", "small")
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "14"))
VIDEO_LIMIT = int(os.environ.get("VIDEO_LIMIT", "40"))
TIME_BUDGET_MIN = int(os.environ.get("TIME_BUDGET_MIN", "300"))
FAIL_MARK = "(转录失败)"
MAX_CHARS = 5000


def sb(path, method="GET", data=None, prefer=None):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key}  # new-style keys are not JWTs: apikey header only
    if prefer:
        headers["Prefer"] = prefer
    body = json.dumps(data).encode() if data is not None else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}",
                                 data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def fetch_pending():
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=MAX_AGE_DAYS)).isoformat()
    return sb(
        "posts?select=post_id,competitor,video_url"
        "&video_url=not.is.null&transcript=is.null"
        f"&last_synced=gte.{cutoff}"
        "&order=viral_score.desc.nullslast"
    )


def save_transcript(post_id, text):
    sb(f"posts?post_id=eq.{urllib.parse.quote(post_id)}", method="PATCH",
       data={"transcript": text[:MAX_CHARS]}, prefer="return=minimal")


def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)


def main():
    if not SUPABASE_URL or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required")
        sys.exit(1)

    queue = fetch_pending()
    print(f"{len(queue)} videos pending transcription")
    if not queue:
        return
    if len(queue) > VIDEO_LIMIT:
        print(f"capping at {VIDEO_LIMIT} ({len(queue) - VIDEO_LIMIT} left for next run)")
        queue = queue[:VIDEO_LIMIT]

    import whisper
    print(f"[Init] loading Whisper '{MODEL_NAME}'...")
    model = whisper.load_model(MODEL_NAME)

    import time as _time
    started = _time.time()
    ok = failed = retry = 0
    for i, it in enumerate(queue, 1):
        if (_time.time() - started) / 60 > TIME_BUDGET_MIN:
            print(f"time budget reached — {len(queue) - i + 1} left for next run")
            break
        print(f"[{i}/{len(queue)}] @{it['competitor']} {it['post_id']}")
        tmp_path = None
        text = None
        gone = False
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                tmp_path = f.name
            download(it["video_url"], tmp_path)
            result = model.transcribe(tmp_path, fp16=False)
            text = (result.get("text") or "").strip() or "(无口播内容)"
        except urllib.error.HTTPError as exc:
            gone = exc.code in (401, 403, 404, 410)
            print(f"  {'GONE' if gone else 'TRANSIENT'}: HTTP {exc.code}")
        except Exception as exc:
            print(f"  TRANSIENT: {exc}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if text is not None:
            try:
                save_transcript(it["post_id"], text)
                ok += 1
                print(f"  OK: {text[:70]}")
            except Exception as exc:
                retry += 1
                print(f"  SAVE FAILED, retry next run: {exc}")
        elif gone:
            try:
                save_transcript(it["post_id"], FAIL_MARK)
            except Exception:
                pass
            failed += 1
        else:
            retry += 1

    print(f"Done: {ok} transcribed, {failed} gone (marked), {retry} to retry")


if __name__ == "__main__":
    main()

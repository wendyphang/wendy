#!/usr/bin/env python3
"""
Viral Content Sync — Supabase edition (Instagram + Facebook + 小红书)
----------------------------------------------------------------------
Scrapes ONE platform's competitors via Apify (profiles for follower counts,
then recent posts), keeps the standout posts, and UPSERTS into the Supabase
`posts` table. The workflow runs this once per platform (TRACKER=IG/FB/XHS).

A post is kept when it clears a small noise floor AND passes ANY of three
tests (OR, not AND -- the old AND rule starved small and large accounts
alike):
  1. engagement rate  >= MIN_ER
  2. comments         >= MIN_COMMENTS
  3. engagements      >= OUTLIER_X x that account's OWN median engagements
     (median taken over the posts scraped this run -- a real baseline, so a
     5k-follower account is judged against itself, not against a 300k one)
On top of that, every account always contributes its TOP_N_PER_COMP best
posts, so a quiet week still fills the dashboard instead of showing zero.

Widening the filter costs NOTHING extra on Apify: the scrape already pulls
every post, the filter only decides what gets stored.

Upsert notes: existing posts get their metrics/video URL refreshed (fresh
CDN links let previously skipped videos transcribe); status / transcript /
ai_breakdown are never in the payload, so worked rows are never clobbered.
engagement_rate and viral_score are generated columns — never written.

Platforms
  IG  apify~instagram-profile-scraper + apify~instagram-post-scraper
  FB  apify~facebook-pages-scraper    + apify~facebook-posts-scraper
      (Facebook Pages only — personal profiles can't be scraped)
  XHS a community actor (default zhorex~rednote-xiaohongshu-scraper, swap
      with the XHS_ACTOR variable): mode=profile for 粉丝数, mode=user_posts
      for the notes, then mode=post_details on the kept notes for the full
      正文 / 发布日期 / 视频链接 (XHS_DETAILS caps how many, 0 = off).

Env (required): TRACKER ("IG" / "FB" / "XHS"), APIFY_TOKEN, SUPABASE_URL,
                SUPABASE_SERVICE_ROLE_KEY
Env (optional secrets): XHS_COOKIE — logged-in xiaohongshu.com Cookie header;
                makes the XHS scrape much more reliable (use a spare account)
Env (tuning, all optional -- set as GitHub Actions *Variables*):
     MIN_ER (0.02)  MIN_COMMENTS (15)  MIN_ENGAGE (20)  OUTLIER_X (1.5)
     TOP_N_PER_COMP (3)  MAX_PER_COMP (15)  DAYS_BACK (60)
     RESULTS_LIMIT (50)  MAX_ROWS (3000)
     FB_RESULTS_LIMIT (=RESULTS_LIMIT)  XHS_RESULTS_LIMIT (20)
     XHS_ACTOR (zhorex~rednote-xiaohongshu-scraper)  XHS_DETAILS (20)
"""

import hashlib
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from platforms import ID_PREFIX, LABEL, handle, profile_url

TRACKER = (os.environ.get("TRACKER") or "IG").strip().upper()
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")

POLL_INTERVAL = 30
POLL_TIMEOUT = 900
PROTECTED = ("拍摄中", "已处理")


def _envf(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def _envi(name, default):
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return int(default)


# --- tuning knobs (GitHub Actions Variables; defaults are the loose set) ---
MIN_ER         = _envf("MIN_ER", 0.02)        # 2% engagement rate
MIN_COMMENTS   = _envi("MIN_COMMENTS", 15)    # absolute comment count
MIN_ENGAGE     = _envi("MIN_ENGAGE", 20)      # noise floor: likes+comments
OUTLIER_X      = _envf("OUTLIER_X", 1.5)      # x the account's own median
TOP_N_PER_COMP = _envi("TOP_N_PER_COMP", 3)   # always keep each account's best N
MAX_PER_COMP   = _envi("MAX_PER_COMP", 15)    # cap per account, so one big
                                              # account cannot flood the board
DAYS_BACK      = _envi("DAYS_BACK", 60)       # lookback window (free volume)
RESULTS_LIMIT  = _envi("RESULTS_LIMIT", 50)   # posts scraped per account (costs $)
MAX_ROWS       = _envi("MAX_ROWS", 3000)      # dashboard payload cap (all platforms)

FB_RESULTS_LIMIT  = _envi("FB_RESULTS_LIMIT", RESULTS_LIMIT)
# 小红书 actors are billed per note and cost ~10x IG, so a smaller default.
XHS_RESULTS_LIMIT = _envi("XHS_RESULTS_LIMIT", 20)
XHS_ACTOR   = (os.environ.get("XHS_ACTOR") or "zhorex~rednote-xiaohongshu-scraper").strip().replace("/", "~")
XHS_DETAILS = _envi("XHS_DETAILS", 20)        # notes to enrich with full 正文 (0 = off)
XHS_COOKIE  = (os.environ.get("XHS_COOKIE") or "").strip()

HASHTAG_RE = re.compile(r"#([^\s#,，。!！?？\[\]]{1,40})")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class HTTPFail(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body[:200]}")
        self.code, self.body = code, body


def _request(url, method="GET", data=None, headers=None, timeout=60):
    hdrs = headers or {}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return raw
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        safe = re.sub(r"token=[^&]+", "token=***", url[:100])
        print(f"  HTTP {exc.code} for {method} {safe}")
        print(f"  Response: {text[:400]}")
        raise HTTPFail(exc.code, text) from None


def sb(path, method="GET", data=None, prefer=None):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key}  # new-style keys are not JWTs: apikey header only
    if prefer:
        headers["Prefer"] = prefer
    return _request(f"{SUPABASE_URL}/rest/v1/{path}", method, data, headers, 60)


def num(v):
    """Counts arrive as 123 / '1,234' / '1.2万' / '3.4w' / '10万+' / None."""
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().lower().replace(",", "").rstrip("+")
    mult = 1
    for suf, m in (("万", 10_000), ("w", 10_000), ("亿", 100_000_000),
                   ("k", 1_000), ("m", 1_000_000)):
        if s.endswith(suf):
            s, mult = s[:-len(suf)], m
            break
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def first(d, *keys):
    """First non-empty value among dotted keys ('author.userId')."""
    for k in keys:
        cur = d
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur not in (None, "", [], {}):
            return cur
    return None


def tags_of(text, given=None):
    if isinstance(given, list) and given:
        out = [(t.get("name") if isinstance(t, dict) else str(t)) for t in given]
        return ", ".join(t.lstrip("#") for t in out if t)
    if isinstance(given, str) and given.strip():
        return given
    return ", ".join(dict.fromkeys(HASHTAG_RE.findall(text or "")))


# ---------------------------------------------------------------------------
# Competitors (self-service list in Supabase)
# ---------------------------------------------------------------------------


def fetch_competitors():
    rows = sb(f"competitors?select=username&tracker=eq.{TRACKER}&active=is.true")
    names = []
    for r in rows:
        raw = (r.get("username") or "").strip()
        n = handle(raw, TRACKER)
        if not n:
            print(f"  WARNING: skipping non-profile link: {raw[:60]}")
            continue
        if n not in names:
            names.append(n)
    return names


def suggest_competitors(profile_items):
    """IG only: rolling shortlist — replace last week's un-adopted
    suggestions with this week's most frequent unseen related profiles."""
    existing = {(r.get("username") or "").lower().lstrip("@")
                for r in sb("competitors?select=username")}
    counts = {}
    for prof in profile_items:
        for rp in (prof.get("relatedProfiles") or []):
            u = (rp.get("username") or "").strip().lower()
            if u and u not in existing:
                counts[u] = counts.get(u, 0) + 1
    top = sorted(counts.items(), key=lambda x: -x[1])[:8]
    if not top:
        print("[Suggest] no new related profiles this run")
        return
    pat = urllib.parse.quote("系统推荐*")
    sb(f"competitors?tracker=eq.{TRACKER}&active=is.false&notes=like.{pat}",
       method="DELETE", prefer="return=minimal")
    today = datetime.now(timezone.utc).date().isoformat()
    sb("competitors", method="POST", prefer="return=minimal", data=[
        {"username": u, "tracker": TRACKER, "active": False,
         "notes": f"系统推荐 {today}(相关度 {n})— 觉得好就把 active 打勾"}
        for u, n in top
    ])
    print(f"[Suggest] refreshed shortlist: {', '.join(u for u, _ in top)}")


# ---------------------------------------------------------------------------
# Apify
# ---------------------------------------------------------------------------


def run_actor(token, actor_id, actor_input, label):
    print(f"[{label}] {actor_id}...")
    result = _request(f"https://api.apify.com/v2/acts/{actor_id}/runs?token={token}",
                      "POST", actor_input, timeout=120)
    run_id = result["data"]["id"]
    print(f"  run {run_id}")
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        st = _request(f"https://api.apify.com/v2/actor-runs/{run_id}?token={token}",
                      timeout=30)["data"]
        print(f"  {st['status']}")
        if st["status"] == "SUCCEEDED":
            items = _request(
                f"https://api.apify.com/v2/datasets/{st['defaultDatasetId']}/items"
                f"?token={token}&clean=true", timeout=120)
            return items if isinstance(items, list) else []
        if st["status"] in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run ended: {st['status']}")
    # abort so a run nobody will fetch stops billing
    try:
        _request(f"https://api.apify.com/v2/actor-runs/{run_id}/abort?token={token}",
                 "POST", timeout=30)
        print(f"  aborted {run_id} to stop billing")
    except Exception as exc:
        print(f"  WARNING: could not abort {run_id}: {exc}")
    raise TimeoutError(f"run {run_id} exceeded {POLL_TIMEOUT}s — fewer competitors "
                       f"or a higher POLL_TIMEOUT needed")


def parse_date(v):
    if isinstance(v, (int, float)):
        if v > 1e12:  # milliseconds
            v = v / 1000
        return datetime.fromtimestamp(v, tz=timezone.utc).date().isoformat()
    if isinstance(v, str) and v.strip():
        s = v.strip()
        if s.isdigit():
            return parse_date(int(s))
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
            if m:
                return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"
    return None


def item(comp, fol, likes, comments, row):
    """One scraped post in the common shape to_rows() understands."""
    row.setdefault("tracker", TRACKER)
    row.setdefault("competitor", comp)
    row["likes"], row["comments"], row["followers"] = likes, comments, fol
    return {"comp": comp, "fol": fol, "likes": likes, "comments": comments, "row": row}


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------


def ig_post_type(p):
    m = {"Image": "Image", "Video": "Video", "Sidecar": "Carousel", "Reel": "Reel"}
    return m.get((p.get("type") or "").strip(),
                 "Video" if p.get("isVideo") else "Image")


def scrape_ig(token, names, today):
    profiles = run_actor(token, "apify~instagram-profile-scraper",
                         {"usernames": names}, "Step 1 profiles")
    followers = {}
    for pr in profiles:
        u = (pr.get("username") or "").lower().strip()
        c = pr.get("followersCount") or pr.get("followers") or 0
        if u and c:
            followers[u] = int(c)
    print(f"  followers for {len(followers)}/{len(names)}")

    try:
        suggest_competitors(profiles)
    except Exception as exc:
        print(f"[Suggest] skipped ({exc})")

    posts = run_actor(token, "apify~instagram-post-scraper",
                      {"username": names, "resultsLimit": RESULTS_LIMIT,
                       "onlyPostsNewerThan": f"{DAYS_BACK} days"}, "Step 2 posts")
    print(f"  scraped {len(posts)} posts (last {DAYS_BACK} days, "
          f"<= {RESULTS_LIMIT}/account)")

    out = []
    for p in posts:
        if p.get("error") or not p.get("shortCode"):
            out.append(None)
            continue
        u = (p.get("ownerUsername") or (p.get("owner") or {}).get("username") or "")
        u = u.lower().strip()
        pt = ig_post_type(p)
        tags = p.get("hashtags") or []
        out.append(item(u, followers.get(u, 0),
                        int(p.get("likesCount") or p.get("likes") or 0),
                        int(p.get("commentsCount") or p.get("comments") or 0), {
            "post_id": p["shortCode"],
            "caption": (p.get("caption") or "")[:5000],
            "post_type": pt,
            "post_date": parse_date(p.get("timestamp")),
            "post_url": f"https://www.instagram.com/p/{p['shortCode']}/",
            "thumbnail_url": p.get("displayUrl") or p.get("imageUrl") or None,
            "video_url": p.get("videoUrl") or None,
            "hashtags": ", ".join(tags) if isinstance(tags, list) else str(tags),
            "is_video": bool(p.get("isVideo") or pt in ("Video", "Reel")),
            "last_synced": today,
        }))
    return out


# ---------------------------------------------------------------------------
# Facebook (Pages)
# ---------------------------------------------------------------------------


def scrape_fb(token, names, today):
    urls = [profile_url(n, "FB") for n in names]
    starts = [{"url": u} for u in urls]

    pages = run_actor(token, "apify~facebook-pages-scraper",
                      {"startUrls": starts}, "Step 1 pages")
    followers, titles = {}, {}
    for pg in pages:
        fol = num(first(pg, "followers", "followersCount", "likes"))
        keys = {handle(first(pg, "inputUrl", "pageUrl", "facebookUrl", "url") or "", "FB"),
                handle(first(pg, "pageUrl", "facebookUrl") or "", "FB"),
                str(first(pg, "pageName") or "").lower(),
                str(first(pg, "pageId", "facebookId") or "")}
        for k in keys - {""}:
            if fol:
                followers[k] = fol
            if pg.get("title"):
                titles[k] = pg["title"]
    print(f"  followers for {sum(1 for n in names if n in followers)}/{len(names)}")

    posts = run_actor(token, "apify~facebook-posts-scraper",
                      {"startUrls": starts, "resultsLimit": FB_RESULTS_LIMIT,
                       "onlyPostsNewerThan": f"{DAYS_BACK} days",
                       "captionText": False}, "Step 2 posts")
    print(f"  scraped {len(posts)} posts (last {DAYS_BACK} days, "
          f"<= {FB_RESULTS_LIMIT}/page)")

    out = []
    for p in posts:
        pid = str(first(p, "postId", "id") or "")
        if p.get("error") or not pid:
            out.append(None)
            continue
        # match the post back to the competitor handle we asked for
        cands = [handle(p.get("inputUrl") or "", "FB"),
                 handle(p.get("facebookUrl") or "", "FB"),
                 str(p.get("pageName") or "").lower(),
                 str(p.get("facebookId") or ""),
                 str(first(p, "user.id") or "")]
        comp = next((c for c in cands if c in names), None) \
            or next((c for c in cands if c), "")
        fol = next((followers[c] for c in cands if c in followers), 0)
        media = p.get("media") or []
        m0 = media[0] if media and isinstance(media[0], dict) else {}
        url = p.get("url") or p.get("topLevelUrl") or ""
        kinds = {str(m.get("__typename") or "").lower() for m in media if isinstance(m, dict)}
        is_video = bool(p.get("isVideo") or "video" in kinds or "/reel/" in url
                        or "/videos/" in url or p.get("viewsCount"))
        pt = ("Reel" if "/reel/" in url else "Video" if is_video
              else "Carousel" if len(media) > 1 else "Image" if media else "Text")
        text = p.get("text") or ""
        video_url = first(p, "videoUrl", "video_url") or first(
            m0, "playable_url_quality_hd", "playable_url", "videoUrl")
        out.append(item(comp, fol, num(first(p, "likes", "topReactionsCount")),
                        num(p.get("comments")), {
            "post_id": ID_PREFIX["FB"] + pid,
            "author_name": first(p, "user.name") or titles.get(comp) or p.get("pageName"),
            "caption": text[:5000],
            "post_type": pt,
            "post_date": parse_date(first(p, "time", "timestamp")),
            "post_url": url or f"https://www.facebook.com/{pid}",
            "thumbnail_url": first(m0, "thumbnail", "photo_image.uri", "image.uri"),
            "video_url": video_url,
            "hashtags": tags_of(text),
            "is_video": is_video,
            "last_synced": today,
        }))
    return out


# ---------------------------------------------------------------------------
# 小红书 (RedNote)
# ---------------------------------------------------------------------------


def _xhs_input(**kw):
    if XHS_COOKIE:
        kw["cookieString"] = XHS_COOKIE
    return kw


def _xhs_note_id(p):
    nid = first(p, "postId", "noteId", "note_id", "id", "postData.noteId")
    if nid:
        return str(nid)
    m = re.search(r"/(?:explore|discovery/item)/([0-9a-f]{24})",
                  str(first(p, "postUrl", "url", "postData.postUrl") or ""))
    return m.group(1) if m else ""


def scrape_xhs(token, names, today):
    urls = [profile_url(n, "XHS") for n in names]
    if not XHS_COOKIE:
        print("  NOTE: no XHS_COOKIE secret — 小红书 often hides note IDs from "
              "logged-out scrapers. If notes come back without links, add the "
              "cookie (see README).")

    followers, nick = {}, {}
    try:
        profs = run_actor(token, XHS_ACTOR,
                          _xhs_input(mode="profile", userUrls=urls), "Step 1 profiles")
        for pr in profs:
            uid = handle(first(pr, "profileUrl") or "", "XHS") or str(first(pr, "userId") or "")
            if not uid:
                continue
            fol = num(first(pr, "followers", "fans", "fansCount"))
            if fol:
                followers[uid] = fol
            if first(pr, "nickname"):
                nick[uid] = first(pr, "nickname")
    except Exception as exc:
        print(f"  WARNING: profile step failed ({exc}) — scoring falls back to likes")
    print(f"  followers for {sum(1 for n in names if n in followers)}/{len(names)}")

    posts = run_actor(token, XHS_ACTOR,
                      _xhs_input(mode="user_posts", userUrls=urls,
                                 maxResults=XHS_RESULTS_LIMIT), "Step 2 notes")
    print(f"  scraped {len(posts)} notes (<= {XHS_RESULTS_LIMIT}/account)")

    out = []
    for p in posts:
        if p.get("error"):
            out.append(None)
            continue
        data = p.get("postData") if isinstance(p.get("postData"), dict) else p
        uid = (handle(p.get("profileUrl") or "", "XHS")
               or str(first(data, "author.userId", "user.userId", "userId") or ""))
        title = str(first(data, "title", "displayTitle") or "")
        content = str(first(data, "content", "desc", "description") or "")
        nid = _xhs_note_id(data)
        url = str(first(data, "postUrl", "url") or "")
        if not nid:
            # no id from a logged-out scrape: still show the note, keyed on a
            # stable hash so re-syncs update the same row instead of duplicating
            if not title:
                out.append(None)
                continue
            nid = "h" + hashlib.sha1(f"{uid}|{title}".encode()).hexdigest()[:20]
        if not url or "/explore/" not in url:
            url = (f"https://www.xiaohongshu.com/explore/{nid}"
                   if not nid.startswith("h") else profile_url(uid, "XHS"))
        images = first(data, "images", "imageList") or []
        cover = (images[0] if images and isinstance(images[0], str)
                 else first(images[0], "url", "urlDefault") if images else None) \
            or first(data, "cover.urlDefault", "cover.url", "cover.urlPre",
                     "cover.infoList.0.url", "cover")
        if isinstance(cover, dict):
            cover = first(cover, "urlDefault", "url")
        ntype = str(first(data, "type") or "").lower()
        is_video = ntype == "video" or bool(first(data, "videoUrl", "hasVideo"))
        caption = (title + ("\n" + content if content and content != title else "")).strip()
        likes = num(first(data, "likes", "likedCount", "interactInfo.likedCount", "liked_count"))
        comments = num(first(data, "comments", "commentCount", "interactInfo.commentCount"))
        out.append(item(uid, followers.get(uid, 0), likes, comments, {
            "post_id": ID_PREFIX["XHS"] + nid,
            "author_name": (first(data, "author.nickname", "user.nickname", "user.nickName",
                                  "authorName") or nick.get(uid)),
            "caption": caption[:5000],
            "post_type": "Video" if is_video else "Note",
            "post_date": parse_date(first(data, "publishedAt", "time", "publishTime")),
            "post_url": url,
            "thumbnail_url": cover if isinstance(cover, str) else None,
            "video_url": first(data, "videoUrl"),
            "hashtags": tags_of(caption, first(data, "tags", "tagList")),
            "is_video": is_video,
            "last_synced": today,
        }))
    return out


def enrich_xhs(token, rows):
    """Feed cards carry only the title. For the notes that made the cut, pull
    the full 正文 / 发布日期 / 视频链接 so AI 拆解 and 我的稿 have material."""
    if XHS_DETAILS <= 0:
        return
    targets = [r for r in rows if "/explore/" in (r.get("post_url") or "")]
    targets.sort(key=lambda r: -(r["likes"] + r["comments"] * 3))
    targets = targets[:XHS_DETAILS]
    if not targets:
        return
    try:
        det = run_actor(token, XHS_ACTOR,
                        _xhs_input(mode="post_details",
                                   postUrls=[r["post_url"] for r in targets]),
                        f"Step 3 details ({len(targets)} notes)")
    except Exception as exc:
        print(f"  WARNING: details step failed ({exc}) — keeping titles only")
        return
    by_id = {ID_PREFIX["XHS"] + _xhs_note_id(d): d for d in det if _xhs_note_id(d)}
    hit = 0
    for r in targets:
        d = by_id.get(r["post_id"])
        if not d:
            continue
        hit += 1
        title = str(first(d, "title") or "")
        content = str(first(d, "content", "desc") or "")
        cap = (title + ("\n" + content if content else "")).strip()
        if len(cap) > len(r.get("caption") or ""):
            r["caption"] = cap[:5000]
        r["post_date"] = parse_date(first(d, "publishedAt", "time")) or r.get("post_date")
        if r.get("is_video"):
            r["video_url"] = first(d, "videoUrl") or r.get("video_url")
        r["hashtags"] = tags_of(r["caption"], first(d, "tags")) or r.get("hashtags")
        r["comments"] = max(r["comments"], num(first(d, "comments")))
        r["likes"] = max(r["likes"], num(first(d, "likes")))
    print(f"  enriched {hit}/{len(targets)} notes with full text")


# ---------------------------------------------------------------------------
# Filter (same rules for every platform)
# ---------------------------------------------------------------------------


def to_rows(items):
    """Group the scrape by account, judge each account against ITSELF, and
    guarantee every account contributes its best few posts."""
    by_comp, nofol, broken = {}, 0, 0
    for it in items:
        if not it or not it["comp"]:
            broken += 1
            continue
        fol = it["fol"]
        if not fol:
            if TRACKER == "IG":
                nofol += 1
                continue
            # FB/XHS profile lookups are flakier than IG's: keep the post and
            # let it compete on its account's own median instead of dropping it
            nofol += 1
        eng = it["likes"] + it["comments"]
        by_comp.setdefault(it["comp"], []).append({
            "row": it["row"],
            "eng": eng,
            "er": eng / fol if fol else 0,
            "comments": it["comments"],
            # same weighting the dashboard sorts by, so "top N" means the same thing
            "score": (it["likes"] + it["comments"] * 3) / fol * 100 if fol
                     else (it["likes"] + it["comments"] * 3) / 1e4,
        })

    rows, below_total, rescued_total = [], 0, 0
    print(f"  {'账号':<24}{'抓到':>5}{'入选':>5}{'补位':>5}  基准/中位互动")
    for u in sorted(by_comp):
        recs = by_comp[u]
        engs = [r["eng"] for r in recs]
        med = statistics.median(engs) if len(engs) >= 3 else 0
        keep_ids, kept = set(), []
        for r in recs:
            if r["eng"] < MIN_ENGAGE or r["row"]["post_id"] in keep_ids:
                continue
            if ((r["er"] and r["er"] >= MIN_ER)
                    or r["comments"] >= MIN_COMMENTS
                    or (med > 0 and r["eng"] >= OUTLIER_X * med)):
                keep_ids.add(r["row"]["post_id"])
                kept.append(r)
        passed = len(kept)
        # safety net: a quiet account still contributes its best posts
        rescued = 0
        if passed < TOP_N_PER_COMP:
            for r in sorted(recs, key=lambda r: -r["score"]):
                if r["row"]["post_id"] in keep_ids:
                    continue
                keep_ids.add(r["row"]["post_id"])
                kept.append(r)
                rescued += 1
                if len(kept) >= TOP_N_PER_COMP:
                    break
        capped = 0
        if len(kept) > MAX_PER_COMP:
            kept.sort(key=lambda r: -r["score"])
            capped = len(kept) - MAX_PER_COMP
            kept = kept[:MAX_PER_COMP]
        below_total += len(recs) - passed
        rescued_total += rescued
        rows.extend(r["row"] for r in kept)
        note = f" · 超额截掉 {capped}" if capped else ""
        fol = recs[0]["row"]["followers"]
        name = recs[0]["row"].get("author_name") or u
        print(f"  @{str(name)[:23]:<23}{len(recs):>5}{len(kept):>5}{rescued:>5}  "
              f"中位 {int(med)} 互动 · {fol:,} 粉{note}")

    print(f"  ── 合计 {len(rows)} 条入库 "
          f"({len(rows) - rescued_total} 达标 + {rescued_total} 补位) | "
          f"{below_total} 条未达标 | {nofol} 条缺粉丝数 | {broken} 条抓取失败")
    if len(rows) < 20:
        print("  提示: 内容偏少。到仓库 Settings → Variables 调松阈值，例如")
        print("        MIN_ER=0.01  MIN_COMMENTS=8  OUTLIER_X=1.2  TOP_N_PER_COMP=5")
        print("        想要每个账号更多条 → 调大 MAX_PER_COMP")
        print("        或在 Supabase 的 competitors 表多加几个竞对账号。")
    return rows


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def upsert(rows):
    """Write in chunks. Databases created before the multi-platform update
    have no author_name column: drop it and keep going instead of failing."""
    for i in range(0, len(rows), 100):
        chunk = rows[i:i+100]
        try:
            sb("posts?on_conflict=post_id", method="POST", data=chunk,
               prefer="resolution=merge-duplicates,return=minimal")
        except HTTPFail as exc:
            if "author_name" not in exc.body:
                raise
            print("  NOTE: posts 表还没有 author_name 列 —— 到 Supabase SQL Editor 跑一句:")
            print("        alter table posts add column if not exists author_name text;")
            print("        (不跑也能用,只是小红书/FB 卡片上显示 ID 而不是昵称)")
            for r in rows:
                r.pop("author_name", None)
            sb("posts?on_conflict=post_id", method="POST", data=chunk,
               prefer="resolution=merge-duplicates,return=minimal")


def cleanup():
    rows = sb("posts?select=post_id,status,last_synced&order=last_synced.asc.nullsfirst")
    if len(rows) <= MAX_ROWS:
        print(f"[Cleanup] {len(rows)} rows <= {MAX_ROWS}, nothing removed")
        return
    overflow = len(rows) - MAX_ROWS
    victims = [r["post_id"] for r in rows if r.get("status") not in PROTECTED][:overflow]
    if not victims:
        print(f"[Cleanup] WARNING: {overflow} over cap but all rows carry work")
        return
    for i in range(0, len(victims), 100):
        chunk = ",".join(urllib.parse.quote(v, safe="") for v in victims[i:i+100])
        sb(f"posts?post_id=in.({chunk})",
           method="DELETE", prefer="return=minimal")
    print(f"[Cleanup] removed {len(victims)} oldest unworked rows")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SCRAPERS = {"IG": scrape_ig, "FB": scrape_fb, "XHS": scrape_xhs}


def main():
    token = (os.environ.get("APIFY_TOKEN") or "").strip()
    if not token or not SUPABASE_URL or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("ERROR: APIFY_TOKEN / SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required")
        sys.exit(1)
    if TRACKER not in SCRAPERS:
        print(f"ERROR: TRACKER must be one of {', '.join(SCRAPERS)} (got {TRACKER})")
        sys.exit(1)

    names = fetch_competitors()
    if not names:
        print(f"No active {TRACKER} ({LABEL[TRACKER]}) competitors in Supabase — "
              f"nothing to do. 想追踪就在 competitors 表加一行,tracker 填 {TRACKER}。")
        return
    print(f"[{TRACKER} · {LABEL[TRACKER]}] {len(names)} competitors: "
          f"{', '.join(names[:8])}" + (" ..." if len(names) > 8 else ""))
    print(f"  filter: ER>={MIN_ER:.1%} OR comments>={MIN_COMMENTS} OR "
          f"engagements>={OUTLIER_X}x own median | floor {MIN_ENGAGE} | "
          f"keep {TOP_N_PER_COMP}-{MAX_PER_COMP} per account")

    today = datetime.now(timezone.utc).date().isoformat()
    items = SCRAPERS[TRACKER](token, names, today)
    rows = to_rows(items)
    if TRACKER == "XHS" and rows:
        enrich_xhs(token, rows)

    if rows:
        upsert(rows)
        print(f"[Upsert] {len(rows)} rows written")

        # An IG 403 usually means the CDN signature expired, not that the video
        # is gone. This sync just refreshed those URLs — clear the fail marks so
        # transcription gets another shot with the fresh links.
        mark = urllib.parse.quote("(转录失败)")
        sb(f"posts?transcript=eq.{mark}&last_synced=eq.{today}&tracker=eq.{TRACKER}",
           method="PATCH", data={"transcript": None}, prefer="return=minimal")
        print("[Upsert] reopened previously-failed transcripts with fresh URLs")
    else:
        print("[Upsert] nothing passed the filter — check the per-account table "
              "above, then loosen the Variables or add more competitors")

    try:
        cleanup()
    except Exception as exc:
        print(f"[Cleanup] skipped ({exc})")

    print("Done.")


if __name__ == "__main__":
    main()

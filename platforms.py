"""
Shared platform helpers — one place that decides what a competitor handle
means on each platform, so sync_content.py and generate_dashboard.py always
agree (the dashboard hides accounts whose `active` box is unticked, and that
only works if both scripts normalise '@Nike' / full links the same way).

The `tracker` column doubles as the platform:
    IG  = Instagram      FB = Facebook Page      XHS = 小红书 (RedNote)
"""

import urllib.parse

PLATFORMS = ("IG", "FB", "XHS")
LABEL = {"IG": "Instagram", "FB": "Facebook", "XHS": "小红书"}

# Post IDs share one unique column across platforms, so FB / XHS ids get a
# prefix. IG keeps the bare shortcode (existing rows + the IG embed use it).
ID_PREFIX = {"IG": "", "FB": "fb_", "XHS": "xhs_"}


def handle(name, tracker="IG"):
    """Normalise whatever the user typed into the competitors table.

    IG : 'nike' / '@nike' / 'instagram.com/nike/?igsh=..'      -> 'nike'
    FB : 'nike' / 'facebook.com/nike' / 'fb.com/profile.php?id=123' -> 'nike' / '123'
    XHS: '5d5a56c1...' / 'xiaohongshu.com (or rednote.com)/user/profile/5d5a56c1...?xsec=..'
                                                                -> '5d5a56c1...'
    Returns '' for links that point at a single post instead of a profile.
    """
    n = (name or "").strip()
    t = (tracker or "IG").upper()
    if "://" not in n and ("." in n.split("/")[0] and "/" in n):
        n = "https://" + n  # 'instagram.com/nike' without a scheme
    if "://" in n:
        u = urllib.parse.urlparse(n)
        parts = [p for p in u.path.split("/") if p]
        host = u.netloc.lower()
        if "instagram.com" in host:
            if not parts or parts[0] in ("p", "reel", "reels", "stories", "tv"):
                return ""
            n = parts[0]
        elif "facebook.com" in host or "fb.com" in host:
            q = urllib.parse.parse_qs(u.query)
            if parts and parts[0] == "profile.php" and q.get("id"):
                n = q["id"][0]
            elif parts and parts[0] in ("people",) and len(parts) >= 3:
                n = parts[2]
            elif parts and parts[0] in ("pages", "pg") and len(parts) >= 2:
                n = parts[-1] if parts[-1].isdigit() else parts[1]
            elif not parts or parts[0] in ("watch", "reel", "share", "groups",
                                           "story.php", "permalink.php", "photo"):
                return ""
            else:
                n = parts[0]
        elif any(d in host for d in ("xiaohongshu.com", "xhslink.com", "rednote.com")):
            if "profile" in parts:
                i = parts.index("profile")
                n = parts[i + 1] if i + 1 < len(parts) else ""
            else:
                return ""  # explore/<noteId> = a single note, not a profile
        else:
            n = parts[0] if parts else ""
    n = n.lstrip("@").strip()
    return n if t == "XHS" else n.lower()


def profile_url(h, tracker="IG"):
    t = (tracker or "IG").upper()
    if t == "FB":
        if h.isdigit():
            return f"https://www.facebook.com/profile.php?id={h}"
        return f"https://www.facebook.com/{h}"
    if t == "XHS":
        return f"https://www.xiaohongshu.com/user/profile/{h}"
    return f"https://www.instagram.com/{h}/"

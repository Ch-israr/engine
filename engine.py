"""TwitterDown engine — standalone X video extractor (Python stdlib only).

Turns an x.com / twitter.com tweet link into direct video.twimg.com MP4
links, without yt-dlp and without any X account or API key.

Two routes in, tried in this order (same strategy yt-dlp uses):

1. GraphQL (``TweetResultByRestId``) with an anonymous guest token.
   Full metadata: title, uploader, all qualities, thumbnail.
2. Syndication endpoint (``cdn.syndication.twimg.com/tweet-result``).
   No auth at all — the token is a pure function of the tweet id.
   Used when GraphQL is rate-limited, rejected, or empty. Gives less
   metadata, but the video links are the same.

Plus an optional third route, tried FIRST when configured:

0. App (X API v2) with the app's own Bearer token, read from the
   ``X_BEARER_TOKEN`` environment variable — never from code, never
   written to a file. Most reliable when the app has credits, and the
   best candidate for sensitive-media tweets guests cannot see. When
   the app is out of credits (HTTP 402) or the token is missing, the
   engine silently falls through to the guest routes, so downloads
   keep working. The Consumer Key / Secret are NOT used: app-only
   calls need only the Bearer token.

Public API (designed as a drop-in for the yt-dlp block in the main
TwitterDown Flask app — same function name, same return shape, same
error wording, so the swap later is a one-function replacement)::

    videos, thumbnail, title = extract_direct_videos(url)

``videos`` is a list of ``{"label", "width", "height", "url"}`` sorted by
quality ascending, best format per height, direct http(s) MP4 links only.
``label`` follows the site's rule: ``"{width}x{height}"`` when the width
is known, else ``"{height}p"`` (a GIF with no dimensions is ``"0p"``).

Errors raise :class:`EngineError` with an ``http_status`` attribute that
maps straight onto the site's existing responses:

* 400 — not an X link / unparseable URL
* 404 — no video in the post (photos only, private, deleted, protected)
* 502 — X's endpoints failed on both routes

No state is kept except an in-memory guest-token cache (one per process),
so the module is safe to import into a gunicorn worker as-is.
"""

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request

ENGINE_VERSION = "0.1.0"

# Public client credentials for X's guest activation — the same values X's
# own web app ships and yt-dlp publishes openly. They identify the *app*,
# not a user; there is no account, password or API key here. Injected at
# build time from the installed yt-dlp source; never printed or logged.
_AUTH = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
_LEGACY_AUTH = "AAAAAAAAAAAAAAAAAAAAAIK1zgAAAAAA2tUWuhGZ2JceoId5GwYWU5GspY4%3DUq7gzFoCZs1QfwGoVdvSac3IniczZEYXIcDyumCauIXpcAPorE"

_API_BASE = "https://api.x.com/1.1/"
_V2_BASE = "https://api.twitter.com/2/tweets/"
_GRAPHQL_BASE = "https://x.com/i/api/graphql/"
_GRAPHQL_ENDPOINT = "2ICDjqPd81tulZcYrtpTuQ/TweetResultByRestId"
_SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Feature flags for the TweetDetail query, as X's web client sends them.
# X changes these occasionally; if GraphQL starts returning errors about
# unknown features, refresh this dict from yt-dlp's twitter extractor.
_GRAPHQL_FEATURES = {
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": False,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

# Same wording the site already maps to its friendly 404 message.
NO_VIDEO_MESSAGE = (
    "no video could be found in this post — it may only contain photos, "
    "or it may be private or deleted."
)

_TIMEOUT = 30
_guest_token = None
_last_route = None  # route that actually served the last _fetch_status call
_last_status = None  # (twid, status) from the last extract_direct_videos call

# X's direct MP4 URLs carry their dimensions in the path:
#   .../vid/1280x720/abc123.mp4
# GIF (tweet_video) URLs carry none, so width/height stay None.
_DIM_RE = re.compile(r"/(\d+)x(\d+)/")
_TCO_RE = re.compile(r"\s+(https?://[^ ]+)")


class EngineError(Exception):
    """An extraction failure that maps to an HTTP status for the site."""

    def __init__(self, http_status, message):
        super().__init__(message)
        self.http_status = http_status
        self.message = message


# --------------------------------------------------------------------------
# URL parsing
# --------------------------------------------------------------------------

def parse_tweet_id(url):
    """Return the numeric tweet id from an x.com / twitter.com link.

    Accepts https://x.com/<user>/status/<id>, https://twitter.com/...,
    mobile subdomains, and /i/status/<id>. Anything else raises 400.
    """
    try:
        parts = urllib.parse.urlparse((url or "").strip())
    except Exception:
        raise EngineError(400, "Invalid URL")
    host = (parts.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host not in ("x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"):
        raise EngineError(400, "TwitterDown is for X only. Please paste a link from x.com or twitter.com.")
    m = re.search(r"/status/(\d+)", parts.path or "")
    if not m:
        raise EngineError(400, "That link doesn't look like a tweet with a video.")
    return m.group(1)


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------

def _http_json(url, headers=None, data=None, method="GET"):
    """GET/POST a URL and decode the JSON body. HTTP errors become the
    status code; network failures become 502."""
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            body = {}
        return e.code, body
    except Exception as e:
        raise EngineError(502, "Could not reach X: %s" % e)


# --------------------------------------------------------------------------
# Route 0: X API v2 with the app's own Bearer token (optional)
# --------------------------------------------------------------------------

def _v2_status(twid):
    """Fetch the tweet through X API v2 using the app's Bearer token.

    The token comes from the X_BEARER_TOKEN environment variable only.
    Raises 402 when the app is out of credits, 429 when rate-limited —
    both are "try the next route" signals for the auto chain, not fatal.
    """
    token = os.environ.get("X_BEARER_TOKEN", "")
    if not token:
        raise EngineError(502, "X app credentials not configured")
    url = _V2_BASE + twid + "?" + urllib.parse.urlencode({
        "expansions": "attachments.media_keys",
        "media.fields": "type,url,variants,preview_image_url,width,height,duration_ms",
        "tweet.fields": "possibly_sensitive,created_at,text,author_id",
        "user.fields": "name,username",
    })
    status, body = _http_json(url, headers={
        "Authorization": "Bearer " + token,
        "User-Agent": _BROWSER_UA,
    })
    if status == 402:
        err = EngineError(402, "X app credits depleted")
        err.detail = (body or {}).get("detail", "")
        raise err
    if status == 429:
        raise EngineError(429, "X app rate-limited")
    if status in (401, 403):
        raise EngineError(502, "X app token rejected (HTTP %s)" % status)
    if status != 200:
        raise EngineError(502, "X API v2 error (HTTP %s)" % status)
    data = (body or {}).get("data")
    if not isinstance(data, dict):
        # e.g. {"errors": [{"title": "Not Found Error", ...}]} — the tweet
        # is gone, protected, or otherwise invisible to the app.
        err = EngineError(404, NO_VIDEO_MESSAGE)
        errors = (body or {}).get("errors") or []
        err.detail = "; ".join(str(e.get("title", "")) for e in errors if isinstance(e, dict)) or "no data"
        raise err
    includes = (body or {}).get("includes") or {}
    media = []
    for m in (includes.get("media") or []):
        if not isinstance(m, dict):
            continue
        media.append({
            "type": m.get("type"),
            "video_info": {"variants": m.get("variants") or []},
            "media_url_https": m.get("preview_image_url") or m.get("url"),
            "width": m.get("width"),
            "height": m.get("height"),
        })
    users = (includes.get("users") or [])
    user = users[0] if users and isinstance(users[0], dict) else {}
    return {
        "full_text": data.get("text") or "",
        "possibly_sensitive": data.get("possibly_sensitive"),
        "user": {"name": user.get("name"), "screen_name": user.get("username")},
        "extended_entities": {"media": media},
    }


# --------------------------------------------------------------------------
# Route 1: guest token + GraphQL
# --------------------------------------------------------------------------

def _fetch_guest_token():
    """Activate an anonymous guest token. Cached per process; cleared and
    re-fetched once if a later call comes back 401/403."""
    global _guest_token
    if _guest_token:
        return _guest_token
    status, body = _http_json(
        _API_BASE + "guest/activate.json",
        headers={
            "Authorization": "Bearer " + _AUTH,
            # X answers 404 to requests with urllib's default UA; a real
            # browser UA is required even for this anonymous endpoint.
            "User-Agent": _BROWSER_UA,
        },
        data=b"",
        method="POST",
    )
    token = (body or {}).get("guest_token") if isinstance(body, dict) else None
    if status != 200 or not token:
        raise EngineError(502, "Could not get a guest token from X (HTTP %s)" % status)
    _guest_token = token
    return token


def _graphql_to_legacy(data, twid):
    """Convert a TweetResultByRestId response to yt-dlp's legacy shape."""
    result = ((data or {}).get("data") or {}).get("tweetResult", {}).get("result") or {}
    typename = result.get("__typename")
    if typename == "TweetTombstone" or "tombstone" in result:
        # Deleted / removed tweet. X sometimes sends the tombstone text,
        # sometimes just the typename with no detail at all. Either way
        # the site's contract is the friendly no-video 404, so the
        # specific reason rides along as `detail` for logs only.
        cause = (((result.get("tombstone") or {}).get("text") or {}).get("text") or "")
        cause = cause[:-len(". Learn more")] if cause.endswith(". Learn more") else cause
        err = EngineError(404, NO_VIDEO_MESSAGE)
        err.detail = cause or "TweetTombstone (deleted or removed)"
        raise err
    if typename == "TweetUnavailable":
        reason = result.get("reason") or "Requested tweet is unavailable"
        err = EngineError(404, NO_VIDEO_MESSAGE)
        err.detail = "TweetUnavailable: %s" % reason
        raise err
    if typename not in ("Tweet", "TweetWithVisibilityResults", None):
        # Unknown shape — treat as unavailable rather than crashing.
        err = EngineError(404, NO_VIDEO_MESSAGE)
        err.detail = "Unknown typename: %s" % typename
        raise err
    if typename == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}
    status = dict(result.get("legacy") or {})
    core = result.get("core") or {}
    user = ((core.get("user_results") or {}).get("result") or {}).get("legacy") or {}
    if user:
        status["user"] = user
    if result.get("card", {}).get("legacy"):
        status["card"] = result["card"]["legacy"]
    quoted = ((result.get("quoted_status_result") or {}).get("result") or {}).get("legacy")
    if quoted:
        status["quoted_status"] = quoted
    retweeted = (((result.get("legacy") or {}).get("retweeted_status_result") or {}).get("result") or {}).get("legacy")
    if retweeted:
        status["retweeted_status"] = retweeted
        ruser = (((result.get("legacy") or {}).get("retweeted_status_result") or {}).get("result") or {}).get("core", {}).get("user_results", {}).get("result", {}).get("legacy")
        if ruser:
            status["retweeted_status"]["user"] = ruser
    views = result.get("views")
    if isinstance(views, dict) and views.get("count") is not None:
        status["view_count"] = views["count"]
    return status


def _graphql_status(twid):
    """Fetch the tweet through X's GraphQL API as a guest."""
    global _guest_token
    query = {
        "variables": json.dumps({
            "tweetId": twid,
            "withCommunity": False,
            "includePromotedContent": False,
            "withVoice": False,
        }, separators=(",", ":")),
        "features": json.dumps(_GRAPHQL_FEATURES, separators=(",", ":")),
        "fieldToggles": json.dumps({"withArticleRichContentState": False}, separators=(",", ":")),
    }
    url = _GRAPHQL_BASE + _GRAPHQL_ENDPOINT + "?" + urllib.parse.urlencode(query)
    for attempt in range(2):
        token = _fetch_guest_token()
        status, body = _http_json(url, headers={
            "Authorization": "Bearer " + _AUTH,
            "x-guest-token": token,
            "x-twitter-client-language": "en",
            "User-Agent": _BROWSER_UA,
        })
        if status in (401, 403) and attempt == 0:
            # Guest token may have expired; get a fresh one and retry once.
            _guest_token = None
            continue
        if status == 429:
            raise EngineError(429, "X rate-limited the request")
        if status != 200:
            errors = ", ".join(
                str(e.get("message", "")) for e in (body or {}).get("errors", [])
                if isinstance(e, dict)
            )
            raise EngineError(502, "X GraphQL error (HTTP %s): %s" % (status, errors or "unknown"))
        errors = (body or {}).get("errors") or []
        messages = ", ".join(str(e.get("message", "")) for e in errors if isinstance(e, dict))
        if messages and messages.lower() != "dependency: unspecified":
            raise EngineError(502, "X GraphQL error: %s" % messages)
        return _graphql_to_legacy(body, twid)
    raise EngineError(502, "X GraphQL error: guest token rejected")


# --------------------------------------------------------------------------
# Route 2: syndication endpoint (no auth)
# --------------------------------------------------------------------------

def syndication_token(twid):
    """The syndication token is a pure function of the tweet id:
    ((id / 1e15) * pi) written in base 36, with '0' and '.' removed."""
    n = (int(twid) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    whole = int(n)
    out = ""
    while whole > 0:
        whole, r = divmod(whole, 36)
        out = digits[r] + out
    out = out or "0"
    frac = n - int(n)
    tail = ""
    for _ in range(12):
        frac *= 36
        d = int(frac)
        frac -= d
        tail += digits[d]
    return (out + "." + tail).replace("0", "").replace(".", "")


def _syndication_status(twid):
    """Fetch the tweet from the public syndication endpoint (Googlebot UA)."""
    url = _SYNDICATION_URL + "?" + urllib.parse.urlencode({
        "id": twid,
        "token": syndication_token(twid),
    })
    status, body = _http_json(url, headers={"User-Agent": "Googlebot"})
    if status != 200 or not isinstance(body, dict) or not body:
        raise EngineError(502, "Syndication endpoint failed (HTTP %s)" % status)
    # Shape it like the legacy response so the rest of the code is shared.
    media = []
    for detail in (body.get("mediaDetails") or []):
        if isinstance(detail, dict):
            media.append(detail)
    for quoted in [body.get("quoted_tweet") or {}]:
        for detail in (quoted.get("mediaDetails") or []):
            if isinstance(detail, dict):
                media.append(detail)
    status_out = {
        "full_text": body.get("text") or "",
        "user": body.get("user") or {},
        "extended_entities": {"media": media},
        "favorite_count": body.get("favorite_count"),
    }
    return status_out


# --------------------------------------------------------------------------
# Shared: status -> videos / thumbnail / title
# --------------------------------------------------------------------------

def _media_from_status(status):
    """The tweet's own media, or the retweeted tweet's media."""
    status = status.get("retweeted_status") or status
    entities = status.get("extended_entities") or status.get("entities") or {}
    return [m for m in (entities.get("media") or []) if isinstance(m, dict)]


def _variants_to_videos(media):
    """Direct MP4 variants of one media item -> the site's video dicts."""
    candidates = []
    for variant in ((media.get("video_info") or {}).get("variants") or []):
        url = variant.get("url") or ""
        if not url or ".m3u8" in url:
            continue  # playlists, not files
        if (variant.get("content_type") or "") not in ("", "video/mp4"):
            continue
        width = height = None
        m = _DIM_RE.search(url)
        if m:
            width, height = int(m.group(1)), int(m.group(2))
        if width is None and media.get("width") and media.get("height"):
            # API v2 reports the video's real dimensions; use them when
            # the CDN URL carries none.
            width, height = int(media["width"]), int(media["height"])
        candidates.append({
            "url": url,
            "width": width,
            "height": height or 0,
            "tbr": int((variant.get("bitrate") or 0) / 1000) or 0,
        })
    # Best format per height (highest bitrate), sorted ascending.
    best = {}
    for f in candidates:
        cur = best.get(f["height"])
        if cur is None or f["tbr"] > cur["tbr"]:
            best[f["height"]] = f
    videos = []
    for height in sorted(best):
        f = best[height]
        width = f["width"]
        label = "%sx%s" % (width, height) if width else "%sp" % height
        videos.append({"label": label, "width": width, "height": height, "url": f["url"]})
    return videos


def _title_from_status(status):
    """'Uploader - tweet text', t.co links stripped, capped at 72 chars —
    the same shape yt-dlp returns for the site today."""
    text = (status.get("full_text") or status.get("text") or "").replace("\n", " ")
    text = _TCO_RE.sub("", text).strip()
    if len(text) > 72:
        text = text[:72]
    user = status.get("user") or {}
    name = user.get("name")
    return ("%s - %s" % (name, text)) if name else text


def _fetch_status(twid, route="auto"):
    """Get the tweet's legacy-shaped status via the requested route.

    auto chains: app (only when X_BEARER_TOKEN is set) -> graphql ->
    syndication. A route that fails with 402/429/502, or answers with no
    media, falls through to the next one. 400/404 are final: no other
    route can see a tweet this one says is gone.
    """
    global _last_route
    if route == "syndication":
        _last_route = "syndication"
        return _syndication_status(twid)
    if route == "graphql":
        _last_route = "graphql"
        return _graphql_status(twid)
    if route == "app":
        _last_route = "app"
        return _v2_status(twid)
    chain = []
    if os.environ.get("X_BEARER_TOKEN"):
        chain.append("app")
    chain += ["graphql", "syndication"]
    last_error = None
    for name in chain:
        try:
            if name == "app":
                status = _v2_status(twid)
            elif name == "graphql":
                status = _graphql_status(twid)
            else:
                status = _syndication_status(twid)
        except EngineError as e:
            if e.http_status in (402, 429, 502):
                last_error = e
                continue
            raise
        _last_route = name
        if _media_from_status(status):
            return status
        last_error = EngineError(404, NO_VIDEO_MESSAGE)
    raise last_error or EngineError(502, "All X routes failed")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def extract_direct_videos(url, route="auto"):
    """Same contract as the site's yt-dlp function: (videos, thumbnail,
    title). Raises EngineError(404) with the no-video wording when the
    post has no video, EngineError(400) for non-X links."""
    twid = parse_tweet_id(url)
    status = _fetch_status(twid, route=route)
    global _last_status
    _last_status = (twid, status)
    media = [m for m in _media_from_status(status)
             if m.get("type") in ("video", "animated_gif")]
    if not media:
        raise EngineError(404, NO_VIDEO_MESSAGE)
    # First video in the tweet (matches yt-dlp noplaylist behaviour).
    first = media[0]
    videos = _variants_to_videos(first)
    if not videos:
        raise EngineError(404, NO_VIDEO_MESSAGE)
    thumbnail = first.get("media_url_https") or first.get("media_url")
    return videos, thumbnail, _title_from_status(status)


def extract(url, route="auto"):
    """Full result dict, matching the site's /download JSON plus extras."""
    twid = parse_tweet_id(url)
    videos, thumbnail, title = extract_direct_videos(url, route=route)
    # Reuse the status extract_direct_videos just fetched — don't hit X twice.
    if _last_status and _last_status[0] == twid:
        status = _last_status[1]
    else:
        status = _fetch_status(twid, route=route)
    user = (status.get("retweeted_status") or status).get("user") or {}
    return {
        "videos": videos,
        "thumbnail": thumbnail,
        "title": title,
        "count": len(videos),
        "tweet_id": twid,
        "uploader": user.get("name"),
        "uploader_id": user.get("screen_name"),
        "engine": "twitter-engine/" + ENGINE_VERSION,
        "route": _last_route or route,
    }

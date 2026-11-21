"""Real-tweet tests for the TwitterDown engine.

Runs the engine against live X endpoints (syndication, GraphQL, auto)
and HEAD-checks the returned video links to prove they serve real files.

    python test_engine.py

Exit 0 when every case passes, 1 otherwise. Prints one line per check.
"""

import json
import sys
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from engine import EngineError, extract

GIF_TWEET = "https://x.com/GIPHY/status/1049802824555606017"
VIDEO_TWEETS = [
    "https://twitter.com/captainamerica/status/719944021058060289",
    "https://twitter.com/i/web/status/910031516746514432",
    "https://twitter.com/LisPower1/status/1001551623938805763",
]
PHOTO_TWEET = "https://twitter.com/giphz/status/657991469417025536/photo/1"
# Deleted tweet (X returns a bare TweetTombstone, no text) — regression test
# for the tombstone handling added 2026-11-21. NOTE: a bare tombstone does
# NOT always mean deleted — the same shape is returned for sensitive-media
# tweets hidden from guests (e.g. vdh3ud 2123518226911166491 is a real
# post, reason sensitiveMedia, visible only logged in).
TOMBSTONE_TWEET = "https://x.com/vdh3ud/status/2123518226911166491"

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + detail if detail and not cond else ""))
    if not cond:
        fails.append(name)


def head_video(url):
    """HEAD a video.twimg.com link: expect 200 + video/mp4."""
    req = urllib.request.Request(url, method="HEAD", headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers.get("Content-Type", ""), r.headers.get("Content-Length", "?")
    except Exception as e:
        return None, str(e), "?"


def test_tweet(url, route):
    label = "%s [%s]" % (url.split("/status/")[1][:12], route)
    try:
        res = extract(url, route=route)
    except EngineError as e:
        check(label + " extracts", False, "EngineError %s: %s" % (e.http_status, e.message))
        return None
    check(label + " extracts", True)
    check(label + " has videos", res["count"] >= 1, "count=%s" % res["count"])
    check(label + " labels sane", all(v["label"] and v["url"].startswith("https://video.twimg.com/") for v in res["videos"]))
    check(label + " has title", bool(res["title"]))
    check(label + " has thumbnail", bool(res["thumbnail"]))
    if res["videos"]:
        st, ct, ln = head_video(res["videos"][-1]["url"])
        check(label + " best link serves video", st == 200 and (ct or "").startswith("video/"),
              "HTTP %s %s (%s bytes)" % (st, ct, ln))
    print("     -> %s | %s | %s" % (
        ", ".join(v["label"] for v in res["videos"]),
        (res["title"] or "")[:60],
        res["thumbnail"] or ""))
    return res


def main():
    print("== GIF tweet, syndication route ==")
    test_tweet(GIF_TWEET, "syndication")
    print("== GIF tweet, graphql route ==")
    test_tweet(GIF_TWEET, "graphql")
    print("== GIF tweet, auto route ==")
    test_tweet(GIF_TWEET, "auto")

    print("== video tweets, auto route ==")
    for url in VIDEO_TWEETS:
        test_tweet(url, "auto")

    print("== error cases ==")
    try:
        extract("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        check("non-X URL rejected", False, "no error raised")
    except EngineError as e:
        check("non-X URL rejected", e.http_status == 400, "status=%s" % e.http_status)
    try:
        extract("not a url at all")
        check("garbage URL rejected", False, "no error raised")
    except EngineError as e:
        check("garbage URL rejected", e.http_status == 400, "status=%s" % e.http_status)
    try:
        extract(PHOTO_TWEET, route="syndication")
        check("photo-only tweet -> 404 no-video", False, "no error raised")
    except EngineError as e:
        check("photo-only tweet -> 404 no-video",
              e.http_status == 404 and "no video could be found" in e.message,
              "status=%s msg=%s" % (e.http_status, e.message))
    for route in ("auto", "graphql", "syndication"):
        try:
            extract(TOMBSTONE_TWEET, route=route)
            check("deleted tweet [%s] -> 404 no-video" % route, False, "no error raised")
        except EngineError as e:
            check("deleted tweet [%s] -> 404 no-video" % route,
                  e.http_status == 404 and "no video could be found" in e.message,
                  "status=%s msg=%s" % (e.http_status, e.message))

    print("== app route (X API v2, needs X_BEARER_TOKEN) ==")
    import os
    if not os.environ.get("X_BEARER_TOKEN"):
        print("SKIP app-route checks (no X_BEARER_TOKEN in environment)")
    else:
        try:
            extract(VIDEO_TWEETS[0], route="app")
            check("app route with depleted credits -> 402", False, "no error raised")
        except EngineError as e:
            check("app route with depleted credits -> 402",
                  e.http_status == 402 and "credits" in e.message,
                  "status=%s msg=%s" % (e.http_status, e.message))
        # auto must fall through the dead app route and still extract.
        res = test_tweet(VIDEO_TWEETS[0], "auto")
        if res is not None:
            check("auto with dead app route falls back", res.get("route") in ("graphql", "syndication"),
                  "served by %s" % res.get("route"))

    print()
    if fails:
        print("%d FAILURES: %s" % (len(fails), "; ".join(fails)))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

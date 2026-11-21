# TwitterDown engine — standalone X video extractor

A small, dependency-free replacement for the yt-dlp call inside TwitterDown.
Pure Python standard library: no packages to install, no API key, no X
account. **Standalone by design** — it lives here, outside the main
website, so the engine and the site can be tested independently. It is
NOT wired into the live site yet.

## Files

| File | What it is |
|---|---|
| `engine.py` | The extractor. Import `extract_direct_videos` / `extract`. |
| `cli.py` | `python cli.py <tweet-url> [--route auto\|graphql\|syndication]` → JSON on stdout. |
| `test_engine.py` | Real-tweet tests (runs against live X endpoints). `python test_engine.py` |

## How it works

Three routes in, tried in this order by `route="auto"`:

0. **App (X API v2)** — `GET https://api.twitter.com/2/tweets/<id>
   ?expansions=attachments.media_keys&media.fields=...` with the app's
   own Bearer token from the `X_BEARER_TOKEN` environment variable.
   Tried first when the token is set. Most reliable when the app has
   credits, and the best candidate for sensitive-media tweets guests
   cannot see. On 402 (credits depleted) / 429 / 401 / 403 it silently
   falls through to the guest routes, so downloads keep working.
1. **GraphQL** — `POST https://api.x.com/1.1/guest/activate.json` for an
   anonymous guest token, then `GET https://x.com/i/api/graphql/
   2ICDjqPd81tulZcYrtpTuQ/TweetResultByRestId` with the tweet id,
   feature flags and field toggles. Full metadata: title, uploader,
   all qualities, thumbnail.
2. **Syndication** — `GET https://cdn.syndication.twimg.com/tweet-result
   ?id=<id>&token=<token>` with `User-Agent: Googlebot`. No auth at all:
   the token is a pure function of the tweet id,
   `((id / 1e15) * π)` in base 36 with `0` and `.` stripped. Used when
   GraphQL is rate-limited (429), rejected, or returns no media.
   Less metadata, same video links.

Both routes return X's `video_info.variants`. Only direct MP4 files are
kept (`.m3u8` playlists are skipped). Dimensions come from the video URL
itself (`.../vid/1280x720/...mp4`); GIFs (`tweet_video`) carry none, so
they report width `null`, height `0`, label `0p` — exactly like the live
site today.

## Credentials (read this before deploying)

- The engine reads **one** value from the environment:
  `X_BEARER_TOKEN`. It is never written into any file in this folder,
  never printed, never logged.
- The app's **Consumer Key / Secret are not used**: app-only API calls
  need only the Bearer token. They matter only for OAuth 1.0a
  user-context calls, which this engine does not make. Keep them
  somewhere safe; do not paste them into code or chat again.
- **Status 2026-11-21: the app's credits are depleted.** The Bearer
  token itself is valid (X accepts the auth), but every v2 call returns
  HTTP 402 `{"detail": "credits depleted"}`. The engine handles that by
  falling back to the guest routes, so nothing breaks — but the app
  route returns no data until credits are topped up (or the monthly
  allowance replenishes) at developer.x.com. The v1.1
  `statuses/show.json` endpoint with the same Bearer token is a dead
  end (HTTP 404, empty body) — v2 is the only app route.
- Because of the 402, the sensitive-media unlock is **not yet proven**:
  app auth should return media for sensitive tweets (the web client's
  blur rule does not apply to the API), but that can only be verified
  once the app has credits. Re-test the sensitive tweet
  (`vdh3ud/status/2123518226911166491`) via `--route app` after a
  top-up: if it returns variants, the unlock is real.
- For the live site later, the token belongs in Render's Environment
  variables (same place the mail credentials would have gone), never
  in the repo. One extra caution: the token is billed per call, so the
  app route should stay behind the auto-fallback chain and the engine
  must not call v2 twice per tweet — `extract()` reuses the status it
  already fetched (see `_last_status`).

## Output contract (matches the live site's `/download`)

```python
videos, thumbnail, title = extract_direct_videos(url)
# videos: [{"label": "1280x720", "width": 1280, "height": 720,
#           "url": "https://video.twimg.com/...mp4"}, ...]
# sorted by quality ascending, best format per height (highest bitrate)
```

`extract(url)` returns the same plus `count`, `tweet_id`, `uploader`,
`uploader_id`, `engine` and `route`.

Errors raise `EngineError` with an `http_status` attribute:

| Status | Meaning |
|---|---|
| 400 | Not an x.com / twitter.com link, or unparseable |
| 404 | No video in the post (photos only, private, deleted, protected) — message is the site's existing no-video wording, so the site's `except` mapping works unchanged |
| 502 | Both X routes failed |

## Test results (2026-11-21, run from this machine)

`python test_engine.py` → **ALL CHECKS PASSED (36/36 without the app
token; 44/44 with `X_BEARER_TOKEN` set)**.

- GIF tweet via syndication, GraphQL and auto routes — 1 variant, label `0p`.
- 3 video tweets via auto — 3, 3 and 2 qualities (up to 1280x720).
- Every returned "best" link HEAD-checked: HTTP 200, `video/mp4`.
- Non-X URL → 400, garbage URL → 400, photo-only tweet → 404 no-video.
- App route (with `X_BEARER_TOKEN` set): forced `--route app` on a video
  tweet returns a clean 402 `X app credits depleted`; `auto` falls
  through the dead app route and still extracts via the guest routes.
  Without the token the app checks skip.
- Deleted tweet → 404 no-video on all three routes (regression test added
  2026-11-21 for https://x.com/vdh3ud/status/2123518226911166491 — X
  returns a bare `TweetTombstone` with no text; the engine used to miss
  that shape and fall through silently, now it raises the no-video 404
  explicitly. The live site agrees: yt-dlp returns the same 404).
  Correction: a bare tombstone does NOT always mean deleted. That same
  tweet turned out to be a real post hidden from guests as sensitive
  media (`reason: sensitiveMedia`, `BlurredMediaTombstone`, blurred photo
  placeholder only) — guests get no video data at all, so engine, yt-dlp
  and the live site all 404 identically. Guest access is the limit, not
  the extraction. Unlocking sensitive-media tweets needs a logged-in X
  session (cookies) — an open decision, see the maintenance notes.

Two things found while testing (both fixed in the engine):

- X answers **404 to the guest-token endpoint when the request carries
  urllib's default User-Agent** — a real browser UA is required even for
  this anonymous endpoint.
- Five of the old yt-dlp test tweets (2015–2019) no longer have any media
  on X at all; the engine correctly 404s them. Test vectors were swapped
  for tweets whose videos still exist.

## What it does NOT do (yet)

- **Multi-video tweets**: only the first video in a tweet is returned
  (matches yt-dlp `noplaylist` behaviour on the live site today).
- **Card / quoted video URLs** (`twitter.com/i/videos/tweet/...`,
  `i/cards/tfw/v1/...`): the site's `is_x_url` + yt-dlp handles these;
  the engine only parses `/status/<id>` links for now.
- **NSFW / login-gated tweets**: GraphQL returns `TweetUnavailable`
  (NSFW needs a logged-in viewer); the engine 404s them, same as the
  site does today without a login.
- No subtitle / HLS handling — the site only ever serves direct MP4s.

## Maintenance notes (read this before the swap)

X changes things every few months. The pieces most likely to break, in
order:

0. **The app's credits** — the app route is dead while credits are
   depleted (HTTP 402). Watch for it: if `auto` results start coming
   back served by `graphql`/`syndication` instead of `app`, check the
   credit balance at developer.x.com before debugging anything else.
1. **The GraphQL query id** (`2ICDjqPd81tulZcYrtpTuQ`) — X rotates it;
   when GraphQL starts failing, take the new id from the latest yt-dlp
   `twitter.py` (`TwitterIE._GRAPHQL_ENDPOINT`).
2. **The bearer tokens** (`_AUTH`, `_LEGACY_AUTH`) — public client
   credentials X ships in its own web app; same source as (1).
3. **The feature flags** (`_GRAPHQL_FEATURES`) — same source as (1).
4. **The syndication endpoint** — the most stable piece; it has worked
   unchanged for years and needs no token refresh.

If GraphQL breaks, `route="auto"` falls back to syndication
automatically, so downloads keep working with reduced metadata while
the query id is refreshed. The syndication-only mode (`--route
syndication`) is the emergency fallback.

## Integration plan (for later — NOT done yet)

When the engine is proven, the swap into the main site is small:

1. Copy `engine.py` into the `twitter` repo as a new module
   (the repo's Dockerfile copies files explicitly — add a `COPY`
   line for it, the same lesson as `translations.py`).
2. In `app.py`, replace the body of `extract_direct_videos` with a call
   to the engine's function of the same name and signature. Keep the
   `download_file_fallback` path untouched as the last resort.
3. Remove `yt-dlp` from `requirements.txt` once the engine has served
   live traffic for a while. (Keep it until then — one-line revert.)
4. Re-run the site's live checks: video post → 5 qualities, GIF post →
   MP4 via `/get`, photo post → 404 friendly message, non-X link → 400.
5. Afterwards, the Render image gets smaller and cold starts get faster
   (no yt-dlp import), and the `twitter-7vlf` service no longer depends
   on a third-party extractor's release cycle.

Do NOT integrate until the engine has been soaked: run `test_engine.py`
regularly for a week, and try it against fresh viral video tweets, not
just the old test vectors.

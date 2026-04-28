"""
Nepali Romanized Comment Scraper
=================================
Continuously scrapes comments from the most-commented Nepali YouTube videos,
keeping only comments that pass the Nepali language filter (see lang_filter.py).

Designed for unattended VPS / Raspberry Pi operation:
  • Saves progress after every page so a crash or reboot resumes mid-video.
  • On YouTube quota exhaustion it sleeps until midnight Pacific and resumes.
  • Skips videos already fully scraped.
  • All output also written to a rotating log file (scraper.log).
  • Intended to run as a systemd service (see scraper.service).

Output
------
Each video produces a single JSON file:  <OUTPUT_DIR>/<video_id>.json
The file contains one JSON array of comment objects with fields:
    video_id, comment_id, parent_id, author, text, text_clean,
    likes, published_at, updated_at, reply_count

  text        — raw text as returned by the YouTube API (may contain HTML
                entities such as &amp;, &#39;, <br>, and stray whitespace)
  text_clean  — normalized version: HTML entities decoded, <br>/<br/> tags
                replaced with newlines, zero-width/control characters removed,
                and runs of whitespace collapsed.  Use this field for NLP.

During scraping a companion <video_id>.jsonl file is written (one object per
line) so that partial progress survives a crash.  When the video is fully
scraped the JSONL is converted to a proper JSON array and removed.

Video selection
---------------
By default the scraper discovers the top N most-commented Nepali videos via
YouTube search (automatic discovery mode, controlled by TOP_N and
SEARCH_QUERIES).

To scrape a specific list of videos instead, supply their IDs via:
  • CLI:  python scrap.py --videos ID1 ID2 ID3 ...
  • Env:  VIDEO_IDS="ID1,ID2,ID3" python scrap.py
If both are provided the CLI argument takes precedence.

In manual-ID mode the automatic search phase is skipped entirely.  TOP_N is
still respected when using automatic discovery.

Rate-limit / quota strategy
----------------------------
The scraper distinguishes three classes of API failure:

  1. Daily quota exhaustion (403 quotaExceeded / dailyLimitExceeded)
     → sleep until midnight US/Pacific then resume.

  2. Transient per-minute rate limit (429 or 403 rateLimitExceeded /
     userRateLimitExceeded)
     → exponential back-off starting at 5 s, up to RATE_LIMIT_MAX_WAIT s,
        for at most TRANSIENT_MAX_RETRIES attempts before giving up.

  3. Transient server errors (5xx)
     → same exponential back-off / retry logic as case 2.

This prevents case 2 from incorrectly triggering an overnight sleep, while
still handling case 1 correctly.

Dependencies:
    pip install google-api-python-client python-dotenv lingua-language-detector
"""

import os
import re
import html
import json
import time
import logging
import datetime
import argparse
import googleapiclient.discovery
from googleapiclient.errors import HttpError
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

from lang_filter import NepaliFilter

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

# ---------------------------------------------------------------------------
# Required
# ---------------------------------------------------------------------------
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise ValueError("Missing YOUTUBE_API_KEY in .env or environment")

# ---------------------------------------------------------------------------
# Optional — output / discovery
# ---------------------------------------------------------------------------
# Comma-separated list of YouTube video IDs to scrape instead of
# running automatic search discovery (mirrors the --videos CLI flag).
# VIDEO_IDS env var is read later in resolve_video_ids(); listed here
# for documentation completeness.

TOP_N = int(os.getenv("TOP_N", "20"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "nepali_comments")
LOG_FILE = os.getenv("LOG_FILE", "scraper.log")

# Maximum number of search-result videos fetched per query (API max = 50).
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "50"))

# Enable/disable Nepali language filtering during scraping.
#   true  – keep only romanized/mixed Nepali comments (default)
#   false – download all comments; filtering deferred to ETL
FILTER_COMMENTS = os.getenv("FILTER_COMMENTS", "true").strip().lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Optional — language filtering
# ---------------------------------------------------------------------------
# Lingua confidence threshold (0.0–1.0) above which a comment is discarded
# as English or Spanish.  Higher = more aggressive discard.
LINGUA_CONFIDENCE_THRESHOLD = float(os.getenv("LINGUA_CONFIDENCE_THRESHOLD", "0.85"))

# ---------------------------------------------------------------------------
# Optional — rate-limit / retry tuning
# ---------------------------------------------------------------------------
# Maximum retries for transient errors (per-minute 429s, 5xx server errors).
# Each attempt waits 2× longer than the last (exponential back-off + jitter).
TRANSIENT_MAX_RETRIES = int(os.getenv("TRANSIENT_MAX_RETRIES", "6"))

# Initial back-off for the first transient retry, in seconds.
RATE_LIMIT_BASE_WAIT = float(os.getenv("RATE_LIMIT_BASE_WAIT", "5"))

# Hard ceiling on any single back-off sleep (caps exponential growth).
RATE_LIMIT_MAX_WAIT = float(os.getenv("RATE_LIMIT_MAX_WAIT", "120"))

# Pause between consecutive Phase 1 API calls (search queries, video-detail
# batches) to stay comfortably inside the per-minute quota.
INTER_REQUEST_DELAY = float(os.getenv("INTER_REQUEST_DELAY", "0.5"))

# Pause between comment-page fetches in Phase 2.
PAGE_FETCH_DELAY = float(os.getenv("PAGE_FETCH_DELAY", "0.2"))

SEARCH_QUERIES = [
    "नेपाली गीत",
    "नेपाली भिडियो",
    "नेपाली चलचित्र",
    "nepali comedy",
    "nepali music video 2024",
    "nepali movie official",
]

# ---------------------------------------------------------------------------
# Logging — writes to both console and a rotating log file
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("scraper")
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file: 5 MB × 3 backups = up to 15 MB of logs
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = setup_logging()

# ---------------------------------------------------------------------------
# Quota / rate-limit handling
#
# The YouTube Data API v3 has two distinct failure modes that look similar
# but require very different responses:
#
#   A) Daily quota exhaustion — reason: quotaExceeded / dailyLimitExceeded
#      The entire day's 10,000-unit budget is gone.  The only correct action
#      is to sleep until the quota resets at midnight US/Pacific.
#
#   B) Per-minute / per-user rate limit — reason: rateLimitExceeded /
#      userRateLimitExceeded, or a generic 429 with no structured body.
#      This is a short, transient back-pressure signal.  The correct action
#      is a brief exponential back-off (seconds, not hours).
#
#   C) Transient server error — 500 / 503.
#      Same short back-off as B; the request should be retried.
#
# The original code sent ALL of these to quota_sleep(), which would cause a
# ~24-hour stall whenever a simple per-minute limit was hit.  The reworked
# helpers below split these three cases cleanly.
# ---------------------------------------------------------------------------

# Canonical API reason strings that indicate the daily quota is fully spent.
_DAILY_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})

# Canonical reason strings for transient per-minute / per-user rate limits.
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


def _extract_reason(e: HttpError) -> str:
    """
    Return the first 'reason' string from a structured HttpError body, or ''
    if the body is absent or unparseable.

    Prefer e.error_details (googleapiclient populates this from the JSON body)
    over string-scanning e's repr, which can produce false matches if the
    video title or description happens to contain words like 'quota' or 'limit'.
    """
    try:
        return e.error_details[0].get("reason", "")
    except Exception:
        return ""


def is_daily_quota_error(e: HttpError) -> bool:
    """True only for genuine daily-quota exhaustion — should trigger quota_sleep()."""
    reason = _extract_reason(e)
    if reason in _DAILY_QUOTA_REASONS:
        return True
    # Some older API responses return 403 without a structured body; fall back
    # to status-only detection but ONLY for the known daily-quota status code.
    # We deliberately do NOT keyword-scan the message string here to avoid
    # false positives on rate-limit or user-rate-limit errors.
    return e.resp.status == 403 and not reason  # unknown 403 — treat as daily


def is_transient_error(e: HttpError) -> bool:
    """
    True for short, retriable failures:
      • Per-minute / per-user rate limits (429 or 403 with rate-limit reason)
      • Transient server errors (500, 502, 503, 504)
    These should trigger a brief exponential back-off, NOT quota_sleep().
    """
    reason = _extract_reason(e)
    if reason in _RATE_LIMIT_REASONS:
        return True
    # A bare 429 with no structured body is always a transient rate limit.
    if e.resp.status == 429:
        return True
    # 5xx errors are transient server-side failures worth retrying.
    if e.resp.status in (500, 502, 503, 504):
        return True
    return False


def _seconds_until_quota_reset() -> float:
    """Seconds until midnight US/Pacific (Google's daily quota reset) + 5 min buffer."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # Python < 3.9
    pacific = ZoneInfo("America/Los_Angeles")
    now_pt = datetime.datetime.now(tz=pacific)
    tomorrow_midnight = (now_pt + datetime.timedelta(days=1)).replace(
        hour=0, minute=5, second=0, microsecond=0
    )
    return max(60, (tomorrow_midnight - now_pt).total_seconds())


def quota_sleep() -> None:
    """Sleep until the daily quota resets, logging progress every 30 minutes."""
    secs = _seconds_until_quota_reset()
    wake = datetime.datetime.now() + datetime.timedelta(seconds=secs)
    log.warning(
        "Daily quota exhausted. Sleeping %.1f h — resuming at %s local time.",
        secs / 3600,
        wake.strftime("%Y-%m-%d %H:%M:%S"),
    )
    while secs > 0:
        chunk = min(secs, 1800)
        time.sleep(chunk)
        secs -= chunk
        if secs > 0:
            log.info("[QUOTA WAIT] %.1f h remaining until reset.", secs / 3600)
    log.info("[QUOTA] Resuming scrape now.")


def transient_backoff(attempt: int, context: str) -> None:
    """
    Sleep for an exponentially increasing delay, capped at RATE_LIMIT_MAX_WAIT.

    attempt=0 → RATE_LIMIT_BASE_WAIT seconds
    attempt=1 → 2× base
    attempt=2 → 4× base  … up to RATE_LIMIT_MAX_WAIT

    A small random jitter (±10 %) is added to prevent a fleet of parallel
    scrapers from re-colliding on the API at exactly the same instant.
    """
    import random
    delay = min(RATE_LIMIT_BASE_WAIT * (2**attempt), RATE_LIMIT_MAX_WAIT)
    jitter = delay * random.uniform(-0.1, 0.1)
    total = delay + jitter
    log.warning(
        "[RATE LIMIT] %s — back-off attempt %d/%d, sleeping %.1f s.",
        context,
        attempt + 1,
        TRANSIENT_MAX_RETRIES,
        total,
    )
    time.sleep(total)


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _checkpoint_path(video_id: str) -> str:
    return os.path.join(OUTPUT_DIR, f"{video_id}.checkpoint.json")


def load_checkpoint(video_id: str) -> dict:
    path = _checkpoint_path(video_id)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"next_page_token": None, "pages_done": 0, "comments_kept": 0}


def save_checkpoint(
    video_id: str, next_page_token, pages_done: int, comments_kept: int
) -> None:
    with open(_checkpoint_path(video_id), "w", encoding="utf-8") as f:
        json.dump(
            {
                "next_page_token": next_page_token,
                "pages_done": pages_done,
                "comments_kept": comments_kept,
            },
            f,
        )


def clear_checkpoint(video_id: str) -> None:
    p = _checkpoint_path(video_id)
    if os.path.exists(p):
        os.remove(p)


def is_fully_scraped(video_id: str) -> bool:
    return os.path.exists(os.path.join(OUTPUT_DIR, f"{video_id}.done"))


def mark_done(video_id: str) -> None:
    with open(os.path.join(OUTPUT_DIR, f"{video_id}.done"), "w") as f:
        f.write("done")


# ---------------------------------------------------------------------------
# JSON output helpers
#
# Strategy: during scraping we append newline-delimited JSON (JSONL) to
# <video_id>.jsonl — one comment object per line.  This is crash-safe
# (plain append, no need to rewrite the whole file).  When a video is fully
# scraped, finalize_json() reads the JSONL, wraps it in a JSON array, writes
# <video_id>.json, and deletes the JSONL.  If a crash occurs mid-video the
# JSONL survives and is simply appended to on resume.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Comment text cleaning
# ---------------------------------------------------------------------------
# YouTube's plainText format still contains HTML entities (e.g. &amp; &#39;)
# and occasionally <br> / <br/> line-break tags.  We preserve the original
# API text in the 'text' field and add a 'text_clean' field with a normalized
# version that is more suitable for NLP pipelines.

# Characters to strip: zero-width space, zero-width non-joiner, BOM,
# soft hyphen, and other invisible Unicode formatting characters.
_INVISIBLE_CHARS_RE = re.compile(r"[​‌‍‎‏﻿­  ]")

# Collapse any run of whitespace (spaces, tabs, form-feeds) — but NOT newlines
# — into a single space.  Newlines are preserved because they carry structure
# in multi-line comments.
_WHITESPACE_RUN_RE = re.compile(r"[^\S\n]+")


def clean_comment_text(raw: str) -> str:
    """
    Normalize a raw YouTube comment string for NLP use.

    Steps applied in order:
      1. Replace <br> and <br/> tags (case-insensitive) with a real newline.
         YouTube's 'plainText' format uses these for line breaks even though
         it is otherwise tag-free.
      2. Decode all HTML entities (&amp; → &, &#39; → ', &lt; → <, etc.)
         using Python's html.unescape(), which handles both named and numeric
         references.
      3. Strip invisible / zero-width Unicode formatting characters that are
         common in copy-pasted social-media text and have no linguistic value.
      4. Collapse runs of horizontal whitespace (spaces, tabs) into one space
         while preserving intentional newlines.
      5. Strip leading and trailing whitespace from the whole string and from
         each individual line.
    """
    # 1. <br> / <br/> → newline (YouTube plainText still emits these)
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)

    # 2. Decode HTML entities: &amp; &lt; &#39; &nbsp; etc.
    text = html.unescape(text)

    # 3. Remove invisible Unicode formatting characters
    text = _INVISIBLE_CHARS_RE.sub("", text)

    # 4. Collapse horizontal whitespace runs to a single space
    text = _WHITESPACE_RUN_RE.sub(" ", text)

    # 5. Strip each line, then strip the whole string
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip()


# ---------------------------------------------------------------------------
# JSON output helpers
#
# During scraping we append newline-delimited JSON (JSONL) to <video_id>.jsonl
# — one comment object per line.  This is crash-safe (plain append, no need to
# rewrite the whole file on every page).  When a video is fully scraped,
# finalize_json() reads the JSONL, wraps it into a proper JSON array, writes
# <video_id>.json, and removes the scratch file.  If the process crashes
# mid-video the JSONL survives intact and is simply appended to on resume.
# ---------------------------------------------------------------------------

# Canonical output fields written to every comment object, in stable order.
# text       — raw API text (may contain HTML entities and <br> tags)
# text_clean — normalized version suitable for NLP (entities decoded, <br>
#              expanded to newlines, invisible chars stripped, whitespace
#              collapsed)
JSON_FIELDNAMES = [
    "video_id",
    "comment_id",
    "parent_id",
    "author",
    "text",
    "text_clean",
    "likes",
    "published_at",
    "updated_at",
    "reply_count",
]


def _jsonl_path(video_id: str) -> str:
    """Path to the crash-safe JSONL scratch file written during scraping."""
    return os.path.join(OUTPUT_DIR, f"{video_id}.jsonl")


def _json_path(video_id: str) -> str:
    """Path to the final JSON array file produced after a video is fully scraped."""
    return os.path.join(OUTPUT_DIR, f"{video_id}.json")


def append_comments_to_jsonl(video_id: str, comments: list) -> None:
    """Append comment dicts to the JSONL scratch file (one JSON object per line)."""
    path = _jsonl_path(video_id)
    with open(path, "a", encoding="utf-8") as f:
        for comment in comments:
            # Write only the canonical fields in a stable order
            row = {k: comment[k] for k in JSON_FIELDNAMES}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def finalize_json(video_id: str) -> str:
    """
    Convert the scratch JSONL into a proper JSON array file.

    Reads every line from <video_id>.jsonl, collects the objects into a list,
    writes <video_id>.json, then removes the JSONL.  Returns the output path.
    """
    jsonl_path = _jsonl_path(video_id)
    json_path = _json_path(video_id)

    comments: list[dict] = []
    if os.path.exists(jsonl_path):
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    comments.append(json.loads(line))

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(comments, f, ensure_ascii=False, indent=2)

    # Remove the scratch file now that the canonical JSON exists
    if os.path.exists(jsonl_path):
        os.remove(jsonl_path)

    return json_path


# ---------------------------------------------------------------------------
# Phase 1: collect top Nepali videos
# ---------------------------------------------------------------------------

def _api_call_with_retry(call, context: str):
    """
    Execute a single YouTube API callable with unified error handling.

    Retry logic:
      • Daily quota exhaustion → quota_sleep() then retry indefinitely
        (the quota will eventually reset and the run should continue).
      • Transient rate limit or server error → exponential back-off up to
        TRANSIENT_MAX_RETRIES attempts, then raise so the caller can decide
        whether to skip the item or abort.
      • Any other HttpError → re-raised immediately (e.g. 404 Not Found,
        400 Bad Request — these are programmer errors, not transient).

    Parameters
    ----------
    call    : a zero-argument callable that executes the API request,
              e.g. ``lambda: youtube.search().list(...).execute()``
    context : short human-readable label used in log messages.
    """
    transient_attempts = 0
    while True:
        try:
            return call()
        except HttpError as e:
            if is_daily_quota_error(e):
                # Daily budget gone — sleep until reset, then retry.
                quota_sleep()
                transient_attempts = 0  # reset after a full night's sleep
            elif is_transient_error(e):
                if transient_attempts >= TRANSIENT_MAX_RETRIES:
                    log.error(
                        "[RETRY] %s: exceeded %d transient retries. Re-raising.",
                        context,
                        TRANSIENT_MAX_RETRIES,
                    )
                    raise
                transient_backoff(transient_attempts, context)
                transient_attempts += 1
            else:
                # Non-retriable error (404, 400, unexpected 403, …) — let caller handle.
                raise


def fetch_video_ids_for_query(youtube, query: str) -> list[str]:
    """Search for videos matching query and return their IDs."""
    resp = _api_call_with_retry(
        lambda: youtube.search()
        .list(
            part="id",
            type="video",
            q=query,
            relevanceLanguage="ne",
            regionCode="NP",
            maxResults=MAX_RESULTS,
            order="viewCount",
        )
        .execute(),
        context=f"search '{query}'",
    )
    return [item["id"]["videoId"] for item in resp.get("items", [])]


def fetch_video_details(youtube, video_ids: list[str]) -> list:
    """
    Fetch snippet + statistics for up to len(video_ids) videos.
    Batches requests in groups of 50 (API maximum) with INTER_REQUEST_DELAY
    between batches to avoid bursting the per-minute quota.
    """
    all_videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = _api_call_with_retry(
            lambda b=batch: youtube.videos()
            .list(part="snippet,statistics", id=",".join(b))
            .execute(),
            context=f"videos.list batch {i // 50 + 1}",
        )
        all_videos.extend(resp.get("items", []))
        if i + 50 < len(video_ids):
            # Brief pause between batches — stays inside per-minute limits.
            time.sleep(INTER_REQUEST_DELAY)
    return all_videos


def is_nepali_video(video: dict) -> bool:
    snippet = video.get("snippet", {})
    audio_lang = snippet.get("defaultAudioLanguage", "")
    default_lang = snippet.get("defaultLanguage", "")
    if audio_lang.startswith("ne") or default_lang.startswith("ne"):
        return True
    if audio_lang and not audio_lang.startswith("ne"):
        return False
    combined = snippet.get("title", "") + snippet.get("description", "")
    return any("\u0900" <= ch <= "\u097f" for ch in combined) or True


def collect_nepali_videos(youtube) -> list[str]:
    """
    Run each SEARCH_QUERIES entry through the YouTube search API and collect
    unique video IDs.  A brief pause between queries prevents burst-rate errors.
    """
    seen, all_ids = set(), []
    for idx, query in enumerate(SEARCH_QUERIES):
        log.info("Searching: '%s'", query)
        ids = fetch_video_ids_for_query(youtube, query)
        new = [v for v in ids if v not in seen]
        seen.update(new)
        all_ids.extend(new)
        log.info("  -> %d new video IDs (pool: %d)", len(new), len(all_ids))
        # Pause between search queries — the search.list method costs 100 units;
        # firing them back-to-back can hit the per-minute user-rate limit.
        if idx < len(SEARCH_QUERIES) - 1:
            time.sleep(INTER_REQUEST_DELAY)
    return all_ids


def get_top_commented(videos: list) -> list[dict]:
    filtered = []
    for v in videos:
        if not is_nepali_video(v):
            continue
        cc = int(v["statistics"].get("commentCount", 0))
        if cc == 0:
            continue
        filtered.append(
            {
                "title": v["snippet"]["title"],
                "channel": v["snippet"]["channelTitle"],
                "video_id": v["id"],
                "comment_count": cc,
                "view_count": int(v["statistics"].get("viewCount", 0)),
                "language": v["snippet"].get("defaultAudioLanguage")
                or v["snippet"].get("defaultLanguage", "untagged"),
            }
        )
    filtered.sort(key=lambda x: x["comment_count"], reverse=True)
    return filtered[:TOP_N]


def build_video_stubs(youtube, video_ids: list[str]) -> list[dict]:
    """
    Fetch titles and statistics for an explicit list of video IDs and return
    the same dict shape used by get_top_commented().  Used in manual-ID mode.
    """
    raw = fetch_video_details(youtube, video_ids)
    stubs = []
    id_set = set(video_ids)
    for v in raw:
        if v["id"] not in id_set:
            continue
        cc = int(v["statistics"].get("commentCount", 0))
        stubs.append(
            {
                "title": v["snippet"]["title"],
                "channel": v["snippet"]["channelTitle"],
                "video_id": v["id"],
                "comment_count": cc,
                "view_count": int(v["statistics"].get("viewCount", 0)),
                "language": v["snippet"].get("defaultAudioLanguage")
                or v["snippet"].get("defaultLanguage", "untagged"),
            }
        )
    # Preserve the caller-supplied order
    order = {vid: i for i, vid in enumerate(video_ids)}
    stubs.sort(key=lambda x: order.get(x["video_id"], 9999))
    return stubs


# ---------------------------------------------------------------------------
# Phase 2: scrape comments with checkpointing + quota handling
# ---------------------------------------------------------------------------


def scrape_video_comments(
    youtube, video: dict, video_index: int, total_videos: int, lang_filter
) -> None:
    vid_id = video["video_id"]
    title = video["title"]
    short = title[:55]

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if is_fully_scraped(vid_id):
        log.info("[%d/%d] SKIP (already done): %s", video_index, total_videos, short)
        return

    ckpt = load_checkpoint(vid_id)
    next_page_token = ckpt["next_page_token"]
    pages_done = ckpt["pages_done"]
    comments_kept = ckpt["comments_kept"]

    if pages_done > 0:
        log.info(
            "[%d/%d] RESUME from page %d (%d kept so far): %s",
            video_index,
            total_videos,
            pages_done + 1,
            comments_kept,
            short,
        )
    else:
        log.info(
            "[%d/%d] START scraping (~%d comments expected): %s",
            video_index,
            total_videos,
            video["comment_count"],
            short,
        )

    pages_this_session = 0
    kept_this_session = 0

    while True:
        # ------------------------------------------------------------------
        # Fetch one page of comment threads via the unified retry helper.
        # We must save the checkpoint BEFORE entering the retry loop so that
        # if a daily quota sleep fires mid-video the resume token is on disk.
        # ------------------------------------------------------------------
        save_checkpoint(vid_id, next_page_token, pages_done, comments_kept)

        try:
            token = next_page_token  # capture for the lambda closure
            resp = _api_call_with_retry(
                lambda t=token: youtube.commentThreads()
                .list(
                    part="snippet,replies",
                    videoId=vid_id,
                    maxResults=100,
                    pageToken=t,
                    textFormat="plainText",
                    order="time",
                )
                .execute(),
                context=f"commentThreads.list page {pages_done + 1} for '{short}'",
            )
        except HttpError as e:
            # A 403 that is NOT a quota/rate error means comments are disabled
            # for this video.  Mark it done so we never attempt it again.
            if e.resp.status == 403 and not is_transient_error(e):
                log.warning(
                    "[%d/%d] Comments disabled for '%s'. Marking done.",
                    video_index,
                    total_videos,
                    short,
                )
                mark_done(vid_id)
                clear_checkpoint(vid_id)
                return
            # All other non-retriable errors: log and skip this video.
            log.error(
                "[%d/%d] HTTP %s for '%s': %s. Skipping video.",
                video_index,
                total_videos,
                e.resp.status,
                short,
                e,
            )
            return

        page_comments = []
        page_total = 0
        page_discarded = 0

        for thread in resp.get("items", []):
            top_snip = thread["snippet"]["topLevelComment"]["snippet"]
            top_text = top_snip.get("textDisplay", "")
            page_total += 1

            # Keep comment if filtering is OFF or it passes the Nepali check
            if not FILTER_COMMENTS or (lang_filter and lang_filter.is_nepali(top_text)):
                page_comments.append(
                    {
                        "video_id": vid_id,
                        "comment_id": thread["snippet"]["topLevelComment"]["id"],
                        "parent_id": "",
                        "author": top_snip.get("authorDisplayName", ""),
                        "text": top_text,
                        "text_clean": clean_comment_text(top_text),
                        "likes": top_snip.get("likeCount", 0),
                        "published_at": top_snip.get("publishedAt", ""),
                        "updated_at": top_snip.get("updatedAt", ""),
                        "reply_count": thread["snippet"].get("totalReplyCount", 0),
                    }
                )
            else:
                page_discarded += 1

            for reply in thread.get("replies", {}).get("comments", []):
                r_snip = reply["snippet"]
                r_text = r_snip.get("textDisplay", "")
                page_total += 1
                if not FILTER_COMMENTS or (lang_filter and lang_filter.is_nepali(r_text)):
                    page_comments.append(
                        {
                            "video_id": vid_id,
                            "comment_id": reply["id"],
                            "parent_id": thread["snippet"]["topLevelComment"]["id"],
                            "author": r_snip.get("authorDisplayName", ""),
                            "text": r_text,
                            "text_clean": clean_comment_text(r_text),
                            "likes": r_snip.get("likeCount", 0),
                            "published_at": r_snip.get("publishedAt", ""),
                            "updated_at": r_snip.get("updatedAt", ""),
                            "reply_count": 0,
                        }
                    )
                else:
                    page_discarded += 1

        # Append this page's kept comments to the crash-safe JSONL scratch file.
        if page_comments:
            append_comments_to_jsonl(vid_id, page_comments)

        next_page_token = resp.get("nextPageToken")
        pages_done += 1
        comments_kept += len(page_comments)
        pages_this_session += 1
        kept_this_session += len(page_comments)

        # Overwrite checkpoint with the updated page token so a resume picks
        # up exactly where we left off.
        save_checkpoint(vid_id, next_page_token, pages_done, comments_kept)

        log.debug(
            "[%d/%d] page %d | fetched %d | kept %d | discarded %d | total kept %d",
            video_index,
            total_videos,
            pages_done,
            page_total,
            len(page_comments),
            page_discarded,
            comments_kept,
        )

        if pages_done % 10 == 0:
            log.info(
                "[%d/%d] Progress: page %d | %d romanized-Nepali comments kept so far",
                video_index,
                total_videos,
                pages_done,
                comments_kept,
            )

        if not next_page_token:
            break

        # Brief pause between pages — keeps well inside the per-minute limit.
        time.sleep(PAGE_FETCH_DELAY)

    # Convert JSONL → final JSON array now that all pages are done
    out_path = finalize_json(vid_id)

    mark_done(vid_id)
    clear_checkpoint(vid_id)
    log.info(
        "[%d/%d] DONE: %d kept (%d this session, %d pages) -> %s",
        video_index,
        total_videos,
        comments_kept,
        kept_this_session,
        pages_this_session,
        out_path,
    )


# ---------------------------------------------------------------------------
# Summary + display
# ---------------------------------------------------------------------------

def save_summary(top_videos: list) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(top_videos, f, ensure_ascii=False, indent=2)
    return path


def log_top_videos(top_videos: list) -> None:
    log.info("=" * 70)
    log.info("TOP %d MOST COMMENTED NEPALI VIDEOS", len(top_videos))
    log.info("=" * 70)
    for i, v in enumerate(top_videos, 1):
        done = " [DONE]" if is_fully_scraped(v["video_id"]) else ""
        log.info("%2d. %s%s", i, v["title"][:65], done)
        log.info(
            "    Channel: %s | Comments: %s | Views: %s",
            v["channel"],
            f"{v['comment_count']:,}",
            f"{v['view_count']:,}",
        )


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nepali Romanized Comment Scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Video selection examples:
  python scrap.py --videos dQw4w9WgXcQ abc123
  VIDEO_IDS="dQw4w9WgXcQ,abc123" python scrap.py""",
    )
    parser.add_argument(
        "--videos",
        metavar="VIDEO_ID",
        nargs="+",
        help="One or more YouTube video IDs to scrape (skips automatic discovery).",
    )
    return parser.parse_args()


def resolve_video_ids(args: argparse.Namespace) -> list[str] | None:
    """
    Return an explicit list of video IDs to scrape, or None to use auto-discovery.

    Priority: CLI --videos  >  VIDEO_IDS env var  >  None (auto-discovery).
    """
    if args.videos:
        ids = [v.strip() for v in args.videos if v.strip()]
        log.info("Manual mode: %d video ID(s) supplied via --videos.", len(ids))
        return ids

    env_val = os.getenv("VIDEO_IDS", "").strip()
    if env_val:
        ids = [v.strip() for v in env_val.split(",") if v.strip()]
        log.info("Manual mode: %d video ID(s) supplied via VIDEO_IDS env var.", len(ids))
        return ids

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    log.info("=" * 70)
    log.info("Nepali Romanized Comment Scraper starting up")
    if FILTER_COMMENTS:
        log.info(
            "Output dir: %s  |  Log: %s  |  Discard-if-EN/ES threshold: %.0f%%",
            OUTPUT_DIR,
            LOG_FILE,
            LINGUA_CONFIDENCE_THRESHOLD * 100,
        )
    else:
        log.info(
            "Output dir: %s  |  Log: %s  |  Filtering DISABLED (downloading all comments)",
            OUTPUT_DIR,
            LOG_FILE,
        )
    log.info("=" * 70)

    # Load language filter only when filtering is enabled (saves ~1 GB RAM)
    if FILTER_COMMENTS:
        lang_filter = NepaliFilter(threshold=LINGUA_CONFIDENCE_THRESHOLD)
    else:
        lang_filter = None

    youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=API_KEY)

    explicit_ids = resolve_video_ids(args)

    if explicit_ids:
        log.info("Fetching video details for %d explicit video ID(s)...", len(explicit_ids))
        top_videos = build_video_stubs(youtube, explicit_ids)
        if not top_videos:
            log.error("Could not retrieve details for any of the supplied video IDs. Exiting.")
            return
    else:
        log.info("Phase 1: collecting Nepali video IDs...")
        all_video_ids = collect_nepali_videos(youtube)

        log.info("Phase 2: fetching video details for %d videos...", len(all_video_ids))
        videos = fetch_video_details(youtube, all_video_ids)
        top_videos = get_top_commented(videos)

        if not top_videos:
            log.error("No Nepali videos found. Exiting.")
            return

    log_top_videos(top_videos)
    save_summary(top_videos)

    already_done = sum(1 for v in top_videos if is_fully_scraped(v["video_id"]))
    log.info(
        "Scraping comments (%d/%d videos already complete).",
        already_done,
        len(top_videos),
    )
    log.info("Auto-pauses on quota exhaustion and resumes next day. Safe to restart.")

    for i, video in enumerate(top_videos, 1):
        scrape_video_comments(youtube, video, i, len(top_videos), lang_filter)

    log.info("All videos processed. Run again to pick up any new comments.")


if __name__ == "__main__":
    main()
"""
Nepali Romanized Comment Scraper
=================================
Continuously scrapes comments from the most-commented Nepali YouTube videos,
keeping only comments written in romanized (Latin-script) Nepali.

Filter logic (using lingua-language-detector):
  • Fully Devanagari comments (zero Latin words)          → discard
  • Lingua detects ENGLISH with high confidence           → discard
  • Lingua detects SPANISH with high confidence           → discard
  • Everything else (Nepali, uncertain, ambiguous)        → KEEP
    (romanized Nepali is not a lingua language, so it shows up as uncertain;
     Devanagari-script Nepali is kept because it IS Nepali content —
     only the script-check above removes purely-Devanagari comments)

All 75 Lingua language models are loaded (requires ~1 GB RAM) so the
detector has the full comparison set and gives more honest "uncertain"
results for romanized Nepali rather than being forced to pick between
a tiny set of languages.

Designed for unattended VPS / Raspberry Pi operation:
  • Saves progress after every page so a crash or reboot resumes mid-video.
  • On YouTube quota exhaustion it sleeps until midnight Pacific and resumes.
  • Skips videos already fully scraped.
  • All output also written to a rotating log file (scraper.log).
  • Intended to run as a systemd service (see scraper.service).

Dependencies:
    pip install google-api-python-client python-dotenv lingua-language-detector
"""

import os
import csv
import json
import re
import time
import logging
import datetime
import googleapiclient.discovery
from googleapiclient.errors import HttpError
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

from lingua import Language, LanguageDetectorBuilder  # pip install lingua-language-detector

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise ValueError("Missing YOUTUBE_API_KEY in .env")

TOP_N       = 20
MAX_RESULTS = 50
OUTPUT_DIR  = "nepali_comments"
LOG_FILE    = "scraper.log"

# Confidence threshold: if Lingua's top result for English or Spanish is
# >= this value we treat the comment as that language and discard it.
# 0.85 is intentionally strict — we'd rather keep a borderline English
# comment than accidentally discard a romanized Nepali one.
LINGUA_CONFIDENCE_THRESHOLD = 0.85

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

def setup_logging():
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
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3,
                             encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = setup_logging()

# ---------------------------------------------------------------------------
# Lingua detector — load ALL 75 languages for maximum accuracy
# ---------------------------------------------------------------------------
#
# With 1.6 GB available RAM this server can comfortably hold all lingua
# models (~1 GB).  Loading all languages is better than a small subset
# because Lingua can then say "I genuinely don't know" for romanized Nepali
# rather than being forced to pick between only 3 choices.  The more
# languages it has to compare against, the more honest its uncertainty is.

log.info("Loading Lingua language detector (all languages)...")
_detector = (
    LanguageDetectorBuilder
    .from_all_languages()
    .with_minimum_relative_distance(0.1)  # require at least 10% gap between top-2
    .build()
)
log.info("Lingua detector ready.")

# ---------------------------------------------------------------------------
# Comment language filter
# ---------------------------------------------------------------------------

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]+")


def _latin_words(text):
    """Return Latin-script words only (strips Devanagari first)."""
    latin_only = _DEVANAGARI_RE.sub(" ", text)
    return re.findall(r"[a-zA-Z']+", latin_only)


def _devanagari_words(text):
    return _DEVANAGARI_RE.findall(text)


def is_romanized_nepali(text):
    """
    Return True if the comment should be kept.

    Lingua's job here is ONLY to identify and discard comments that are
    clearly NOT Nepali (i.e. confidently English or Spanish).  If Lingua
    says Nepali, or is uncertain, or picks any other language — we keep it.

    Decision pipeline:
    ┌──────────────────────────────────────────────────────────────────┐
    │ 1. Empty?                               → DISCARD               │
    │ 2. Has Devanagari but zero Latin words  → DISCARD               │
    │    (purely Devanagari script)                                    │
    │ 3. Zero Latin words (emoji/nums only)   → DISCARD               │
    │ 4. Strip Devanagari; run Lingua on      → ENGLISH confident      │
    │    the Latin-only portion                 → DISCARD             │
    │                                         → SPANISH confident      │
    │                                           → DISCARD             │
    │                                         → anything else          │
    │                                           → KEEP                │
    └──────────────────────────────────────────────────────────────────┘

    Step 4 operates only on the Latin portion of mixed comments so that
    a comment like "yo song राम्रो cha bro" is evaluated as "yo song cha bro"
    — stripping Devanagari before sending to Lingua avoids confusing the
    detector with a mixed-script input.
    """
    stripped = text.strip()

    # 1. Empty
    if not stripped:
        return False

    deva  = _devanagari_words(stripped)
    latin = _latin_words(stripped)

    # 2. Purely Devanagari (no Latin at all) → discard
    if deva and not latin:
        return False

    # 3. No Latin letters (emoji/number-only) → discard
    if not latin:
        return False

    # 4. Run Lingua on the Latin-only portion
    latin_text  = " ".join(latin)
    confidences = _detector.compute_language_confidence_values(latin_text)
    conf_map    = {result.language: result.value for result in confidences}

    # Only discard if confidently English or Spanish
    if conf_map.get(Language.ENGLISH, 0) >= LINGUA_CONFIDENCE_THRESHOLD:
        return False
    if conf_map.get(Language.SPANISH, 0) >= LINGUA_CONFIDENCE_THRESHOLD:
        return False

    # Nepali, uncertain, any other language → KEEP
    return True


# ---------------------------------------------------------------------------
# Quota / rate-limit handling
# ---------------------------------------------------------------------------

def _seconds_until_quota_reset():
    """Google resets quotas at midnight US/Pacific. Return seconds until then + 5 min buffer."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # Python < 3.9
    pacific           = ZoneInfo("America/Los_Angeles")
    now_pt            = datetime.datetime.now(tz=pacific)
    tomorrow_midnight = (now_pt + datetime.timedelta(days=1)).replace(
        hour=0, minute=5, second=0, microsecond=0
    )
    return max(60, (tomorrow_midnight - now_pt).total_seconds())


def quota_sleep():
    secs = _seconds_until_quota_reset()
    wake = datetime.datetime.now() + datetime.timedelta(seconds=secs)
    log.warning("Daily quota exhausted. Sleeping %.1f h — resuming at %s local time.",
                secs / 3600, wake.strftime("%Y-%m-%d %H:%M:%S"))
    while secs > 0:
        chunk = min(secs, 1800)
        time.sleep(chunk)
        secs -= chunk
        if secs > 0:
            log.info("[QUOTA WAIT] %.1f h remaining until reset.", secs / 3600)
    log.info("[QUOTA] Resuming scrape now.")


def is_quota_error(http_error):
    try:
        reason = http_error.error_details[0].get("reason", "")
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            return True
    except Exception:
        pass
    msg = str(http_error).lower()
    return http_error.resp.status in (429, 403) and (
        "quota" in msg or "limit" in msg or "rate" in msg
    )


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _checkpoint_path(video_id):
    return os.path.join(OUTPUT_DIR, f"{video_id}.checkpoint.json")


def load_checkpoint(video_id):
    path = _checkpoint_path(video_id)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"next_page_token": None, "pages_done": 0, "comments_kept": 0}


def save_checkpoint(video_id, next_page_token, pages_done, comments_kept):
    with open(_checkpoint_path(video_id), "w", encoding="utf-8") as f:
        json.dump({"next_page_token": next_page_token,
                   "pages_done": pages_done,
                   "comments_kept": comments_kept}, f)


def clear_checkpoint(video_id):
    p = _checkpoint_path(video_id)
    if os.path.exists(p):
        os.remove(p)


def is_fully_scraped(video_id):
    return os.path.exists(os.path.join(OUTPUT_DIR, f"{video_id}.done"))


def mark_done(video_id):
    with open(os.path.join(OUTPUT_DIR, f"{video_id}.done"), "w") as f:
        f.write("done")


# ---------------------------------------------------------------------------
# CSV append helper
# ---------------------------------------------------------------------------

CSV_FIELDNAMES = ["video_id", "comment_id", "parent_id", "author",
                  "text", "likes", "published_at", "updated_at", "reply_count"]


def _csv_path(video_id):
    return os.path.join(OUTPUT_DIR, f"{video_id}.csv")


def append_comments_to_csv(video_id, comments, write_header):
    path = _csv_path(video_id)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(comments)


# ---------------------------------------------------------------------------
# Phase 1: collect top Nepali videos
# ---------------------------------------------------------------------------

def fetch_video_ids_for_query(youtube, query):
    while True:
        try:
            resp = youtube.search().list(
                part="id", type="video", q=query,
                relevanceLanguage="ne", regionCode="NP",
                maxResults=MAX_RESULTS, order="viewCount",
            ).execute()
            return [item["id"]["videoId"] for item in resp.get("items", [])]
        except HttpError as e:
            if is_quota_error(e):
                quota_sleep()
            else:
                raise


def fetch_video_details(youtube, video_ids):
    all_videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i: i + 50]
        while True:
            try:
                resp = youtube.videos().list(
                    part="snippet,statistics", id=",".join(batch)
                ).execute()
                all_videos.extend(resp.get("items", []))
                break
            except HttpError as e:
                if is_quota_error(e):
                    quota_sleep()
                else:
                    raise
    return all_videos


def is_nepali_video(video):
    snippet      = video.get("snippet", {})
    audio_lang   = snippet.get("defaultAudioLanguage", "")
    default_lang = snippet.get("defaultLanguage", "")
    if audio_lang.startswith("ne") or default_lang.startswith("ne"):
        return True
    if audio_lang and not audio_lang.startswith("ne"):
        return False
    combined = snippet.get("title", "") + snippet.get("description", "")
    return any("\u0900" <= ch <= "\u097f" for ch in combined) or True


def collect_nepali_videos(youtube):
    seen, all_ids = set(), []
    for query in SEARCH_QUERIES:
        log.info("Searching: '%s'", query)
        ids = fetch_video_ids_for_query(youtube, query)
        new = [v for v in ids if v not in seen]
        seen.update(new)
        all_ids.extend(new)
        log.info("  -> %d new video IDs (pool: %d)", len(new), len(all_ids))
    return all_ids


def get_top_commented(videos):
    filtered = []
    for v in videos:
        if not is_nepali_video(v):
            continue
        cc = int(v["statistics"].get("commentCount", 0))
        if cc == 0:
            continue
        filtered.append({
            "title":         v["snippet"]["title"],
            "channel":       v["snippet"]["channelTitle"],
            "video_id":      v["id"],
            "comment_count": cc,
            "view_count":    int(v["statistics"].get("viewCount", 0)),
            "language":      v["snippet"].get("defaultAudioLanguage")
                             or v["snippet"].get("defaultLanguage", "untagged"),
        })
    filtered.sort(key=lambda x: x["comment_count"], reverse=True)
    return filtered[:TOP_N]


# ---------------------------------------------------------------------------
# Phase 2: scrape comments with checkpointing + quota handling
# ---------------------------------------------------------------------------

def scrape_video_comments(youtube, video, video_index, total_videos):
    vid_id = video["video_id"]
    title  = video["title"]
    short  = title[:55]

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if is_fully_scraped(vid_id):
        log.info("[%d/%d] SKIP (already done): %s", video_index, total_videos, short)
        return

    ckpt            = load_checkpoint(vid_id)
    next_page_token = ckpt["next_page_token"]
    pages_done      = ckpt["pages_done"]
    comments_kept   = ckpt["comments_kept"]
    write_header    = (pages_done == 0)

    if pages_done > 0:
        log.info("[%d/%d] RESUME from page %d (%d kept so far): %s",
                 video_index, total_videos, pages_done + 1, comments_kept, short)
    else:
        log.info("[%d/%d] START scraping (~%d comments expected): %s",
                 video_index, total_videos, video["comment_count"], short)

    pages_this_session = 0
    kept_this_session  = 0

    while True:
        # API call with quota-aware retry
        while True:
            try:
                resp = youtube.commentThreads().list(
                    part="snippet,replies",
                    videoId=vid_id,
                    maxResults=100,
                    pageToken=next_page_token,
                    textFormat="plainText",
                    order="time",
                ).execute()
                break
            except HttpError as e:
                if is_quota_error(e):
                    save_checkpoint(vid_id, next_page_token, pages_done, comments_kept)
                    quota_sleep()
                elif e.resp.status == 403:
                    log.warning("[%d/%d] Comments disabled for '%s'. Marking done.",
                                video_index, total_videos, short)
                    mark_done(vid_id)
                    clear_checkpoint(vid_id)
                    return
                else:
                    log.error("[%d/%d] HTTP %s for '%s': %s. Skipping.",
                              video_index, total_videos, e.resp.status, short, e)
                    return

        page_comments  = []
        page_total     = 0
        page_discarded = 0

        for thread in resp.get("items", []):
            top_snip = thread["snippet"]["topLevelComment"]["snippet"]
            top_text = top_snip.get("textDisplay", "")
            page_total += 1
            if is_romanized_nepali(top_text):
                page_comments.append({
                    "video_id":     vid_id,
                    "comment_id":   thread["snippet"]["topLevelComment"]["id"],
                    "parent_id":    "",
                    "author":       top_snip.get("authorDisplayName", ""),
                    "text":         top_text,
                    "likes":        top_snip.get("likeCount", 0),
                    "published_at": top_snip.get("publishedAt", ""),
                    "updated_at":   top_snip.get("updatedAt", ""),
                    "reply_count":  thread["snippet"].get("totalReplyCount", 0),
                })
            else:
                page_discarded += 1

            for reply in thread.get("replies", {}).get("comments", []):
                r_snip = reply["snippet"]
                r_text = r_snip.get("textDisplay", "")
                page_total += 1
                if is_romanized_nepali(r_text):
                    page_comments.append({
                        "video_id":     vid_id,
                        "comment_id":   reply["id"],
                        "parent_id":    thread["snippet"]["topLevelComment"]["id"],
                        "author":       r_snip.get("authorDisplayName", ""),
                        "text":         r_text,
                        "likes":        r_snip.get("likeCount", 0),
                        "published_at": r_snip.get("publishedAt", ""),
                        "updated_at":   r_snip.get("updatedAt", ""),
                        "reply_count":  0,
                    })
                else:
                    page_discarded += 1

        if page_comments:
            append_comments_to_csv(vid_id, page_comments, write_header)
            write_header = False

        next_page_token  = resp.get("nextPageToken")
        pages_done      += 1
        comments_kept   += len(page_comments)
        pages_this_session  += 1
        kept_this_session   += len(page_comments)

        save_checkpoint(vid_id, next_page_token, pages_done, comments_kept)

        log.debug("[%d/%d] page %d | fetched %d | kept %d | discarded %d | total kept %d",
                  video_index, total_videos, pages_done,
                  page_total, len(page_comments), page_discarded, comments_kept)

        if pages_done % 10 == 0:
            log.info("[%d/%d] Progress: page %d | %d romanized-Nepali comments kept so far",
                     video_index, total_videos, pages_done, comments_kept)

        if not next_page_token:
            break

        time.sleep(0.15)

    mark_done(vid_id)
    clear_checkpoint(vid_id)
    log.info("[%d/%d] DONE: %d kept (%d this session, %d pages) -> %s",
             video_index, total_videos,
             comments_kept, kept_this_session, pages_this_session,
             _csv_path(vid_id))


# ---------------------------------------------------------------------------
# Summary + display
# ---------------------------------------------------------------------------

def save_summary(top_videos):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, "summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(top_videos, f, ensure_ascii=False, indent=2)
    return path


def log_top_videos(top_videos):
    log.info("=" * 70)
    log.info("TOP %d MOST COMMENTED NEPALI VIDEOS", len(top_videos))
    log.info("=" * 70)
    for i, v in enumerate(top_videos, 1):
        done = " [DONE]" if is_fully_scraped(v["video_id"]) else ""
        log.info("%2d. %s%s", i, v["title"][:65], done)
        log.info("    Channel: %s | Comments: %s | Views: %s",
                 v["channel"], f"{v['comment_count']:,}", f"{v['view_count']:,}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 70)
    log.info("Nepali Romanized Comment Scraper starting up")
    log.info("Output dir: %s  |  Log: %s  |  Discard-if-EN/ES threshold: %.0f%%",
             OUTPUT_DIR, LOG_FILE, LINGUA_CONFIDENCE_THRESHOLD * 100)
    log.info("=" * 70)

    youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=API_KEY)

    log.info("Phase 1: collecting Nepali video IDs...")
    all_video_ids = collect_nepali_videos(youtube)

    log.info("Phase 2: fetching video details for %d videos...", len(all_video_ids))
    videos     = fetch_video_details(youtube, all_video_ids)
    top_videos = get_top_commented(videos)

    if not top_videos:
        log.error("No Nepali videos found. Exiting.")
        return

    log_top_videos(top_videos)
    save_summary(top_videos)

    already_done = sum(1 for v in top_videos if is_fully_scraped(v["video_id"]))
    log.info("Phase 3: scraping comments (%d/%d videos already complete).",
             already_done, len(top_videos))
    log.info("Script auto-pauses on quota exhaustion and resumes next day. Safe to restart.")

    for i, video in enumerate(top_videos, 1):
        scrape_video_comments(youtube, video, i, len(top_videos))

    log.info("All videos processed. Run again to pick up any new comments.")


if __name__ == "__main__":
    main()
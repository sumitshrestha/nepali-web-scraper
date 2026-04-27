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

Dependencies:
    pip install google-api-python-client python-dotenv lingua-language-detector
"""

import os
import csv
import json
import time
import logging
import datetime
import googleapiclient.discovery
from googleapiclient.errors import HttpError
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

from lang_filter import NepaliFilter

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

# How confident Lingua must be (0–1) before discarding a comment as EN or ES.
# Raise toward 0.90 to discard more aggressively; lower toward 0.75 to keep more.
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
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = setup_logging()

# ---------------------------------------------------------------------------
# Quota / rate-limit handling
# ---------------------------------------------------------------------------

def _seconds_until_quota_reset() -> float:
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


def quota_sleep() -> None:
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


def is_quota_error(e: HttpError) -> bool:
    try:
        reason = e.error_details[0].get("reason", "")
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            return True
    except Exception:
        pass
    msg = str(e).lower()
    return e.resp.status in (429, 403) and (
        "quota" in msg or "limit" in msg or "rate" in msg
    )


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


def save_checkpoint(video_id: str, next_page_token, pages_done: int, comments_kept: int) -> None:
    with open(_checkpoint_path(video_id), "w", encoding="utf-8") as f:
        json.dump({"next_page_token": next_page_token,
                   "pages_done": pages_done,
                   "comments_kept": comments_kept}, f)


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
# CSV append helper
# ---------------------------------------------------------------------------

CSV_FIELDNAMES = ["video_id", "comment_id", "parent_id", "author",
                  "text", "likes", "published_at", "updated_at", "reply_count"]


def _csv_path(video_id: str) -> str:
    return os.path.join(OUTPUT_DIR, f"{video_id}.csv")


def append_comments_to_csv(video_id: str, comments: list, write_header: bool) -> None:
    path = _csv_path(video_id)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(comments)


# ---------------------------------------------------------------------------
# Phase 1: collect top Nepali videos
# ---------------------------------------------------------------------------

def fetch_video_ids_for_query(youtube, query: str) -> list[str]:
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


def fetch_video_details(youtube, video_ids: list[str]) -> list:
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


def is_nepali_video(video: dict) -> bool:
    snippet      = video.get("snippet", {})
    audio_lang   = snippet.get("defaultAudioLanguage", "")
    default_lang = snippet.get("defaultLanguage", "")
    if audio_lang.startswith("ne") or default_lang.startswith("ne"):
        return True
    if audio_lang and not audio_lang.startswith("ne"):
        return False
    combined = snippet.get("title", "") + snippet.get("description", "")
    return any("\u0900" <= ch <= "\u097f" for ch in combined) or True


def collect_nepali_videos(youtube) -> list[str]:
    seen, all_ids = set(), []
    for query in SEARCH_QUERIES:
        log.info("Searching: '%s'", query)
        ids = fetch_video_ids_for_query(youtube, query)
        new = [v for v in ids if v not in seen]
        seen.update(new)
        all_ids.extend(new)
        log.info("  -> %d new video IDs (pool: %d)", len(new), len(all_ids))
    return all_ids


def get_top_commented(videos: list) -> list[dict]:
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

def scrape_video_comments(youtube, video: dict, video_index: int,
                          total_videos: int, lang_filter: NepaliFilter) -> None:
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
            if lang_filter.is_nepali(top_text):
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
                if lang_filter.is_nepali(r_text):
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

        next_page_token     = resp.get("nextPageToken")
        pages_done         += 1
        comments_kept      += len(page_comments)
        pages_this_session += 1
        kept_this_session  += len(page_comments)

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
        log.info("    Channel: %s | Comments: %s | Views: %s",
                 v["channel"], f"{v['comment_count']:,}", f"{v['view_count']:,}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=" * 70)
    log.info("Nepali Romanized Comment Scraper starting up")
    log.info("Output dir: %s  |  Log: %s  |  Discard-if-EN/ES threshold: %.0f%%",
             OUTPUT_DIR, LOG_FILE, LINGUA_CONFIDENCE_THRESHOLD * 100)
    log.info("=" * 70)

    # Load the language filter once — all scraping reuses the same instance
    lang_filter = NepaliFilter(threshold=LINGUA_CONFIDENCE_THRESHOLD)

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
    log.info("Auto-pauses on quota exhaustion and resumes next day. Safe to restart.")

    for i, video in enumerate(top_videos, 1):
        scrape_video_comments(youtube, video, i, len(top_videos), lang_filter)

    log.info("All videos processed. Run again to pick up any new comments.")


if __name__ == "__main__":
    main()
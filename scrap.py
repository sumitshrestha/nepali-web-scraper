import os
import googleapiclient.discovery
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise ValueError("Missing YOUTUBE_API_KEY")

TOP_N = 20
MAX_RESULTS = 50

# Nepali-language search queries using Devanagari script + romanized terms.
# Using actual Nepali script is the strongest signal to YouTube that we want
# Nepali-language content (not just content trending in Nepal).
SEARCH_QUERIES = [
    "नेपाली गीत",  # "Nepali song" in Devanagari
    "नेपाली भिडियो",  # "Nepali video"
    "नेपाली चलचित्र",  # "Nepali movie/film"
    "nepali comedy",
    "nepali music video 2024",
    "nepali movie official",
]


def fetch_video_ids_for_query(youtube, query):
    """Search YouTube with relevanceLanguage=ne to bias toward Nepali content."""
    request = youtube.search().list(
        part="id",
        type="video",
        q=query,
        relevanceLanguage="ne",  # Nepali language — strongest filter available
        regionCode="NP",
        maxResults=MAX_RESULTS,
        order="viewCount",  # Higher-viewed Nepali videos tend to have more comments
    )
    response = request.execute()
    return [item["id"]["videoId"] for item in response.get("items", [])]


def fetch_video_details(youtube, video_ids):
    """Fetch snippet and statistics for a list of video IDs."""
    if not video_ids:
        return []
    all_videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        request = youtube.videos().list(part="snippet,statistics", id=",".join(batch))
        response = request.execute()
        all_videos.extend(response.get("items", []))
    return all_videos


def is_nepali_video(video):
    """
    Heuristic filter to drop clearly non-Nepali videos.

    Checks:
    1. defaultAudioLanguage or defaultLanguage is 'ne' (Nepali), if set.
    2. Title or description contains Devanagari script characters (strong signal).
    3. Falls back to accepting the video if no language tag exists (YouTube rarely
       tags Nepali content, so we don't want to be too strict).
    """
    snippet = video.get("snippet", {})

    # If YouTube has explicitly tagged the language, trust it.
    audio_lang = snippet.get("defaultAudioLanguage", "")
    default_lang = snippet.get("defaultLanguage", "")
    if audio_lang.startswith("ne") or default_lang.startswith("ne"):
        return True
    # Reject if explicitly tagged as a different language (e.g. hi, en, te)
    if audio_lang and not audio_lang.startswith("ne"):
        return False

    # Check for Devanagari script in title or description (Unicode range U+0900–U+097F)
    title = snippet.get("title", "")
    description = snippet.get("description", "")
    combined = title + description
    has_devanagari = any("\u0900" <= ch <= "\u097f" for ch in combined)
    if has_devanagari:
        return True

    # No strong signal either way — keep it (many legit Nepali channels write in English)
    return True


def collect_nepali_videos(youtube):
    """Run multiple searches and deduplicate results."""
    seen_ids = set()
    all_video_ids = []

    for query in SEARCH_QUERIES:
        print(f"  Searching: '{query}'")
        ids = fetch_video_ids_for_query(youtube, query)
        new_ids = [vid for vid in ids if vid not in seen_ids]
        seen_ids.update(new_ids)
        all_video_ids.extend(new_ids)
        print(f"    → {len(new_ids)} new video IDs (total pool: {len(all_video_ids)})")

    return all_video_ids


def get_top_commented(videos):
    """Filter to Nepali videos with comments; return top N by comment count."""
    filtered = []
    for v in videos:
        if not is_nepali_video(v):
            continue
        comment_count = int(v["statistics"].get("commentCount", 0))
        if comment_count == 0:
            continue
        filtered.append(
            {
                "title": v["snippet"]["title"],
                "channel": v["snippet"]["channelTitle"],
                "video_id": v["id"],
                "comment_count": comment_count,
                "view_count": int(v["statistics"].get("viewCount", 0)),
                "language": v["snippet"].get("defaultAudioLanguage")
                or v["snippet"].get("defaultLanguage", "untagged"),
            }
        )

    filtered.sort(key=lambda x: x["comment_count"], reverse=True)
    return filtered[:TOP_N]


def print_result(top_videos):
    print(f"\n{'='*80}")
    print(f"TOP {len(top_videos)} MOST COMMENTED NEPALI VIDEOS")
    print(f"{'='*80}\n")
    for i, v in enumerate(top_videos, 1):
        print(f"{i}. {v['title']}")
        print(f"   Channel : {v['channel']}")
        print(f"   Language: {v['language']}")
        print(f"   Comments: {v['comment_count']:,} | Views: {v['view_count']:,}")
        print(f"   https://www.youtube.com/watch?v={v['video_id']}\n")


def main():
    youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=API_KEY)

    print("Collecting Nepali video IDs across multiple searches...")
    all_video_ids = collect_nepali_videos(youtube)

    if not all_video_ids:
        print("No video IDs found. Check your API key.")
        return

    print(f"\nFetching details for {len(all_video_ids)} unique videos...")
    videos = fetch_video_details(youtube, all_video_ids)

    top_videos = get_top_commented(videos)

    if not top_videos:
        print("No Nepali videos with comments found.")
        return

    print_result(top_videos)


if __name__ == "__main__":
    main()

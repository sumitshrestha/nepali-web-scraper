import os
import googleapiclient.discovery
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise ValueError("Missing YOUTUBE_API_KEY")

TOP_N = 20
MAX_RESULTS = 50


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


def get_top_commented(videos):
    """Filter out videos with no comments and return top N by comment count."""
    filtered = []
    for v in videos:
        comment_count = int(v["statistics"].get("commentCount", 0))
        if comment_count > 0:
            filtered.append(
                {
                    "title": v["snippet"]["title"],
                    "channel": v["snippet"]["channelTitle"],
                    "video_id": v["id"],
                    "comment_count": comment_count,
                    "view_count": int(v["statistics"].get("viewCount", 0)),
                }
            )
    filtered.sort(key=lambda x: x["comment_count"], reverse=True)
    return filtered[:TOP_N]


def main():
    youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=API_KEY)

    # --- Attempt 1: Get trending videos in Nepal (Works but includes international content) ---
    print("Attempting to retrieve Nepal's trending chart...")
    request = youtube.videos().list(
        part="id", chart="mostPopular", regionCode="NP", maxResults=MAX_RESULTS
    )
    response = request.execute()
    video_ids = [item["id"] for item in response.get("items", [])]

    if video_ids:
        print(f"Trending chart returned {len(video_ids)} videos.")
        videos = fetch_video_details(youtube, video_ids)
        top_videos = get_top_commented(videos)
        if top_videos:
            print_result(top_videos)
            return

    # --- Fallback: Search for Nepali music videos ---
    print(
        "No results from trending chart. Trying targeted keyword + category search..."
    )
    request = youtube.search().list(
        part="id",
        type="video",
        q="nepali songs",  # More specific search term
        regionCode="NP",  # Prioritize results for Nepal
        videoCategoryId="10",  # Music category
        maxResults=MAX_RESULTS,
    )
    response = request.execute()
    video_ids = [item["id"]["videoId"] for item in response.get("items", [])]

    if not video_ids:
        print(
            "No results found from fallback search. Please check your API key and parameters."
        )
        return

    print(f"Fallback search returned {len(video_ids)} videos.")
    videos = fetch_video_details(youtube, video_ids)
    top_videos = get_top_commented(videos)

    if not top_videos:
        print("No videos with comments found.")
        return

    print_result(top_videos)


def print_result(top_videos):
    print(f"\n{'='*80}")
    print(f"TOP {len(top_videos)} MOST COMMENTED VIDEOS FROM NEPAL")
    print(f"{'='*80}\n")
    for i, v in enumerate(top_videos, 1):
        print(f"{i}. {v['title']}")
        print(f"   Channel: {v['channel']}")
        print(f"   Comments: {v['comment_count']:,} | Views: {v['view_count']:,}")
        print(f"   https://www.youtube.com/watch?v={v['video_id']}\n")


if __name__ == "__main__":
    main()

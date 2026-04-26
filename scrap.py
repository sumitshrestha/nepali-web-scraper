import os
import googleapiclient.discovery
from googleapiclient.errors import HttpError
from dotenv import load_dotenv  # Optional, only if using .env file

# Load from .env file (comment out if using system env only)
load_dotenv()

# --- Configuration ---
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise ValueError("Missing YOUTUBE_API_KEY environment variable")

REGION_CODE = "NP"
MAX_VIDEOS_TO_FETCH = 200
TOP_N = 20

YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"


def get_top_commented_videos():
    youtube = googleapiclient.discovery.build(
        YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, developerKey=API_KEY
    )

    # --- Step 1: Search for popular videos in Nepal ---
    search_response = (
        youtube.search()
        .list(
            part="id",
            type="video",
            regionCode=REGION_CODE,
            order="viewCount",  # Fetches the most viewed videos in the region
            maxResults=MAX_VIDEOS_TO_FETCH,
        )
        .execute()
    )

    video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
    if not video_ids:
        print(f"No videos found for region '{REGION_CODE}'.")
        return

    # --- Step 2: Get statistics (including comment counts) for those videos ---
    # Process in batches to avoid API errors (API typically accepts up to 50 IDs per request)
    all_video_stats = []
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i : i + 50]
        videos_response = (
            youtube.videos()
            .list(part="statistics,snippet", id=",".join(batch_ids))
            .execute()
        )
        all_video_stats.extend(videos_response.get("items", []))

    # --- Step 3: Filter and sort videos by comment count ---
    filtered_videos = []
    for video in all_video_stats:
        # Ensure comment count exists and is an integer, and that the video is available in the region
        comment_count = int(video["statistics"].get("commentCount", 0))
        if comment_count > 0:
            filtered_videos.append(
                {
                    "title": video["snippet"]["title"],
                    "video_id": video["id"],
                    "comment_count": comment_count,
                    "view_count": int(video["statistics"].get("viewCount", 0)),
                }
            )

    # Sort by comment_count in descending order
    sorted_videos = sorted(
        filtered_videos, key=lambda x: x["comment_count"], reverse=True
    )

    # --- Step 4: Get the top N results ---
    top_videos = sorted_videos[:TOP_N]

    # --- Output Results ---
    if not top_videos:
        print("No videos with comments found.")
        return

    print(
        f"\nTop {len(top_videos)} Most Commented YouTube Videos in Nepal (Region Code: {REGION_CODE}):"
    )
    print("=" * 80)
    for i, video in enumerate(top_videos, 1):
        print(f"{i}. Title: {video['title']}")
        print(f"   Video ID: {video['video_id']}")
        print(f"   URL: https://www.youtube.com/watch?v={video['video_id']}")
        print(
            f"   Comments: {video['comment_count']:,} | Views: {video['view_count']:,}"
        )
        print("-" * 40)


if __name__ == "__main__":
    try:
        get_top_commented_videos()
    except HttpError as e:
        print(f"An API error occurred: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

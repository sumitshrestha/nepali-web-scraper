import os
import googleapiclient.discovery
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

# Load API key from .env file
load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise ValueError("YOUTUBE_API_KEY not found in environment variables")

REGION_CODE = "NP"  # Nepal
MAX_RESULTS = 50  # Number of trending videos to fetch
TOP_N = 20  # Final number of most commented videos


def get_top_commented_videos_nepal():
    youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=API_KEY)

    # Step 1: Get the most popular videos in Nepal (this always works)
    print(f"Fetching top {MAX_RESULTS} trending videos in Nepal...")
    try:
        request = youtube.videos().list(
            part="snippet,statistics",
            chart="mostPopular",
            regionCode=REGION_CODE,
            maxResults=MAX_RESULTS,
        )
        response = request.execute()
    except HttpError as e:
        print(f"API error: {e}")
        return

    items = response.get("items", [])
    if not items:
        print("No trending videos found for Nepal. Trying fallback...")
        # Fallback: search with keyword (rarely needed)
        fallback_request = youtube.search().list(
            part="id",
            q="nepal",
            type="video",
            regionCode=REGION_CODE,
            maxResults=MAX_RESULTS,
        )
        fallback_response = fallback_request.execute()
        video_ids = [
            item["id"]["videoId"] for item in fallback_response.get("items", [])
        ]
        if not video_ids:
            print("Still no results. Check your API key or region code.")
            return
        # Fetch details for fallback videos
        videos_request = youtube.videos().list(
            part="snippet,statistics", id=",".join(video_ids)
        )
        videos_response = videos_request.execute()
        items = videos_response.get("items", [])

    # Step 2: Extract comment counts and sort
    video_list = []
    for video in items:
        comment_count = int(video["statistics"].get("commentCount", 0))
        if comment_count > 0:  # Only include videos with comments enabled
            video_list.append(
                {
                    "title": video["snippet"]["title"],
                    "video_id": video["id"],
                    "comment_count": comment_count,
                    "view_count": int(video["statistics"].get("viewCount", 0)),
                    "channel_title": video["snippet"]["channelTitle"],
                }
            )

    # Sort by comment count descending
    video_list.sort(key=lambda x: x["comment_count"], reverse=True)
    top_videos = video_list[:TOP_N]

    # Step 3: Display results
    if not top_videos:
        print("No videos with comments found.")
        return

    print(f"\n{'='*80}")
    print(f"TOP {len(top_videos)} MOST COMMENTED YOUTUBE VIDEOS IN NEPAL")
    print(f"{'='*80}\n")

    for i, v in enumerate(top_videos, 1):
        print(f"{i}. {v['title']}")
        print(f"   Channel: {v['channel_title']}")
        print(f"   Comments: {v['comment_count']:,} | Views: {v['view_count']:,}")
        print(f"   https://www.youtube.com/watch?v={v['video_id']}\n")


if __name__ == "__main__":
    get_top_commented_videos_nepal()

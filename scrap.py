import os
import googleapiclient.discovery
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise ValueError("Missing YOUTUBE_API_KEY environment variable")

REGION_CODE = "NP"
MAX_RESULTS = 50
TOP_N = 20


def search_by_region(youtube, region_code, max_results):
    """
    Search for videos in a specific region using the regionCode parameter.
    """
    request = youtube.search().list(
        part="id",
        type="video",
        regionCode=region_code,
        maxResults=max_results,
        order="viewCount",  # You can change or remove this
    )
    return request.execute()


def search_by_location(youtube, location, radius, max_results):
    """
    Search for videos near a specific geographic location.
    """
    request = youtube.search().list(
        part="id",
        type="video",
        location=location,
        locationRadius=radius,
        maxResults=max_results,
        order="viewCount",
    )
    return request.execute()


def get_top_commented_videos(youtube, video_ids):
    """
    Retrieve video statistics for a list of video IDs and sort by comment count.
    """
    all_video_stats = []
    # Process in batches of 50 (API limit)
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i : i + 50]
        videos_response = (
            youtube.videos()
            .list(part="statistics,snippet", id=",".join(batch_ids))
            .execute()
        )
        all_video_stats.extend(videos_response.get("items", []))

    filtered_videos = []
    for video in all_video_stats:
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

    sorted_videos = sorted(
        filtered_videos, key=lambda x: x["comment_count"], reverse=True
    )
    return sorted_videos[:TOP_N]


def main():
    youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=API_KEY)

    # --- Attempt 1: Search by region ---
    print(f"Searching for videos in region '{REGION_CODE}'...")
    search_response = search_by_region(youtube, REGION_CODE, MAX_RESULTS)
    total_results = search_response.get("pageInfo", {}).get("totalResults", 0)
    print(f"Found {total_results} total results.")

    video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]

    # --- Fallback: Search by location if region search returns no results ---
    if not video_ids:
        print("No results from region search; trying location-based search...")
        # Roughly centered in Nepal
        search_response = search_by_location(
            youtube, "28.3949, 84.1240", "500km", MAX_RESULTS
        )
        video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
        total_results = search_response.get("pageInfo", {}).get("totalResults", 0)
        print(f"Found {total_results} total results from location search.")

    if not video_ids:
        print(
            "No videos found. Please try different search criteria or check your API key."
        )
        return

    # --- Retrieve and sort by comment count ---
    top_videos = get_top_commented_videos(youtube, video_ids)

    if not top_videos:
        print("No videos with comments found.")
        return

    print(f"\nTop {len(top_videos)} Most Commented YouTube Videos in Nepal:")
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
        main()
    except HttpError as e:
        print(f"An API error occurred: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

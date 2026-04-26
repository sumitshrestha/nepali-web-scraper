import os
import googleapiclient.discovery
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise ValueError("Missing YOUTUBE_API_KEY")

REGION_CODE = "NP"
TOP_N = 20
MAX_RESULTS = 100  # Fetch more to filter


def get_nepali_videos_by_language(youtube):
    """Search for videos in Nepali language (more likely from Nepali creators)"""
    request = youtube.search().list(
        part="id",
        type="video",
        relevanceLanguage="ne",  # Nepali language
        maxResults=MAX_RESULTS,
        order="viewCount",  # Get most viewed in Nepali language
    )
    return request.execute()


def get_nepali_videos_by_location(youtube):
    """Search for videos geolocated in Nepal"""
    request = youtube.search().list(
        part="id",
        type="video",
        location="28.3949, 84.1240",  # Center of Nepal
        locationRadius="500km",
        maxResults=MAX_RESULTS,
        order="viewCount",
    )
    return request.execute()


def get_video_details(youtube, video_ids):
    """Fetch statistics and snippet info for a list of video IDs"""
    all_videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        request = youtube.videos().list(part="snippet,statistics", id=",".join(batch))
        response = request.execute()
        all_videos.extend(response.get("items", []))
    return all_videos


def filter_nepali_content(videos):
    """Keep videos that likely come from Nepali creators"""
    nepali_keywords = ["nepali", "nepal", "nepalima", "kathmandu", "पोखरा", "नेपाली"]
    filtered = []
    for v in videos:
        title = v["snippet"]["title"].lower()
        channel = v["snippet"]["channelTitle"].lower()
        # Check if title or channel mentions Nepal/Nepali
        if any(kw in title or kw in channel for kw in nepali_keywords):
            filtered.append(v)
        # Also keep channel IDs known to be Nepali (you can add more)
        elif v["snippet"]["channelId"] in [
            "UC8y3T8s6Kk5Fq_6KjZgXZ5w"
        ]:  # Example, add real IDs
            filtered.append(v)
    return filtered


def main():
    youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=API_KEY)

    print("Searching for Nepali-language videos...")
    response = get_nepali_videos_by_language(youtube)
    video_ids = [item["id"]["videoId"] for item in response.get("items", [])]

    if not video_ids:
        print("No Nepali language videos found. Trying location search...")
        response = get_nepali_videos_by_location(youtube)
        video_ids = [item["id"]["videoId"] for item in response.get("items", [])]

    if not video_ids:
        print("No videos found. Check API key or try different approach.")
        return

    print(f"Found {len(video_ids)} candidate videos. Fetching details...")
    videos = get_video_details(youtube, video_ids)

    # Filter for actual Nepali content
    nepali_videos = filter_nepali_content(videos)
    print(f"After filtering, {len(nepali_videos)} appear to be from Nepali creators.")

    # Sort by comment count
    for v in nepali_videos:
        v["comment_count"] = int(v["statistics"].get("commentCount", 0))
        v["view_count"] = int(v["statistics"].get("viewCount", 0))

    nepali_videos.sort(key=lambda x: x["comment_count"], reverse=True)
    top = nepali_videos[:TOP_N]

    if not top:
        print("No Nepali videos with comments found.")
        return

    print(f"\n{'='*80}")
    print(f"TOP {len(top)} MOST COMMENTED VIDEOS FROM NEPALI CREATORS")
    print(f"{'='*80}\n")

    for i, v in enumerate(top, 1):
        print(f"{i}. {v['snippet']['title']}")
        print(f"   Channel: {v['snippet']['channelTitle']}")
        print(f"   Comments: {v['comment_count']:,} | Views: {v['view_count']:,}")
        print(f"   https://www.youtube.com/watch?v={v['id']}\n")


if __name__ == "__main__":
    main()

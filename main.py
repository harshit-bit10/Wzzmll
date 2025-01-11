import os
import asyncio
import logging
import ffmpeg
import json
import time
import shlex
import shutil
from datetime import datetime , timedelta
from typing import Dict, List, Tuple
from pyrogram import Client, filters
import subprocess
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
import re
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import yt_dlp
from config import *
from config import Config

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Bot instance
bot = Client(
    "LiveRecordBot",
    bot_token=Config.BOT_TOKEN,
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
)



# Directory for saving recordings
DOWNLOADS_DIR = Config.DOWNLOAD_DIRECTORY
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Global user state tracking
user_states: Dict[int, Dict] = {}
user_sessions = {}

# Telegram max message length
MAX_MESSAGE_LENGTH = 4096

# Helper: Run shell commands asynchronously
async def run_command(cmd: str) -> Tuple[str, str]:
    logger.info(f"Executing command: {cmd}")
    process = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()
    return stdout.decode(), stderr.decode()

async def parse_streams(link: str) -> Tuple[List[str], List[str], List[str]]:
    ydl_opts = {
        'quiet': True,
        'extract_flat': False,
        'noplaylist': True,
        'force_generic_extractor': True,
    }

    audio_streams = []
    video_streams = []
    audio_video_streams = []  # For multiplexed audio-video streams
    seen_audio_codecs = set()  # To avoid duplicate audio codec listings

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info_dict = ydl.extract_info(link, download=False)

            logger.info("Available formats and codecs:")
            for stream in info_dict.get('formats', []):
                logger.info(
                    f"Stream format: {stream['format_id']}, "
                    f"vcodec: {stream.get('vcodec')}, acodec: {stream.get('acodec')}, "
                    f"format_note: {stream.get('format_note')}"
                )

                # Video-only streams
                if stream.get('vcodec') != 'none' and stream.get('acodec') == 'none':
                    resolution = f"{stream.get('height', 'Unknown')}p"
                    video_bitrate = f"{stream.get('tbr', 'Unknown')}kbps"
                    video_streams.append(
                        f"{stream['format_id']} - {resolution} - {stream.get('vcodec', 'Unknown')} - {video_bitrate}"
                    )

                # Audio-only streams
                elif stream.get('acodec') != 'none' and stream.get('vcodec') == 'none':
                    audio_bitrate = f"{stream.get('abr', 'Unknown')}kbps"
                    audio_streams.append(
                        f"{stream['format_id']} - {stream.get('acodec', 'Unknown')} - {stream.get('language', 'Unknown')} - {audio_bitrate}"
                    )

                # Multiplexed streams (audio + video)
                elif stream.get('vcodec') != 'none' and stream.get('acodec') != 'none':
                    audio_codec = stream.get('acodec')
                    video_codec = stream.get('vcodec')
                    resolution = f"{stream.get('height', 'Unknown')}p"
                    video_bitrate = f"{stream.get('tbr', 'Unknown')}kbps"
                    audio_bitrate = f"{stream.get('abr', 'Unknown')}kbps"
                    language = stream.get('language', 'Unknown')

                    # Avoid adding duplicate audio codecs in multiplexed streams
                    if audio_codec not in seen_audio_codecs:
                        seen_audio_codecs.add(audio_codec)
                        audio_video_streams.append(
                            f"{stream['format_id']} - {resolution} - {video_codec} ({video_bitrate}) + {language} ({audio_codec}, {audio_bitrate})"
                        )
                        audio_streams.append(f"{stream['format_id']} - {language} - {audio_codec} - {audio_bitrate}")
                        video_streams.append(f"{stream['format_id']} - {resolution} - {video_codec} - {video_bitrate}")
                    else:
                        # If the audio codec is the same, just append the video part with no audio description
                        audio_video_streams.append(
                            f"{stream['format_id']} - {resolution} - {video_codec} ({video_bitrate})"
                        )
                        video_streams.append(f"{stream['format_id']} - {resolution} - {video_codec} - {video_bitrate}")

                # Catch-all else clause for unknown formats
                else:
                    logger.warning(
                        f"Stream format {stream['format_id']} could not be classified: "
                        f"vcodec={stream.get('vcodec', 'none')} acodec={stream.get('acodec', 'none')}."
                    )

            # Log the counts of streams found
            logger.info(
                f"Found {len(video_streams)} video streams, {len(audio_streams)} audio streams, "
                f"and {len(audio_video_streams)} multiplexed streams."
            )

            # Error handling if no valid streams
            if not video_streams and not audio_streams and not audio_video_streams:
                logger.error("No valid video or audio streams found.")
                return [], [], []

        except Exception as e:
            logger.error(f"Error occurred while parsing streams: {e}")
            return [], [], []

    return audio_streams, video_streams, audio_video_streams

# Helper: Create inline buttons for stream selection
def create_buttons(items: List[str], selected: set, prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f"{'✔' if i in selected else ''} {item}",
            callback_data=f"{prefix}_{i}"
        ) for i, item in enumerate(items)
    ]
    buttons.append(InlineKeyboardButton("✅ Confirm", callback_data=f"{prefix}_confirm"))
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])

# Helper: Split and send long messages
async def send_long_message(chat_id: int, text: str):
    for i in range(0, len(text), MAX_MESSAGE_LENGTH):
        await bot.send_message(chat_id, text[i:i + MAX_MESSAGE_LENGTH])

# Command: Start
@bot.on_message(filters.command("start"))
async def start_command(_, message: Message):
    await message.reply_text("Welcome to Live Record Bot! Use /record <link> <hh:mm:ss> to start recording.")

# Command: Record
@bot.on_message(
    (filters.private | filters.group) &  # Allow both private messages and groups
    filters.regex(r"https?://.*\s\d{2}:\d{2}:\d{2}") &  # Match URL followed by timestamp
    filters.user(Config.AUTH_USERS)  # Restrict to authorized users
)

async def record_command(_, message: Message):
    args = message.text.split(maxsplit=5)  # Split into 5 parts (link, duration, title, channel)
    
    if len(args) != 5:
        await message.reply_text("Invalid format! Use: /record <link> <hh:mm:ss> \"<title>\" \"<channel>\"")
        return

    link, duration, title, channel = args[1], args[2], args[3], args[4]
    
    try:
        hours, minutes, seconds = map(int, duration.split(":"))
        duration_seconds = hours * 3600 + minutes * 60 + seconds
    except ValueError:
        await message.reply_text("Invalid duration format. Use hh:mm:ss.")
        return

    await message.reply_text("Fetching streams, please wait...")

    audio_streams, video_streams, audio_video_streams = await parse_streams(link)
    if not audio_streams and not video_streams and not audio_video_streams:
        await message.reply_text("No streams found. Please verify the link.")
        return

    user_states[message.from_user.id] = {
        "link": link,
        "duration": duration_seconds,
        "audio_selected": set(),
        "video_selected": set(),
        "audio_streams": audio_streams,
        "video_streams": video_streams,
        "audio_video_streams": audio_video_streams,  # Add this to track multiplexed streams
        "title": title,
        "channel": channel
    }

    buttons = create_buttons(audio_streams, set(), "audio")
    await message.reply_text("Select audio tracks (multi-select):", reply_markup=buttons)

# Callback: Handle stream selection
# Command: Record
@bot.on_message(
    (filters.private | filters.group) &  # Allow both private messages and groups
    filters.regex(r"https?://.*\s\d{2}:\d{2}:\d{2}") &  # Match URL followed by timestamp
    filters.user(Config.AUTH_USERS)  # Restrict to authorized users
)

async def handle_selection(_, query: CallbackQuery):
    user_id = query.from_user.id
    state = user_states.get(user_id)
    if not state:
        await query.answer("Session expired. Start again.", show_alert=True)
        return

    prefix, action = query.data.split("_")
    if action == "confirm":
        if prefix == "audio" and not state["audio_selected"]:
            await query.answer("Please select at least one audio track.", show_alert=True)
            return
        elif prefix == "audio":
            buttons = create_buttons(state["video_streams"], set(), "video")
            await query.message.edit_text("Select a video track (single select):", reply_markup=buttons)
            return
        elif prefix == "video":
            if not state["video_selected"]:
                await query.answer("Please select a video track.", show_alert=True)
                return
            else:
                await query.message.edit_text("Starting recording...")
                await start_recording(user_id)
                return
        elif prefix == "multiplexed" and not state.get("audio_video_selected"):
            await query.answer("Please select at least one multiplexed stream.", show_alert=True)
            return
        else:
            await query.message.edit_text("Starting recording...")
            await start_recording(user_id)
            return

    idx = int(action)
    selected = state["audio_selected"] if prefix == "audio" else \
              state["video_selected"] if prefix == "video" else \
              state.get("audio_video_selected", set())

    if prefix == "video":
        # Clear previous selection and select the new one
        state["video_selected"].clear()
        state["video_selected"].add(idx)
    else:
        if idx in selected:
            selected.remove(idx)
        else:
            selected.add(idx)

    buttons = create_buttons(
        state["audio_streams"], state["audio_selected"], "audio"
    ) if prefix == "audio" else create_buttons(
        state["video_streams"], state["video_selected"], "video"
    ) if prefix == "video" else create_buttons(
        state["audio_video_streams"], state.get("audio_video_selected", set()), "multiplexed"
    )

    await query.message.edit_text(
        f"Select {'audio' if prefix == 'audio' else 'video' if prefix == 'video' else 'multiplexed'} tracks:",
        reply_markup=buttons
    )

async def send_notification(user_id, message):
    """Sends a notification to the user."""
    try:
        await bot.send_message(user_id, message)
    except Exception as e:
        logger.error(f"Failed to send message to user {user_id}. Error: {str(e)}")

def get_start_time():
    """Returns the current time in hh:mm:ss format."""
    now = datetime.now()
    return now.strftime('%H:%M:%S')

# Helper function to classify the stream type
def classify_stream(link):
    """Classify the stream's type based on its URL or metadata."""
    if "hls" in link:
        return "HLS"
    elif "dash" in link:
        return "DASH"
    elif "rtmp" in link:
        return "RTMP"
    elif "mms" in link:
        return "MMS"
    elif "ism" in link:
        return "Smooth Streaming"
    elif "webm" in link:
        return "WebM"
    # Add more types as needed
    else:
        return "Unknown"

# Adaptive method to handle stream types with start time adjustment
async def run_command_with_adaptive_logic(cmd, stream_type, stream_level, start_time=None):
    """Run a command with adaptive handling for different stream types."""
    if stream_type == "HLS" or stream_type == "DASH":
        # Adjusting for HLS/DASH streams by setting the start time for synchronization
        if start_time:
            cmd += f" -ss {start_time}"

    elif stream_type == "RTMP":
        cmd += " -rtmp_buffer 2000"
    elif stream_type == "MMS":
        cmd += " -mms_buffer_size 2000"
    elif stream_type == "Smooth Streaming":
        cmd += " -smoothstreaming"
    elif stream_type == "WebM":
        cmd += " -c:v vp9 -b:v 1M"
    elif stream_type == "CMAF":
        cmd += " -cmaf"
    elif stream_type == "SRT":
        cmd += " -srt_live_latency 5000"
    elif stream_type == "FLV":
        cmd += " -c:v flv -c:a aac"

    return await run_command(cmd)

async def start_recording(user_id: int):
    try:
        state = user_states.get(user_id)
        if not state:
            logger.error(f"No user state found for user {user_id}.")
            await send_notification(user_id, "Error: No active recording session found.")
            return

        link = state["link"]
        duration = state["duration"]
        audio_tracks = state.get("audio_selected", [])
        video_tracks = list(state.get("video_selected", []))  # Convert to list to allow indexing

        # Step 1: Create a common start time for synchronization
        start_time = time.time()  # Get the current time in seconds

        # Step 2: Create tasks for video and audio processing
        tasks = []
        output_video_files = []
        output_audio_files = []
        muxed_files = []  # List to store muxed file paths

        if 'master.m3u8' in link:
            # Processing for master.m3u8
            logger.info(f"Processing master.m3u8 for user {user_id}.")

            # Process each video track for master.m3u8
            for i, video in enumerate(video_tracks):
                video_output = os.path.join(DOWNLOADS_DIR, f"video_{user_id}_{i}.ts")
                cmd = (
                    f'"{FFMPEG_PATH}" -y -ss {start_time} -i "{link}" -map 0:v:{video} '
                    f"-c:v copy -t {duration} -fflags +genpts -f mpegts \"{video_output}\""
                )
                output_video_files.append(video_output)
                tasks.append(run_command(cmd))

            # Process each audio track for master.m3u8
            for i, audio in enumerate(audio_tracks):
                audio_output = os.path.join(DOWNLOADS_DIR, f"audio_{user_id}_{i}.aac")
                cmd = (
                    f'"{FFMPEG_PATH}" -y -ss {start_time} -i "{link}" -map 0:a:{audio} '
                    f"-c:a copy -t {duration} -f adts \"{audio_output}\""
                )
                output_audio_files.append(audio_output)
                tasks.append(run_command(cmd))

        else:
            # Processing for non-master.m3u8 links (e.g., direct .m3u8 streams)
            logger.info(f"Processing non-master.m3u8 for user {user_id}.")

            # Process video tracks for non-master.m3u8
            for i, video in enumerate(video_tracks):
                video_output = os.path.join(DOWNLOADS_DIR, f"video_{user_id}_{i}.ts")
                cmd = (
                    f'"{FFMPEG_PATH}" -y -ss {start_time} -i "{link}" -map 0:v:{video} '
                    f"-c:v copy -t {duration} -f mpegts \"{video_output}\""
                )
                output_video_files.append(video_output)
                tasks.append(run_command(cmd))

            # Process audio tracks for non-master.m3u8
            for i, audio in enumerate(audio_tracks):
                audio_output = os.path.join(DOWNLOADS_DIR, f"audio_{user_id}_{i}.aac")
                cmd = (
                    f'"{FFMPEG_PATH}" -y -ss {start_time} -i "{link}" -map 0:a:{audio} '
                    f"-c:a copy -t {duration} -f adts \"{audio_output}\""
                )
                output_audio_files.append(audio_output)
                tasks.append(run_command(cmd))

        # Step 3: Run all tasks in parallel for maximum efficiency
        await asyncio.gather(*tasks)

        # Step 4: Verify file creation and send notifications
        for file in output_video_files + output_audio_files:
            if not os.path.exists(file) or os.path.getsize(file) < 1 * 512:  # File size < 0.5 KB
                logger.error(f"Error: File not created or is too small - {file}")
                await send_notification(user_id, f"Recording failed: File error - {file}")
                return

        # Step 5: Mux video and audio files together
        for i, video_file in enumerate(output_video_files):
            muxed_file = os.path.join(DOWNLOADS_DIR, f"muxed_{user_id}_{i}.mp4")
            audio_inputs = " ".join([f"-i \"{audio_file}\"" for audio_file in output_audio_files])
            map_audio = " ".join([f"-map {j + 1}:a" for j in range(len(output_audio_files))])

            # Mux command to combine video with all audio tracks
            mux_cmd = (
                f'"{FFMPEG_PATH}" -y -i \"{video_file}\" {audio_inputs} '
                f"-map 0:v {map_audio} "
                f"-c:v copy -c:a copy -movflags +faststart \"{muxed_file}\""
            )
            try:
                await run_command(mux_cmd)
                muxed_files.append(muxed_file)
                logger.info(f"Muxing completed: {muxed_file}")
            except Exception as e:
                logger.error(f"Error during muxing: {e}")
                await send_notification(user_id, f"Muxing failed: {e}")

        # Step 6: Notify user and handle final files
        logger.info(f"Recording completed for user {user_id}. Files are ready in {DOWNLOADS_DIR}.")
        await send_notification(user_id, "Recording completed. Uploading files...")

        # Function to get video duration
        def get_video_duration(file_path):
            try:
                result = subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                duration_seconds = float(result.stdout.decode().strip())
                hours = int(duration_seconds // 3600)
                minutes = int((duration_seconds % 3600) // 60)
                seconds = int(duration_seconds % 60)
                return f"{hours}h {minutes}m {seconds}s"
            except Exception as e:
                return "Unknown Duration"  # In case of error

        # Upload the muxed files
        for muxed_file in muxed_files:
            if os.path.exists(muxed_file):
                try:
                    file_size = os.path.getsize(muxed_file)
                    file_name = os.path.basename(muxed_file)
                    duration = get_video_duration(muxed_file)

                    # Always upload the file regardless of size (you can modify this part for larger files)
                    user_state = user_states.get(user_id)
                    if user_state:
                        title = user_state.get("title")
                        channel = user_state.get("channel")

                        # Function to get video resolution, audio bitrate, and video bitrate using ffprobe
                        def get_media_info(file_path):
                            try:
                                # Get video resolution using ffprobe
                                video_resolution_cmd = [
                                    "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", 
                                    "-of", "default=noprint_wrappers=1:nokey=1", file_path
                                ]
                                resolution = subprocess.check_output(video_resolution_cmd).decode().strip().split("\n")
                                width, height = resolution[0], resolution[1]
                                resolution_str = f"{height}p"  # For example, 480p, 1080p

                                # Get audio bitrate using ffprobe
                                audio_bitrate_cmd = [
                                    "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=bit_rate", 
                                    "-of", "default=noprint_wrappers=1:nokey=1", file_path
                                ]
                                audio_bitrate = subprocess.check_output(audio_bitrate_cmd).decode().strip()
                                audio_bitrate = int(audio_bitrate) / 1000  # Convert to kbps
                                audio_bitrate = round(audio_bitrate)  # Round off to nearest integer

                                # Get video bitrate using ffprobe
                                video_bitrate_cmd = [
                                    "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=bit_rate", 
                                    "-of", "default=noprint_wrappers=1:nokey=1", file_path
                                ]
                                video_bitrate = subprocess.check_output(video_bitrate_cmd).decode().strip()
                                video_bitrate = int(video_bitrate) / 1000  # Convert to kbps
                                video_bitrate = round(video_bitrate)  # Round off to nearest integer

                                return resolution_str, f"{audio_bitrate} kbps", f"{video_bitrate} kbps"
                            except Exception as e:
                                return "Unknown", "Unknown", "Unknown"

                        # Extract details for each muxed file
                        resolution, audio_bitrate, video_bitrate = get_media_info(muxed_file)

                        # Generate the caption with dynamic title, channel, and credits
                        caption = (
                            f"<b>File-Name:</b> <code>[{Config.CREDITS}].{title}.{channel}.{resolution}-{video_bitrate}.IPTV.WEB-DL.{audio_bitrate}</code>\n"
                            f"<b>Duration:</b> <code>{duration}</code>"
                        )

                        # Send the video file to Telegram
                        await bot.send_video(
                            chat_id=user_id,
                            video=open(muxed_file, 'rb'),
                            caption=caption,
                        )
                        logger.info(f"Video uploaded successfully for user {user_id}.")
                except Exception as e:
                    logger.error(f"Error during upload: {e}")
                    await send_notification(user_id, f"Upload failed: {e}")

        # Cleanup
        logger.info("Cleanup completed.")
        await send_notification(user_id, "Files uploaded and cleanup done.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await send_notification(user_id, f"An error occurred: {e}")


# Start bot
bot.run()



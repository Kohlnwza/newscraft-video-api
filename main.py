import asyncio
import gc
import io
import os
import random
import re
import shutil
import subprocess
import sys
import uuid
from typing import List, Optional
from urllib.parse import quote

import edge_tts
import imageio_ffmpeg
import requests
from fastapi import BackgroundTasks, FastAPI
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel

# Ensure logs appear immediately in Render
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

app = FastAPI(
    title="NewsCraft Video Rendering API",
    description="API สำหรับแปลงสคริปต์ข่าว JSON เป็นวิดีโอสั้นแนวตั้ง 9:16 พร้อมเสียงพากย์ไทยและส่งเข้า LINE OA",
    version="1.1.0",
)

os.makedirs("outputs", exist_ok=True)
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")

HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_USER_ID = os.getenv("LINE_USER_ID", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()

VOICE = "th-TH-NiwatNeural"
VOICE_RATE = "+8%"
FONT_PATH = "thai_font.ttf"
VIDEO_WIDTH = 720
VIDEO_HEIGHT = 1280


class SceneItem(BaseModel):
    scene: int
    voiceover_text: str
    visual_search_query: str
    on_screen_text: str


class VideoPayload(BaseModel):
    project: str
    platform: Optional[str] = "TikTok"
    scenes: List[SceneItem]


@app.get("/")
def health_check():
    """Health check endpoint for Render."""
    return {"status": "ok", "service": "NewsCraft Video Rendering API"}


async def generate_tts(text: str, output_audio: str):
    """Generate energetic Thai news voiceover MP3 using edge-tts."""
    communicate = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE)
    await communicate.save(output_audio)


def create_fallback_image(output_image: str):
    """Create a dark editorial gradient 9:16 background image."""
    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (14, 20, 34))
    draw = ImageDraw.Draw(img)
    for y in range(VIDEO_HEIGHT):
        r = int(14 + (y / VIDEO_HEIGHT) * 18)
        g = int(20 + (y / VIDEO_HEIGHT) * 30)
        b = int(34 + (y / VIDEO_HEIGHT) * 48)
        draw.line([(0, y), (VIDEO_WIDTH, y)], fill=(r, g, b))
    img.save(output_image, quality=92)


def clean_search_query(query: str) -> str:
    """Remove abstract/style words so stock photo search focuses on concrete visual subjects."""
    stop_words = {
        "illustration", "editorial", "modern", "cinematic", "concept",
        "abstract", "background", "graph", "chart", "infographic", "style",
    }
    words = [w for w in query.replace(",", " ").split() if w.lower() not in stop_words]
    return " ".join(words) if words else query


def fetch_pexels_image(query: str, output_image: str, used_urls: set) -> bool:
    """Fetch a crisp 9:16 portrait photo from Pexels API (free key from pexels.com/api)."""
    if not PEXELS_API_KEY:
        return False
    try:
        res = requests.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "orientation": "portrait", "per_page": 5},
            headers={"Authorization": PEXELS_API_KEY, "User-Agent": "NewsCraftAI/1.0"},
            timeout=10,
        )
        res.raise_for_status()
        for photo in res.json().get("photos", []):
            img_url = (photo.get("src") or {}).get("large2x") or (photo.get("src") or {}).get("large")
            if not img_url or img_url in used_urls:
                continue
            try:
                img_res = requests.get(img_url, timeout=12)
                img_res.raise_for_status()
                image = Image.open(io.BytesIO(img_res.content)).convert("RGB")
                image = ImageOps.fit(image, (VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
                image.save(output_image, quality=92)
                used_urls.add(img_url)
                return True
            except Exception:
                continue
    except Exception as e:
        print(f"⚠️ Pexels search failed ({e})", flush=True)
    return False


def fetch_openverse_image(query: str, output_image: str, used_urls: set) -> bool:
    """Fetch a real photograph from Openverse (free, no API key)."""
    try:
        res = requests.get(
            "https://api.openverse.org/v1/images/",
            params={"q": query, "category": "photograph", "mature": "false", "page_size": 8},
            headers={"User-Agent": "NewsCraftAI/1.0"},
            timeout=10,
        )
        res.raise_for_status()
        for item in res.json().get("results", []):
            img_url = item.get("thumbnail") or item.get("url")
            if not img_url or img_url in used_urls:
                continue
            try:
                img_res = requests.get(img_url, headers={"User-Agent": "NewsCraftAI/1.0"}, timeout=12)
                img_res.raise_for_status()
                image = Image.open(io.BytesIO(img_res.content)).convert("RGB")
                image = ImageOps.fit(image, (VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
                image.save(output_image, quality=92)
                used_urls.add(img_url)
                return True
            except Exception:
                continue
    except Exception as e:
        print(f"⚠️ Openverse search failed ({e})", flush=True)
    return False


def fetch_wikimedia_image(query: str, output_image: str, used_urls: set) -> bool:
    """Fetch a real photo from Wikimedia Commons (free, no API key)."""
    try:
        res = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                "gsrnamespace": 6,
                "gsrlimit": 8,
                "prop": "imageinfo",
                "iiprop": "url|mime",
                "iiurlwidth": 1080,
                "format": "json",
            },
            headers={"User-Agent": "NewsCraftAI/1.0"},
            timeout=10,
        )
        res.raise_for_status()
        pages = res.json().get("query", {}).get("pages", {})
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            mime = info.get("mime", "")
            img_url = info.get("thumburl") or info.get("url")
            if not img_url or img_url in used_urls or not mime.startswith("image/") or "svg" in mime:
                continue
            try:
                img_res = requests.get(img_url, headers={"User-Agent": "NewsCraftAI/1.0"}, timeout=12)
                img_res.raise_for_status()
                image = Image.open(io.BytesIO(img_res.content)).convert("RGB")
                image = ImageOps.fit(image, (VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
                image.save(output_image, quality=92)
                used_urls.add(img_url)
                return True
            except Exception:
                continue
    except Exception as e:
        print(f"⚠️ Wikimedia search failed ({e})", flush=True)
    return False


def generate_ai_image(prompt: str, output_image: str, used_urls: Optional[set] = None):
    """Get a relevant 9:16 visual (Pexels -> Pollinations -> Openverse -> Wikimedia -> short query)."""
    if used_urls is None:
        used_urls = set()

    clean_q = clean_search_query(prompt)

    if fetch_pexels_image(clean_q, output_image, used_urls):
        print(f"✅ Image fetched from Pexels ({clean_q})", flush=True)
        return

    enhanced_prompt = (
        f"{prompt}, modern editorial news illustration style, cinematic lighting, "
        "high detail, vibrant colors, vertical 9:16 composition, no text, no watermark"
    )

    try:
        seed = random.randint(1, 999999)
        url = (
            f"https://image.pollinations.ai/prompt/{quote(enhanced_prompt)}"
            f"?width=720&height=1344&seed={seed}&nologo=true"
        )
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        image = Image.open(io.BytesIO(res.content)).convert("RGB")
        w, h = image.size
        image = image.crop((0, 0, w, max(1, h - 64)))
        image = ImageOps.fit(image, (VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
        image.save(output_image, quality=92)
        return
    except Exception as e:
        print(f"⚠️ Pollinations skipped ({e}), searching stock photos...", flush=True)

    if fetch_openverse_image(clean_q, output_image, used_urls):
        print(f"✅ Image fetched from Openverse ({clean_q})", flush=True)
        return

    if fetch_wikimedia_image(clean_q, output_image, used_urls):
        print(f"✅ Image fetched from Wikimedia ({clean_q})", flush=True)
        return

    words = clean_q.split()
    if len(words) > 2:
        short_query = " ".join(words[:2])
        if fetch_pexels_image(short_query, output_image, used_urls):
            print(f"✅ Image fetched from Pexels (short: {short_query})", flush=True)
            return
        if fetch_openverse_image(short_query, output_image, used_urls):
            print(f"✅ Image fetched from Openverse (short: {short_query})", flush=True)
            return
        if fetch_wikimedia_image(short_query, output_image, used_urls):
            print(f"✅ Image fetched from Wikimedia (short: {short_query})", flush=True)
            return

    if fetch_openverse_image("technology news", output_image, used_urls):
        print("✅ Image fetched from Openverse (generic fallback)", flush=True)
        return

    print("⚠️ All image sources failed, using fallback background.", flush=True)
    create_fallback_image(output_image)


def wrap_thai_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    """Wrap text by space-separated words first so Thai words are not split mid-syllable."""
    words = text.split(" ")
    lines = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
                current = word
            else:
                chunk = ""
                for ch in word:
                    test = chunk + ch
                    if draw.textbbox((0, 0), test, font=font)[2] <= max_width or not chunk:
                        chunk = test
                    else:
                        lines.append(chunk)
                        chunk = ch
                current = chunk

    if current:
        lines.append(current)
    return lines


def create_static_chrome_overlay(scene_num: int, total_scenes: int, out_path: str):
    """Create static top/bottom vignette, brand badge, and scene counter PNG."""
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Top and bottom subtle vignette gradients
    for y in range(420):
        alpha = int(175 * (1 - y / 420))
        draw.line([(0, y), (VIDEO_WIDTH, y)], fill=(8, 12, 20, alpha))
    for y in range(VIDEO_HEIGHT - 260, VIDEO_HEIGHT):
        progress = (y - (VIDEO_HEIGHT - 260)) / 260
        alpha = int(165 * progress)
        draw.line([(0, y), (VIDEO_WIDTH, y)], fill=(8, 12, 20, alpha))

    # Top-left brand badge
    badge_font = ImageFont.truetype(FONT_PATH, 24)
    draw.rounded_rectangle([(36, 62), (275, 110)], radius=24, fill=(215, 38, 56, 235))
    draw.text((54, 71), "● NEWSCRAFT AI", font=badge_font, fill=(255, 255, 255, 255))

    # Top-right scene counter pill
    scene_label = f"{scene_num} / {total_scenes}"
    draw.rounded_rectangle([(VIDEO_WIDTH - 135, 62), (VIDEO_WIDTH - 36, 110)], radius=24, fill=(15, 23, 42, 200))
    sb = draw.textbbox((0, 0), scene_label, font=badge_font)
    sx = (VIDEO_WIDTH - 135) + (99 - (sb[2] - sb[0])) // 2
    draw.text((sx, 71), scene_label, font=badge_font, fill=(226, 232, 240, 255))

    # Subtle track for bottom progress bar
    draw.rectangle([(0, VIDEO_HEIGHT - 8), (VIDEO_WIDTH, VIDEO_HEIGHT)], fill=(255, 255, 255, 45))

    overlay.save(out_path)
    overlay.close()


def create_headline_card_overlay(text: str, out_path: str):
    """Create transparent PNG containing only the Thai headline card (for slide-in animation)."""
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    headline_font = ImageFont.truetype(FONT_PATH, 44)
    lines = wrap_thai_text(draw, text.strip(), headline_font, VIDEO_WIDTH - 120)

    line_height = 64
    total_h = len(lines) * line_height
    start_y = 185
    pad_y = 22

    card_top = start_y - pad_y
    card_bottom = start_y + total_h + pad_y
    draw.rounded_rectangle(
        [(36, card_top), (VIDEO_WIDTH - 36, card_bottom)],
        radius=18,
        fill=(12, 18, 30, 205),
        outline=(255, 215, 0, 140),
        width=2,
    )
    draw.rounded_rectangle([(36, card_top + 10), (46, card_bottom - 10)], radius=4, fill=(255, 215, 0, 255))

    y = start_y
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=headline_font)
        text_w = bbox[2] - bbox[0]
        x = (VIDEO_WIDTH - text_w) // 2 + 4
        draw.text(
            (x, y),
            line,
            font=headline_font,
            fill=(255, 230, 0, 255),
            stroke_width=3,
            stroke_fill=(0, 0, 0, 255),
        )
        y += line_height

    overlay.save(out_path)
    overlay.close()


def get_audio_duration(ffmpeg_exe: str, audio_path: str) -> float:
    """Get audio duration in seconds via ffmpeg stderr output."""
    res = subprocess.run([ffmpeg_exe, "-i", audio_path], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
    if not m:
        return 5.0
    h, mins, secs = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return max(1.0, h * 3600 + mins * 60 + secs)


def send_video_to_line(video_url: str, preview_url: str, project_name: str):
    """Push the rendered .mp4 video URL back to the LINE OA chat."""
    if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_USER_ID):
        print("⚠️ LINE_CHANNEL_ACCESS_TOKEN or LINE_USER_ID not configured.", flush=True)
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    data = {
        "to": LINE_USER_ID,
        "messages": [
            {
                "type": "text",
                "text": (
                    f"🎬 คลิปสั้นโปรเจกต์ '{project_name}' เรนเดอร์เสร็จเรียบร้อยแล้วครับ!\n"
                    f"ดาวน์โหลด/เปิดดู: {video_url}"
                ),
            },
            {
                "type": "video",
                "originalContentUrl": video_url,
                "previewImageUrl": preview_url,
            },
        ],
    }
    res = requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=data, timeout=30)
    print(f"LINE Push Status: {res.status_code} {res.text}", flush=True)


def render_single_scene(item: SceneItem, total_scenes: int, work_dir: str, ffmpeg_exe: str, used_urls: set) -> str:
    """Render one 720x1280 scene with alternating camera motion, slide-in headline, and progress bar."""
    idx = item.scene
    audio_path = f"{work_dir}/scene_{idx}.mp3"
    bg_img_path = f"{work_dir}/scene_{idx}_bg.jpg"
    chrome_path = f"{work_dir}/scene_{idx}_chrome.png"
    card_path = f"{work_dir}/scene_{idx}_card.png"
    scene_mp4 = f"{work_dir}/scene_{idx}.mp4"

    print(f"🎨 Scene {idx}: generating image...", flush=True)
    generate_ai_image(item.visual_search_query, bg_img_path, used_urls)
    create_static_chrome_overlay(idx, total_scenes, chrome_path)
    create_headline_card_overlay(item.on_screen_text, card_path)

    duration = get_audio_duration(ffmpeg_exe, audio_path)
    frames = max(40, int(duration * 20) + 10)

    # Alternate camera motion per scene: 1=Zoom-in, 2=Zoom-out, 3=Pan L->R
    motion_mode = idx % 3
    if motion_mode == 1:
        zoom_expr = (
            f"zoompan=z='min(zoom+0.0014,1.10)':d={frames}:"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=720x1280:fps=20"
        )
    elif motion_mode == 2:
        zoom_expr = (
            f"zoompan=z='if(eq(on,1),1.10,max(zoom-0.0012,1.0))':d={frames}:"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=720x1280:fps=20"
        )
    else:
        zoom_expr = (
            f"zoompan=z='1.08':d={frames}:"
            f"x='(iw-iw/zoom)*min(on/{frames},1)':y='ih/2-(ih/zoom/2)':s=720x1280:fps=20"
        )

    filter_complex = (
        f"[0:v]scale=800:1422,{zoom_expr}[bg];"
        "[bg][1:v]overlay=0:0:shortest=1[bg_chrome];"
        "[bg_chrome][2:v]overlay=x='if(lt(t,0.45),-720*(1-t/0.45)*(1-t/0.45),0)':y=0:shortest=1[with_card];"
        f"[with_card]drawbox=x=0:y=1272:w='720*min(t/{duration:.2f},1)':h=8:color=0xFFD700@0.95:t=fill[v]"
    )

    print(f"🎞️ Scene {idx}: encoding motion graphics ({duration:.1f}s)...", flush=True)
    subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-loop",
            "1",
            "-i",
            bg_img_path,
            "-loop",
            "1",
            "-i",
            chrome_path,
            "-loop",
            "1",
            "-i",
            card_path,
            "-i",
            audio_path,
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "3:a",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "1",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-shortest",
            scene_mp4,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    gc.collect()
    return scene_mp4


def render_video_sync(payload: VideoPayload, job_id: str, work_dir: str):
    """Render each scene sequentially and concat with ffmpeg stream copy."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    total_scenes = len(payload.scenes)
    scene_files = []
    used_urls = set()

    for item in payload.scenes:
        scene_mp4 = render_single_scene(item, total_scenes, work_dir, ffmpeg_exe, used_urls)
        scene_files.append(os.path.abspath(scene_mp4))

    safe_project = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in payload.project)
    output_filename = f"{safe_project}_{job_id}.mp4"
    output_path = f"outputs/{output_filename}"
    thumb_filename = f"{safe_project}_{job_id}_thumb.jpg"
    thumb_path = f"outputs/{thumb_filename}"

    concat_list_path = f"{work_dir}/concat.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for path in scene_files:
            f.write(f"file '{path}'\n")

    subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list_path,
            "-c",
            "copy",
            output_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Extract a real thumbnail from 0.8s into the video for LINE preview
    subprocess.run(
        [ffmpeg_exe, "-y", "-ss", "0.8", "-i", output_path, "-vframes", "1", "-q:v", "3", thumb_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    base_url = PUBLIC_BASE_URL.rstrip("/") if PUBLIC_BASE_URL else "http://localhost:8000"
    public_video_url = f"{base_url}/outputs/{output_filename}"
    preview_url = (
        f"{base_url}/outputs/{thumb_filename}"
        if os.path.exists(thumb_path)
        else "https://images.pexels.com/photos/373543/pexels-photo-373543.jpeg?auto=compress&cs=tinysrgb&w=600"
    )
    print(f"✅ Render complete: {output_path}", flush=True)

    send_video_to_line(public_video_url, preview_url, payload.project)


async def process_and_render_video(payload: VideoPayload, job_id: str):
    work_dir = f"temp_{job_id}"
    os.makedirs(work_dir, exist_ok=True)
    print(f"🚀 Job {job_id}: starting ({len(payload.scenes)} scenes)", flush=True)

    try:
        for item in payload.scenes:
            idx = item.scene
            audio_path = f"{work_dir}/scene_{idx}.mp3"
            print(f"🎙️ Scene {idx}: generating TTS...", flush=True)
            await generate_tts(item.voiceover_text, audio_path)

        await asyncio.to_thread(render_video_sync, payload, job_id, work_dir)
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดระหว่างสร้างวิดีโอ: {e}", flush=True)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.post("/create-short-video", summary="สร้างวิดีโอสั้นแนวตั้ง 9:16 จากสคริปต์ JSON")
async def create_short_video(payload: VideoPayload, background_tasks: BackgroundTasks):
    """รับ JSON สคริปต์ข่าวจาก AI Agent แล้วสั่งเรนเดอร์วิดีโอในเบื้องหลังและส่งเข้า LINE"""
    job_id = uuid.uuid4().hex[:6]
    background_tasks.add_task(process_and_render_video, payload, job_id)
    return {
        "status": "processing",
        "job_id": job_id,
        "message": "รับสคริปต์เรียบร้อยแล้ว ระบบกำลังเรนเดอร์วิดีโอและจะส่งเข้าแชท LINE ภายใน 30-45 วินาทีครับ",
    }

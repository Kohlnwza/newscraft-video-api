import asyncio
import gc
import io
import os
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
from huggingface_hub import InferenceClient
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

# Ensure logs appear immediately in Render
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

app = FastAPI(
    title="NewsCraft Video Rendering API",
    description="API สำหรับแปลงสคริปต์ข่าว JSON เป็นวิดีโอสั้นแนวตั้ง 9:16 พร้อมเสียงพากย์ไทยและส่งเข้า LINE OA",
    version="1.0.0",
)

os.makedirs("outputs", exist_ok=True)
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")

HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_USER_ID = os.getenv("LINE_USER_ID", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()

VOICE = "th-TH-NiwatNeural"
FONT_PATH = "thai_font.ttf"
VIDEO_WIDTH = 540
VIDEO_HEIGHT = 960


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
    """Generate Thai voiceover MP3 using edge-tts."""
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_audio)


def create_fallback_image(output_image: str):
    """Create a dark gradient 9:16 background image when HF_TOKEN is unset or API fails."""
    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (18, 24, 38))
    draw = ImageDraw.Draw(img)
    for y in range(VIDEO_HEIGHT):
        r = int(18 + (y / VIDEO_HEIGHT) * 16)
        g = int(24 + (y / VIDEO_HEIGHT) * 28)
        b = int(38 + (y / VIDEO_HEIGHT) * 42)
        draw.line([(0, y), (VIDEO_WIDTH, y)], fill=(r, g, b))
    img.save(output_image)


def generate_ai_image(prompt: str, output_image: str):
    """Generate a vertical 9:16 editorial news image (HF -> Pollinations free AI -> gradient fallback)."""
    enhanced_prompt = (
        f"{prompt}, modern editorial news illustration style, cinematic lighting, "
        "high detail, vibrant colors, vertical 9:16 composition, no text, no watermark"
    )

    if HF_TOKEN:
        try:
            client = InferenceClient(api_key=HF_TOKEN)
            image = client.text_to_image(
                prompt=enhanced_prompt,
                model="black-forest-labs/FLUX.1-schnell",
                width=544,
                height=960,
            )
            image = image.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
            image.save(output_image)
            return
        except Exception as e:
            print(f"⚠️ HF Image failed ({e}), trying Pollinations.ai...", flush=True)

    try:
        url = f"https://image.pollinations.ai/prompt/{quote(enhanced_prompt)}?width=544&height=960&nologo=true"
        res = requests.get(url, timeout=35)
        res.raise_for_status()
        image = Image.open(io.BytesIO(res.content)).convert("RGB")
        image = image.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
        image.save(output_image)
        return
    except Exception as e:
        print(f"⚠️ Pollinations failed ({e}), using fallback background.", flush=True)
        create_fallback_image(output_image)


def wrap_thai_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    """Wrap Thai/mixed text by pixel width."""
    lines = []
    current = ""
    for ch in text:
        test = current + ch
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = test
        else:
            lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines


def overlay_headline_on_image(img_path: str, text: str, out_path: str):
    """Draw Thai headline with semi-transparent dark backdrop and yellow text."""
    base = Image.open(img_path).convert("RGBA").resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font = ImageFont.truetype(FONT_PATH, 38)
    lines = wrap_thai_text(draw, text.strip(), font, VIDEO_WIDTH - 80)

    line_height = 54
    total_h = len(lines) * line_height
    start_y = 180

    # Subtle dark pill box behind text for high contrast
    box_pad_y = 18
    draw.rounded_rectangle(
        [(28, start_y - box_pad_y), (VIDEO_WIDTH - 28, start_y + total_h + box_pad_y)],
        radius=16,
        fill=(0, 0, 0, 150),
    )

    y = start_y
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (VIDEO_WIDTH - text_w) // 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(255, 230, 0, 255),
            stroke_width=3,
            stroke_fill=(0, 0, 0, 255),
        )
        y += line_height

    combined = Image.alpha_composite(base, overlay).convert("RGB")
    combined.save(out_path, quality=92)
    base.close()
    overlay.close()
    combined.close()


def send_video_to_line(video_url: str, project_name: str):
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
                "previewImageUrl": "https://images.pexels.com/photos/373543/pexels-photo-373543.jpeg?auto=compress&cs=tinysrgb&w=600",
            },
        ],
    }
    res = requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=data, timeout=30)
    print(f"LINE Push Status: {res.status_code} {res.text}", flush=True)


def render_single_scene(item: SceneItem, work_dir: str, ffmpeg_exe: str) -> str:
    """Render one scene with pure Pillow + ffmpeg for minimal RAM usage."""
    idx = item.scene
    audio_path = f"{work_dir}/scene_{idx}.mp3"
    raw_img_path = f"{work_dir}/scene_{idx}_raw.png"
    text_img_path = f"{work_dir}/scene_{idx}_txt.jpg"
    scene_mp4 = f"{work_dir}/scene_{idx}.mp4"

    print(f"🎨 Scene {idx}: generating image...", flush=True)
    generate_ai_image(item.visual_search_query, raw_img_path)
    overlay_headline_on_image(raw_img_path, item.on_screen_text, text_img_path)

    print(f"🎞️ Scene {idx}: encoding video...", flush=True)
    subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-loop",
            "1",
            "-framerate",
            "15",
            "-i",
            text_img_path,
            "-i",
            audio_path,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "stillimage",
            "-threads",
            "1",
            "-crf",
            "26",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
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
    scene_files = []

    for item in payload.scenes:
        scene_mp4 = render_single_scene(item, work_dir, ffmpeg_exe)
        scene_files.append(os.path.abspath(scene_mp4))

    safe_project = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in payload.project)
    output_filename = f"{safe_project}_{job_id}.mp4"
    output_path = f"outputs/{output_filename}"

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

    base_url = PUBLIC_BASE_URL.rstrip("/") if PUBLIC_BASE_URL else "http://localhost:8000"
    public_video_url = f"{base_url}/outputs/{output_filename}"
    print(f"✅ Render complete: {output_path}", flush=True)

    send_video_to_line(public_video_url, payload.project)


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

import asyncio
import gc
import os
import shutil
import subprocess
import uuid
from typing import List, Optional

import edge_tts
import imageio_ffmpeg
import requests
from fastapi import BackgroundTasks, FastAPI
from fastapi.staticfiles import StaticFiles
from huggingface_hub import InferenceClient
from moviepy import (
    AudioFileClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
)
from PIL import Image, ImageDraw
from pydantic import BaseModel

app = FastAPI(
    title="NewsCraft Video Rendering API",
    description="API สำหรับแปลงสคริปต์ข่าว JSON เป็นวิดีโอสั้นแนวตั้ง 9:16 พร้อมเสียงพากย์ไทยและส่งเข้า LINE OA",
    version="1.0.0",
)

os.makedirs("outputs", exist_ok=True)
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")

HF_TOKEN = os.getenv("HF_TOKEN", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")

VOICE = "th-TH-NiwatNeural"
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
    """Generate Thai voiceover MP3 using edge-tts."""
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_audio)


def create_fallback_image(output_image: str):
    """Create a dark gradient 9:16 background image when HF_TOKEN is unset or API fails."""
    width, height = 544, 960
    img = Image.new("RGB", (width, height), (18, 24, 38))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        r = int(18 + (y / height) * 16)
        g = int(24 + (y / height) * 28)
        b = int(38 + (y / height) * 42)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    img.save(output_image)


def generate_ai_image(prompt: str, output_image: str):
    """Generate a vertical 9:16 editorial news image via Hugging Face FLUX.1-schnell."""
    if not HF_TOKEN:
        print("⚠️ HF_TOKEN not set, using fallback gradient background.")
        create_fallback_image(output_image)
        return

    try:
        client = InferenceClient(api_key=HF_TOKEN)
        enhanced_prompt = (
            f"{prompt}, modern editorial news illustration style, cinematic lighting, "
            "high detail, vibrant colors, vertical 9:16 composition, no text, no watermark"
        )
        image = client.text_to_image(
            prompt=enhanced_prompt,
            model="black-forest-labs/FLUX.1-schnell",
            width=544,
            height=960,
        )
        image.save(output_image)
    except Exception as e:
        print(f"⚠️ Image generation failed ({e}), using fallback background.")
        create_fallback_image(output_image)


def apply_ken_burns_zoom(img_path: str, duration: float):
    """Apply subtle Ken Burns zoom-in effect to a 720x1280 image clip."""
    clip = ImageClip(img_path).with_duration(duration).resized((VIDEO_WIDTH, VIDEO_HEIGHT))
    zoomed_clip = clip.resized(lambda t: 1.0 + 0.06 * (t / duration))
    return CompositeVideoClip([zoomed_clip.with_position("center")], size=(VIDEO_WIDTH, VIDEO_HEIGHT))


def send_video_to_line(video_url: str, project_name: str):
    """Push the rendered .mp4 video URL back to the LINE OA chat."""
    if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_USER_ID):
        print("⚠️ LINE_CHANNEL_ACCESS_TOKEN or LINE_USER_ID not configured.")
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
    print("LINE Push Status:", res.status_code, res.text)


def render_single_scene(item: SceneItem, work_dir: str, duration: float) -> str:
    """Render one scene to a standalone MP4 to keep peak RAM well below 512 MB."""
    idx = item.scene
    audio_path = f"{work_dir}/scene_{idx}.mp3"
    img_path = f"{work_dir}/scene_{idx}.png"
    scene_mp4 = f"{work_dir}/scene_{idx}.mp4"

    generate_ai_image(item.visual_search_query, img_path)

    audio_clip = None
    bg_clip = None
    txt_clip = None
    scene_composite = None

    try:
        audio_clip = AudioFileClip(audio_path)
        bg_clip = apply_ken_burns_zoom(img_path, duration)
        txt_clip = (
            TextClip(
                text=item.on_screen_text,
                font=FONT_PATH,
                font_size=46,
                color="yellow",
                stroke_color="black",
                stroke_width=3,
                method="caption",
                size=(620, None),
                margin=(16, 16),
            )
            .with_position(("center", 230))
            .with_duration(duration)
        )
        scene_composite = CompositeVideoClip([bg_clip, txt_clip]).with_audio(audio_clip)
        scene_composite.write_videofile(
            scene_mp4,
            fps=24,
            codec="libx264",
            audio_codec="aac",
            ffmpeg_params=["-preset", "ultrafast", "-threads", "1", "-crf", "26"],
            logger=None,
        )
    finally:
        for clip in (scene_composite, txt_clip, bg_clip, audio_clip):
            if clip is not None:
                clip.close()
        gc.collect()

    return scene_mp4


def render_video_sync(payload: VideoPayload, job_id: str, work_dir: str, audio_durations: dict):
    """Render each scene sequentially and concat with ffmpeg stream copy."""
    scene_files = []
    for item in payload.scenes:
        scene_mp4 = render_single_scene(item, work_dir, audio_durations[item.scene])
        scene_files.append(os.path.abspath(scene_mp4))

    safe_project = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in payload.project)
    output_filename = f"{safe_project}_{job_id}.mp4"
    output_path = f"outputs/{output_filename}"

    concat_list_path = f"{work_dir}/concat.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for path in scene_files:
            f.write(f"file '{path}'\n")

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
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
    )

    base_url = PUBLIC_BASE_URL.rstrip("/") if PUBLIC_BASE_URL else "http://localhost:8000"
    public_video_url = f"{base_url}/outputs/{output_filename}"
    print(f"✅ Render complete: {output_path}")

    send_video_to_line(public_video_url, payload.project)


async def process_and_render_video(payload: VideoPayload, job_id: str):
    work_dir = f"temp_{job_id}"
    os.makedirs(work_dir, exist_ok=True)
    audio_durations = {}

    try:
        for item in payload.scenes:
            idx = item.scene
            audio_path = f"{work_dir}/scene_{idx}.mp3"
            await generate_tts(item.voiceover_text, audio_path)
            a_clip = AudioFileClip(audio_path)
            audio_durations[idx] = a_clip.duration + 0.3
            a_clip.close()

        await asyncio.to_thread(render_video_sync, payload, job_id, work_dir, audio_durations)
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดระหว่างสร้างวิดีโอ: {e}")
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

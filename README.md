# NewsCraft Video Rendering API

Backend FastAPI สำหรับ **NewsCraft AI** ทำหน้าที่รับ JSON สคริปต์ข่าวจาก LangMind มาสร้างเป็นวิดีโอสั้นแนวตั้ง `9:16` (`1080x1920`) พร้อม:
- เสียงพากย์ภาษาไทย (`edge-tts`)
- ภาพประกอบ AI (`black-forest-labs/FLUX.1-schnell` ผ่าน Hugging Face Inference API) + เอฟเฟกต์ซูม (Ken Burns)
- ข้อความพาดหัวภาษาไทย (`thai_font.ttf`)
- ส่งไฟล์ `.mp4` กลับเข้าแชท LINE OA อัตโนมัติ

## ไฟล์ในโปรเจกต์
- `main.py` — FastAPI server และ pipeline เรนเดอร์วิดีโอ
- `requirements.txt` — Python dependencies
- `thai_font.ttf` — ฟอนต์ภาษาไทยสำหรับเรนเดอร์ข้อความบนคลิป
- `outputs/` — โฟลเดอร์เก็บไฟล์ `.mp4` ที่เรนเดอร์เสร็จแล้ว
- `.env.example` — ตัวอย่างตัวแปร Environment Variables

## การทดสอบรันบนเครื่อง (Local)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
เปิดดูเอกสาร API และ `/openapi.json` ได้ที่: `http://localhost:8000/docs`

## การ Deploy ขึ้น Render (Free Tier / ไม่ใช้ Docker)
1. อัปโหลดไฟล์ทั้งหมดขึ้น GitHub Repository
2. ไปที่ [render.com](https://render.com) -> **New +** -> **Web Service** -> เชื่อมต่อ GitHub Repo
3. ตั้งค่า:
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** `Free`
4. เพิ่ม **Environment Variables** 4 ตัว:
   - `HF_TOKEN` — Hugging Face Access Token (`hf_...`)
   - `LINE_CHANNEL_ACCESS_TOKEN` — Channel Access Token ของ LINE OA
   - `LINE_USER_ID` — User ID หรือ Group ID ปลายทางใน LINE
   - `PUBLIC_BASE_URL` — URL ของเว็บ Render เช่น `https://newscraft-video-api.onrender.com`

## การเชื่อมต่อเข้า LangMind
1. ใน LangMind ไปที่เมนู **เครื่องมือ** -> **นำเข้าการตั้งค่าและบริการขั้นสูง**
2. ใส่ `https://<your-render-url>.onrender.com/openapi.json` ในช่อง **SPEC URL** -> กด **โหลด** -> **+ เพิ่ม**
3. เข้าไปที่เอเจนต์ **NewsCraft AI** -> **เครื่องมือ** -> เปิดสวิตช์เครื่องมือใหม่ -> กด **เผยแพร่**

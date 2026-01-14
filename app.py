import os, subprocess, signal, json, logging, threading, time, shutil, psutil
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Body
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from yt_dlp import YoutubeDL

# --- SETUP ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("OVERLORD-ENGINE")

BASE_DIR = Path(__file__).parent
LIVE_DIR = BASE_DIR / "static/live"
UPLOAD_DIR = BASE_DIR / "static/uploads"
DB_FILE = BASE_DIR / "db.json"
for d in [LIVE_DIR, UPLOAD_DIR]: d.mkdir(parents=True, exist_ok=True)

def load_db():
    if not DB_FILE.exists(): return {"media": [], "queue": []}
    try: return json.loads(DB_FILE.read_text())
    except: return {"media": [], "queue": []}

def save_db(data):
    DB_FILE.write_text(json.dumps(data, indent=2))

# --- YT-DLP HELPER ---
def get_stream_url(url):
    if not any(x in url for x in ["youtube.com", "youtu.be", "twitch.tv", "twitter.com"]):
        return url # Direct CDN link
    try:
        ydl_opts = {'format': 'best[ext=mp4]/best', 'quiet': True, 'noplaylist': True}
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info['url']
    except Exception as e:
        logger.error(f"URL Resolve Error: {e}")
        return None

# --- ENGINE ---
class OverlordEngine:
    def __init__(self):
        self.process = None
        self.current_video = "IDLE"
        self.lock = threading.Lock()

    def stop(self):
        with self.lock:
            if self.process:
                self.process.terminate()
                self.process.wait()
                self.process = None
            self.current_video = "IDLE"

    def start(self, source, title):
        self.stop()
        actual_url = get_stream_url(source)
        if not actual_url: return
        
        # Render Optimized FFmpeg with Watermark
        cmd = [
            "ffmpeg", "-re", "-i", actual_url,
            "-vf", "scale=-2:480,drawtext=text='ASHLABS LIVE':x=w-130:y=20:fontsize=20:fontcolor=white@0.6",
            "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "700k", "-maxrate", "700k", "-bufsize", "1400k",
            "-c:a", "aac", "-b:a", "64k", "-f", "hls", "-hls_time", "4", "-hls_list_size", "5",
            "-hls_flags", "delete_segments+discont_start", str(LIVE_DIR / "index.m3u8"), "-y"
        ]
        with self.lock:
            self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.current_video = title

    def monitor(self):
        while True:
            db = load_db()
            if db["queue"] and (not self.process or self.process.poll() is not None):
                next_id = db["queue"].pop(0)
                media = next((m for m in db["media"] if m["id"] == next_id), None)
                if media:
                    db["queue"].append(next_id) # Loop
                    save_db(db)
                    self.start(media["url"], media["title"])
            time.sleep(3)

engine = OverlordEngine()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/stream", StaticFiles(directory=LIVE_DIR), name="stream")

@app.on_event("startup")
def startup(): threading.Thread(target=engine.monitor, daemon=True).start()

# --- API ---
@app.get("/api/status")
def status():
    return {"current": engine.current_video, "cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent, "queue": load_db()["queue"]}

@app.post("/api/media/remote")
def add_remote(url: str, title: str):
    db = load_db()
    id = str(int(time.time()))
    db["media"].append({"id": id, "title": f"🌐 {title}", "url": url})
    save_db(db)
    return {"id": id}

@app.post("/api/queue/reorder")
def reorder_queue(order: List[str] = Body(...)):
    db = load_db()
    db["queue"] = order
    save_db(db)
    return {"status": "Queue Updated"}

@app.post("/api/queue/add")
def add_queue(id: str):
    db = load_db()
    db["queue"].append(id)
    save_db(db)
    return {"status": "Added"}

@app.post("/api/stream/skip")
def skip(): engine.stop(); return {"status": "Skipped"}

@app.get("/api/media")
def list_media(): return load_db()["media"]

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ts = str(int(time.time()))
    path = UPLOAD_DIR / f"{ts}_{file.filename}"
    with path.open("wb") as f: shutil.copyfileobj(file.file, f)
    db = load_db()
    db["media"].append({"id": ts, "title": file.filename, "url": f"static/uploads/{ts}_{file.filename}"})
    save_db(db)
    return {"status": "Uploaded"}

@app.delete("/api/media/{id}")
def delete_media(id: str):
    db = load_db()
    db["media"] = [m for m in db["media"] if m["id"] != id]
    db["queue"] = [qid for qid in db["queue"] if qid != id]
    save_db(db)
    return {"status": "Deleted"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)

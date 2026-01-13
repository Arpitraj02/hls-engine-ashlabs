import os
import subprocess
import signal
import json
import logging
import threading
import time
import shutil
import psutil
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- SETUP & LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("LIVE-ENGINE")

BASE_DIR = Path(__file__).parent
LIVE_DIR = BASE_DIR / "static" / "live"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
DB_FILE = BASE_DIR / "db.json"

for d in [LIVE_DIR, UPLOAD_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# --- MODELS ---
class StreamRequest(BaseModel):
    media_id: Optional[str] = None
    group_name: Optional[str] = None
    loop: bool = True

# --- DATABASE ---
def load_db():
    if not DB_FILE.exists(): return {"media": [], "groups": {}}
    return json.loads(DB_FILE.read_text())

def save_db(data):
    DB_FILE.write_text(json.dumps(data, indent=2))

# --- THE ENGINE ---
class LiveStreamEngine:
    def __init__(self):
        self.process = None
        self.playlist = []
        self.index = 0
        self.is_looping = True
        self.active_title = "IDLE"
        self.start_time = 0
        self.lock = threading.Lock()

    def kill_ffmpeg(self):
        with self.lock:
            if self.process:
                logger.info("Killing FFmpeg process...")
                self.process.terminate()
                try: self.process.wait(timeout=5)
                except: self.process.kill()
                self.process = None
            self.active_title = "IDLE"

    def start_ffmpeg(self, source_path, title):
        self.kill_ffmpeg()
        
        # Determine if it's a URL or local file
        input_src = source_path
        if not source_path.startswith(("http", "https")):
            input_src = str(BASE_DIR / source_path)

        # OPTIMIZED COMMAND FOR RENDER (512MB RAM)
        # -re: Read input at native frame rate (Essential for True Live)
        cmd = [
            "ffmpeg", "-re", "-i", input_src,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", "700k", "-maxrate", "700k", "-bufsize", "1400k",
            "-vf", "scale=-2:480", # Scale to 480p to keep CPU/RAM low
            "-c:a", "aac", "-b:a", "64k", "-ar", "44100",
            "-f", "hls",
            "-hls_time", "4", 
            "-hls_list_size", "6", # Keeps ~24 seconds of buffer
            "-hls_flags", "delete_segments+append_list+discont_start",
            "-hls_segment_filename", str(LIVE_DIR / "seg_%03d.ts"),
            str(LIVE_DIR / "index.m3u8"),
            "-y"
        ]

        with self.lock:
            self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.active_title = title
            self.start_time = time.time()
            logger.info(f"LIVE NOW: {title}")

    def monitor_thread(self):
        """Infinite loop to manage transitions and playlist looping."""
        while True:
            if self.playlist:
                # If FFmpeg is not running (video ended or crashed)
                if not self.process or self.process.poll() is not None:
                    if self.index >= len(self.playlist):
                        if self.is_looping:
                            logger.info("Playlist ended. Looping back to start.")
                            self.index = 0
                        else:
                            logger.info("Playlist ended. Stopping.")
                            self.playlist = []
                            continue
                    
                    db = load_db()
                    mid = self.playlist[self.index]
                    media = next((m for m in db["media"] if m["id"] == mid), None)
                    
                    if media:
                        self.start_ffmpeg(media["url"], media["title"])
                        self.index += 1
                    else:
                        self.index += 1 # Skip if media deleted
            time.sleep(2)

engine = LiveStreamEngine()

# --- FASTAPI APP ---
app = FastAPI(title="24/7 Live HLS Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/stream", StaticFiles(directory=LIVE_DIR), name="stream")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

@app.on_event("startup")
def startup():
    threading.Thread(target=engine.monitor_thread, daemon=True).start()

@app.get("/api/status")
def get_status():
    return {
        "status": "LIVE" if engine.process and engine.process.poll() is None else "IDLE",
        "now_playing": engine.active_title,
        "ram": f"{psutil.virtual_memory().percent}%",
        "cpu": f"{psutil.cpu_percent()}%",
        "playlist_index": engine.index
    }

@app.get("/api/media")
def list_media():
    return load_db()["media"]

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ts = str(int(time.time()))
    filename = f"{ts}_{file.filename}"
    path = UPLOAD_DIR / filename
    
    with path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    db = load_db()
    new_media = {
        "id": ts,
        "title": file.filename,
        "url": f"static/uploads/{filename}"
    }
    db["media"].append(new_media)
    save_db(db)
    return new_media

@app.post("/api/stream/start")
def start(req: StreamRequest):
    db = load_db()
    if req.group_name:
        engine.playlist = db["groups"].get(req.group_name, [])
        engine.index = 0
    elif req.media_id:
        engine.playlist = [req.media_id]
        engine.index = 0
    
    engine.is_looping = req.loop
    engine.kill_ffmpeg() # Forces monitor to start the new playlist
    return {"message": "Stream starting..."}

@app.post("/api/stream/stop")
def stop():
    engine.playlist = []
    engine.kill_ffmpeg()
    return {"message": "Stream stopped"}

@app.post("/api/stream/skip")
def skip():
    engine.kill_ffmpeg() # Monitor will immediately start the next video
    return {"message": "Skipping..."}

@app.post("/api/groups")
def create_group(name: str, ids: List[str]):
    db = load_db()
    db["groups"][name] = ids
    save_db(db)
    return {"status": "Group saved"}

@app.delete("/api/media/{mid}")
def delete_media(mid: str):
    db = load_db()
    item = next((m for m in db["media"] if m["id"] == mid), None)
    if item:
        try: os.remove(BASE_DIR / item["url"])
        except: pass
        db["media"] = [m for m in db["media"] if m["id"] != mid]
        save_db(db)
    return {"status": "deleted"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

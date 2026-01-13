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
from typing import List, Optional, Dict
from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HLS-Engine")

BASE_DIR = Path(__file__).parent
LIVE_DIR = BASE_DIR / "static" / "live"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
DB_FILE = BASE_DIR / "database.json"

for folder in [LIVE_DIR, UPLOAD_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

# --- MODELS ---
class StreamConfig(BaseModel):
    url: Optional[str] = None
    group_id: Optional[str] = None
    loop: bool = True

# --- DATABASE LOGIC ---
class Database:
    @staticmethod
    def load():
        if not DB_FILE.exists():
            return {"media": [], "groups": {}}
        return json.loads(DB_FILE.read_text())

    @staticmethod
    def save(data):
        DB_FILE.write_text(json.dumps(data, indent=2))

# --- STREAM MANAGER (The Brain) ---
class StreamManager:
    def __init__(self):
        self.process = None
        self.playlist = []
        self.current_index = 0
        self.is_looping = False
        self.stop_requested = False
        self.lock = threading.Lock()
        self.status = "IDLE"
        self.current_video_title = "None"

    def stop_ffmpeg(self):
        with self.lock:
            if self.process:
                logger.info("Stopping FFmpeg...")
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except:
                    self.process.kill()
                self.process = None
            self.status = "IDLE"

    def start_ffmpeg(self, video_url: str, title: str):
        self.stop_ffmpeg()
        
        # Determine if source is local or URL
        input_source = video_url
        if not video_url.startswith(("http", "https")):
            input_source = str(BASE_DIR / video_url)

        # RENDER OPTIMIZED COMMAND
        cmd = [
            "ffmpeg", "-re", "-i", input_source,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", "600k", "-maxrate", "600k", "-bufsize", "1000k", 
            "-vf", "scale=-2:480", # Downscale to 480p to save RAM
            "-c:a", "aac", "-ar", "44100", "-b:a", "64k",
            "-f", "hls", "-hls_time", "4", "-hls_list_size", "3",
            "-hls_flags", "delete_segments+append_list+discont_start",
            "-hls_segment_filename", str(LIVE_DIR / "seg_%03d.ts"),
            str(LIVE_DIR / "index.m3u8"), "-y"
        ]

        with self.lock:
            self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.status = "LIVE"
            self.current_video_title = title
            logger.info(f"Streaming: {title}")

    def skip(self):
        """Kills current process; the monitor thread will automatically pick up the next video."""
        logger.info("Skip requested.")
        self.stop_ffmpeg()

    def monitor_loop(self):
        """Orchestrates the 24/7 playlist logic."""
        while True:
            if not self.stop_requested and self.playlist:
                # If nothing is playing, start the next one
                if self.process is None or self.process.poll() is not None:
                    if self.current_index >= len(self.playlist):
                        if self.is_looping:
                            self.current_index = 0
                        else:
                            self.playlist = []
                            continue
                    
                    video_id = self.playlist[self.current_index]
                    db = Database.load()
                    video_data = next((m for m in db["media"] if m["id"] == video_id), None)
                    
                    if video_data:
                        self.start_ffmpeg(video_data["url"], video_data["title"])
                        self.current_index += 1
                    else:
                        self.current_index += 1 # Skip missing files
            time.sleep(3)

manager = StreamManager()

# --- FASTAPI APP ---
app = FastAPI(title="Ultimate HLS Engine")

# ENABLE CORS (So your React frontend can connect)
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
    threading.Thread(target=manager.monitor_loop, daemon=True).start()
    # Disk Cleanup Thread
    def cleanup():
        while True:
            now = time.time()
            for f in LIVE_DIR.glob("*.ts"):
                if now - f.stat().st_mtime > 60: f.unlink()
            time.sleep(30)
    threading.Thread(target=cleanup, daemon=True).start()

# --- ENDPOINTS ---

@app.get("/health")
def health(): return {"status": "ok"}

@app.get("/api/status")
def get_status():
    return {
        "status": manager.status,
        "current_video": manager.current_video_title,
        "playlist_pos": f"{manager.current_index}/{len(manager.playlist)}",
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent
    }

@app.get("/api/media")
def list_media(): return Database.load()["media"]

@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    file_path = UPLOAD_DIR / file.filename
    with file_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    db = Database.load()
    new_id = str(int(time.time()))
    db["media"].append({
        "id": new_id,
        "title": file.filename,
        "url": f"static/uploads/{file.filename}"
    })
    Database.save(db)
    return {"status": "Uploaded", "id": new_id}

@app.post("/api/stream/start")
def start_stream(config: StreamConfig):
    manager.stop_requested = False
    if config.group_id:
        db = Database.load()
        manager.playlist = db["groups"].get(config.group_id, [])
        manager.current_index = 0
        manager.is_looping = config.loop
        manager.stop_ffmpeg() # Force restart with new playlist
    elif config.url:
        manager.playlist = [] 
        manager.start_ffmpeg(config.url, "Manual URL")
    return {"message": "Command received"}

@app.post("/api/stream/stop")
def stop_all():
    manager.stop_requested = True
    manager.playlist = []
    manager.stop_ffmpeg()
    return {"message": "Stopped"}

@app.post("/api/stream/skip")
def skip_video():
    manager.skip()
    return {"message": "Skipped to next video"}

@app.post("/api/groups")
def create_group(name: str, video_ids: List[str]):
    db = Database.load()
    db["groups"][name] = video_ids
    Database.save(db)
    return {"status": "Group Created"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

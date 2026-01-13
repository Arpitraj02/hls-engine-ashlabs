import os
import subprocess
import signal
import json
import logging
import threading
import time
import psutil
from pathlib import Path
from typing import List, Optional, Dict
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, BackgroundTasks, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# --- CONFIGURATION & LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("HLS-Engine")

BASE_DIR = Path(__file__).parent
LIVE_DIR = BASE_DIR / "static" / "live"
DB_FILE = BASE_DIR / "database.json"
LIVE_DIR.mkdir(parents=True, exist_ok=True)

# --- MODELS ---
class Media(BaseModel):
    id: str
    title: str
    url: str

class Group(BaseModel):
    name: str
    video_ids: List[str]

class StreamConfig(BaseModel):
    url: Optional[str] = None
    group_id: Optional[str] = None
    loop: bool = True

# --- CORE ENGINE: STREAM MANAGER ---
class StreamManager:
    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.active_metadata: Dict = {"status": "IDLE", "current": None}
        self.lock = threading.Lock()
        self.stop_signal = False
        
        # Playlist state
        self.playlist_queue: List[str] = []
        self.current_index = 0
        self.is_looping = False

    def _get_video_url(self, video_id: str) -> Optional[str]:
        db = Database.load()
        for m in db["media"]:
            if m["id"] == video_id:
                return m["url"]
        return None

    def kill_ffmpeg(self):
        with self.lock:
            if self.process:
                logger.info("Terminating existing FFmpeg process...")
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                self.process = None
            self.active_metadata["status"] = "IDLE"
            self.active_metadata["current"] = None

    def start_ffmpeg(self, url: str):
        self.kill_ffmpeg()
        
        # Render-optimized FFmpeg Command
        cmd = [
            "ffmpeg", "-re", "-i", url,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", "700k", "-maxrate", "700k", "-bufsize", "1000k", # Strict caps
            "-vf", "scale=-2:480", # Downscale to 480p to save RAM/CPU
            "-c:a", "aac", "-ar", "44100", "-b:a", "64k",
            "-f", "hls",
            "-hls_time", "4",
            "-hls_list_size", "3",
            "-hls_flags", "delete_segments+append_list+discont_start",
            "-hls_segment_filename", str(LIVE_DIR / "chunk_%03d.ts"),
            str(LIVE_DIR / "index.m3u8"),
            "-y"
        ]

        with self.lock:
            try:
                self.process = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
                )
                self.active_metadata["status"] = "LIVE"
                self.active_metadata["current"] = url
                logger.info(f"Stream started: {url}")
            except Exception as e:
                logger.error(f"Failed to start FFmpeg: {e}")

    def playlist_monitor(self):
        """Watcher thread that manages transitions and loops."""
        while True:
            if not self.stop_signal and self.playlist_queue:
                # Check if process is dead
                if not self.process or self.process.poll() is not None:
                    if self.current_index >= len(self.playlist_queue):
                        if self.is_looping:
                            self.current_index = 0
                            logger.info("Looping playlist back to start.")
                        else:
                            self.playlist_queue = []
                            logger.info("Playlist finished.")
                            continue
                    
                    target_id = self.playlist_queue[self.current_index]
                    target_url = self._get_video_url(target_id)
                    
                    if target_url:
                        self.start_ffmpeg(target_url)
                        self.current_index += 1
                    else:
                        self.current_index += 1 # Skip broken IDs
            
            time.sleep(3)

# --- DATABASE LAYER ---
class Database:
    @staticmethod
    def load():
        if not DB_FILE.exists():
            return {"media": [], "groups": {}}
        return json.loads(DB_FILE.read_text())

    @staticmethod
    def save(data):
        DB_FILE.write_text(json.dumps(data, indent=2))

# --- SYSTEM UTILS ---
def cleanup_segments():
    """Disk Guard: Prevent Render's ephemeral disk from filling up."""
    while True:
        try:
            now = time.time()
            for file in LIVE_DIR.glob("*.ts"):
                if now - file.stat().st_mtime > 60: # Delete segments older than 1m
                    file.unlink()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        time.sleep(30)

def resource_monitor():
    """Memory Guard: Auto-restart if RAM exceeds 85% to prevent OOM crash."""
    while True:
        mem = psutil.virtual_memory().percent
        if mem > 85:
            logger.warning(f"HIGH MEMORY USAGE ({mem}%). Resetting Stream...")
            engine.kill_ffmpeg()
        time.sleep(60)

# --- API INITIALIZATION ---
app = FastAPI(title="Pro HLS Engine")
app.mount("/stream", StaticFiles(directory=LIVE_DIR), name="stream")
engine = StreamManager()

@app.on_event("startup")
def startup_event():
    threading.Thread(target=engine.playlist_monitor, daemon=True).start()
    threading.Thread(target=cleanup_segments, daemon=True).start()
    threading.Thread(target=resource_monitor, daemon=True).start()

# --- API ENDPOINTS ---

@app.get("/health")
async def health():
    return {"status": "online", "timestamp": datetime.now()}

@app.get("/api/status")
async def get_status():
    return {
        "status": engine.active_metadata["status"],
        "current_video": engine.active_metadata["current"],
        "system": {
            "cpu": f"{psutil.cpu_percent()}%",
            "ram": f"{psutil.virtual_memory().percent}%",
            "uptime": "..." # Add uptime logic if needed
        }
    }

@app.get("/api/media")
async def list_media():
    return Database.load()["media"]

@app.post("/api/media")
async def add_media(m: Media):
    db = Database.load()
    db["media"].append(m.dict())
    Database.save(db)
    return {"status": "added"}

@app.post("/api/stream/start")
async def start_stream(config: StreamConfig):
    engine.stop_signal = False
    
    if config.group_id:
        db = Database.load()
        if config.group_id not in db["groups"]:
            raise HTTPException(status_code=404, detail="Group not found")
        
        engine.playlist_queue = db["groups"][config.group_id]
        engine.current_index = 0
        engine.is_looping = config.loop
        engine.kill_ffmpeg() # Trigger the playlist monitor
        return {"message": f"Playlist {config.group_id} started"}
    
    if config.url:
        engine.playlist_queue = [] # Clear playlist if playing solo
        engine.start_ffmpeg(config.url)
        return {"message": "Solo stream started"}

@app.post("/api/stream/stop")
async def stop_stream():
    engine.stop_signal = True
    engine.playlist_queue = []
    engine.kill_ffmpeg()
    return {"status": "stopped"}

@app.post("/api/groups")
async def manage_groups(g: Group):
    db = Database.load()
    db["groups"][g.name] = g.video_ids
    Database.save(db)
    return {"status": "group_updated"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

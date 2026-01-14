import os, subprocess, signal, json, logging, threading, time, shutil, psutil
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Body, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from yt_dlp import YoutubeDL

# --- INITIALIZATION & DIRECTORIES ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("ASHLABS-OVERLORD")

BASE_DIR = Path(__file__).parent
LIVE_DIR = BASE_DIR / "static/live"
UPLOAD_DIR = BASE_DIR / "static/uploads"
DB_FILE = BASE_DIR / "db.json"

for d in [LIVE_DIR, UPLOAD_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# --- DATABASE LOGIC ---
def load_db():
    if not DB_FILE.exists(): return {"media": [], "queue": []}
    try:
        content = DB_FILE.read_text()
        return json.loads(content) if content else {"media": [], "queue": []}
    except: return {"media": [], "queue": []}

def save_db(data):
    DB_FILE.write_text(json.dumps(data, indent=2))

# --- YT-DLP RESOLVER (Extracts direct MP4 from YouTube/Twitch) ---
def resolve_url(url):
    if not any(x in url for x in ["youtube.com", "youtu.be", "twitch.tv"]):
        return url
    try:
        ydl_opts = {'format': 'best[ext=mp4]/best', 'quiet': True, 'noplaylist': True}
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info['url']
    except Exception as e:
        logger.error(f"yt-dlp resolution failed: {e}")
        return None

# --- CORE BROADCASTER ENGINE ---
class OverlordEngine:
    def __init__(self):
        self.process = None
        self.current_title = "OFFLINE"
        self.is_auto = True
        self.is_looping = True # Looping Toggle
        self.lock = threading.Lock()

    def kill_process(self):
        with self.lock:
            if self.process:
                logger.info("Killing FFmpeg process...")
                self.process.terminate()
                try: self.process.wait(timeout=2)
                except: self.process.kill()
                self.process = None
            self.current_title = "OFFLINE"

    def start_ffmpeg(self, source, title):
        self.kill_process()
        stream_url = resolve_url(source)
        if not stream_url: return

        # Render Optimization: 480p, 600k bitrate, Ultrafast preset
        cmd = [
            "ffmpeg", "-re", "-i", stream_url,
            "-vf", "scale=-2:480,drawtext=text='ASHLABS LIVE':x=w-140:y=20:fontsize=20:fontcolor=white@0.7:box=1:boxcolor=black@0.4",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", 
            "-b:v", "600k", "-maxrate", "600k", "-bufsize", "1200k",
            "-c:a", "aac", "-b:a", "64k", "-ar", "44100",
            "-f", "hls", "-hls_time", "4", "-hls_list_size", "5",
            "-hls_flags", "delete_segments+discont_start", 
            str(LIVE_DIR / "index.m3u8"), "-y"
        ]
        
        with self.lock:
            self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.current_title = title
            logger.info(f"Broadcast Started: {title}")

    def monitor_loop(self):
        """Playlist Manager: Plays next video in queue automatically"""
        while True:
            if self.is_auto:
                db = load_db()
                if db["queue"] and (not self.process or self.process.poll() is not None):
                    next_id = db["queue"].pop(0)
                    media = next((m for m in db["media"] if m["id"] == next_id), None)
                    
                    if media:
                        if self.is_looping:
                            db["queue"].append(next_id) # Add back to end if loop enabled
                        
                        save_db(db)
                        self.start_ffmpeg(media["url"], media["title"])
            
            # Resource Guard: Safety restart if RAM usage > 90%
            if psutil.virtual_memory().percent > 90:
                logger.warning("RAM limit reached. Resetting stream...")
                self.kill_process()

            time.sleep(3)

engine = OverlordEngine()

# --- FASTAPI SETUP ---
app = FastAPI(title="Ashlabs Overlord Engine")

# CORS ENABLED (Crucial for frontend connection)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/stream", StaticFiles(directory=LIVE_DIR), name="stream")

@app.on_event("startup")
def startup_event():
    # Start Monitor Thread
    threading.Thread(target=engine.monitor_loop, daemon=True).start()
    # Cleanup old segments on boot
    for f in LIVE_DIR.glob("*.ts"): f.unlink()

# --- API ENDPOINTS ---

@app.get("/api/status")
def get_status():
    return {
        "is_live": engine.process is not None and engine.process.poll() is None,
        "current": engine.current_title,
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent,
        "is_looping": engine.is_looping,
        "queue": load_db()["queue"]
    }

@app.post("/api/control/loop")
def toggle_loop(enable: bool = Query(...)):
    engine.is_looping = enable
    return {"status": f"Looping {'Enabled' if enable else 'Disabled'}"}

@app.post("/api/control/stop")
def stop_stream():
    engine.is_auto = False
    engine.kill_process()
    return {"status": "Broadcast Stopped"}

@app.post("/api/control/start")
def start_stream():
    engine.is_auto = True
    return {"status": "Auto-Pilot Activated"}

@app.post("/api/control/skip")
def skip_stream():
    engine.kill_process()
    return {"status": "Skipped"}

@app.get("/api/media")
def list_media():
    return load_db()["media"]

@app.post("/api/media/remote")
def add_remote(url: str, title: str):
    db = load_db()
    media_id = str(int(time.time()))
    db["media"].append({"id": media_id, "title": f"🔗 {title}", "url": url})
    save_db(db)
    return {"id": media_id}

@app.post("/api/queue/add")
def add_to_queue(id: str = Query(...)):
    db = load_db()
    if any(m["id"] == id for m in db["media"]):
        db["queue"].append(id)
        save_db(db)
        return {"status": "Added to Queue"}
    raise HTTPException(status_code=404, detail="ID not found")

@app.post("/api/queue/reorder")
def reorder_queue(order: List[str] = Body(...)):
    db = load_db()
    db["queue"] = order
    save_db(db)
    return {"status": "Queue Updated"}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    ts = str(int(time.time()))
    filename = f"{ts}_{file.filename.replace(' ', '_')}"
    file_path = UPLOAD_DIR / filename
    with file_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    db = load_db()
    db["media"].append({"id": ts, "title": file.filename, "url": f"static/uploads/{filename}"})
    save_db(db)
    return {"status": "Uploaded", "id": ts}

@app.delete("/api/media/{id}")
def delete_media(id: str):
    db = load_db()
    db["media"] = [m for m in db["media"] if m["id"] != id]
    db["queue"] = [qid for qid in db["queue"] if qid != id]
    save_db(db)
    return {"status": "Deleted"}

@app.get("/health")
def health(): return {"status": "alive"}

if __name__ == "__main__":
    import uvicorn
    # Render uses 'PORT' env var automatically
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

import os, subprocess, signal, json, logging, threading, time, shutil, psutil
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Body
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from yt_dlp import YoutubeDL

# --- SETUP & LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("ASHLABS-TITAN")

BASE_DIR = Path(__file__).parent
LIVE_DIR = BASE_DIR / "static/live"
UPLOAD_DIR = BASE_DIR / "static/uploads"
DB_FILE = BASE_DIR / "db.json"

for d in [LIVE_DIR, UPLOAD_DIR]: 
    d.mkdir(parents=True, exist_ok=True)

# --- DATABASE HELPERS ---
def load_db():
    if not DB_FILE.exists(): return {"media": [], "queue": []}
    try:
        content = DB_FILE.read_text()
        return json.loads(content) if content else {"media": [], "queue": []}
    except: return {"media": [], "queue": []}

def save_db(data):
    DB_FILE.write_text(json.dumps(data, indent=2))

# --- YT-DLP RESOLVER ---
def resolve_url(url):
    if not any(x in url for x in ["youtube.com", "youtu.be", "twitch.tv"]): return url
    try:
        ydl_opts = {'format': 'best[ext=mp4]/best', 'quiet': True, 'noplaylist': True}
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)['url']
    except Exception as e:
        logger.error(f"YT-DLP Error: {e}")
        return None

# --- ENGINE CORE ---
class OverlordBroadcaster:
    def __init__(self):
        self.process = None
        self.current = "OFFLINE"
        self.is_auto = True
        self.lock = threading.Lock()

    def kill(self):
        with self.lock:
            if self.process:
                logger.info("Terminating FFmpeg...")
                self.process.terminate()
                try: self.process.wait(timeout=2)
                except: self.process.kill()
                self.process = None
            self.current = "OFFLINE"

    def start(self, source, title):
        self.kill()
        stream_url = resolve_url(source)
        if not stream_url: 
            logger.error(f"Could not resolve source: {source}")
            return
        
        # PRO SETTINGS: Watermark + High Compression for Render Free Tier
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
            self.current = title
            logger.info(f"Broadcast Started: {title}")

    def monitor(self):
        """Infinite loop to handle playlist transitions"""
        while True:
            if self.is_auto:
                db = load_db()
                # Start next if process is dead
                if db["queue"] and (not self.process or self.process.poll() is not None):
                    next_id = db["queue"].pop(0)
                    media = next((m for m in db["media"] if m["id"] == next_id), None)
                    if media:
                        db["queue"].append(next_id) # Add back to end for infinite loop
                        save_db(db)
                        self.start(media["url"], media["title"])
            time.sleep(3)

# --- FASTAPI APP ---
app = FastAPI(title="Ashlabs Overlord v3")

# 🚨 THE CORS FIX: This allows your HTML page to talk to the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allow all origins
    allow_credentials=True,
    allow_methods=["*"], # Allow GET, POST, DELETE, etc.
    allow_headers=["*"], # Allow all headers
)

engine = OverlordBroadcaster()
app.mount("/stream", StaticFiles(directory=LIVE_DIR), name="stream")

@app.on_event("startup")
def startup():
    threading.Thread(target=engine.monitor, daemon=True).start()

# --- API ENDPOINTS ---

@app.get("/api/status")
def get_status():
    return {
        "is_live": engine.process is not None and engine.process.poll() is None,
        "current": engine.current,
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent,
        "queue": load_db()["queue"]
    }

@app.get("/health")
def health(): return {"status": "online"}

@app.post("/api/control/stop")
def stop_all():
    engine.is_auto = False
    engine.kill()
    return {"status": "Broadcaster Stopped"}

@app.post("/api/control/start")
def start_auto():
    engine.is_auto = True
    return {"status": "Auto-Pilot Activated"}

@app.post("/api/control/skip")
def skip():
    engine.kill()
    return {"status": "Skipped to next track"}

@app.get("/api/media")
def list_media():
    return load_db()["media"]

@app.post("/api/media/remote")
def add_remote(url: str, title: str):
    db = load_db()
    id = str(int(time.time()))
    db["media"].append({"id": id, "title": f"🔗 {title}", "url": url})
    save_db(db)
    return {"id": id}

@app.post("/api/queue/add")
def add_to_queue(id: str):
    db = load_db()
    if any(m["id"] == id for m in db["media"]):
        db["queue"].append(id)
        save_db(db)
        return {"status": "Added"}
    raise HTTPException(404, "Media ID not found")

@app.post("/api/queue/reorder")
def reorder_queue(order: List[str] = Body(...)):
    db = load_db()
    db["queue"] = order
    save_db(db)
    return {"status": "Reordered"}

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ts = str(int(time.time()))
    safe_filename = f"{ts}_{file.filename.replace(' ', '_')}"
    path = UPLOAD_DIR / safe_filename
    with path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    db = load_db()
    db["media"].append({"id": ts, "title": file.filename, "url": f"static/uploads/{safe_filename}"})
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

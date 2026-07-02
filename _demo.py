"""Render a scripted ~21s demo of the camera-follow: ambient -> emergency
appears -> zoom in & follow -> resolves -> zoom back out. Visual only."""
import json
import random
import subprocess
import threading
import time
from pathlib import Path

from contrail.renderer.render import capture_loop

REGION = [-7.1, 47.3, 6.2, 55.6]            # full UK/Europe region
STATE_PATH = "/tmp/demo_state.json"
FRAMES_DIR = "/tmp/demo_frames"
OUT = "/Users/kc/proj/yt_live/camera_demo.mp4"

random.seed(7)
# Static-ish background traffic scattered across the region.
BG = [{"lat": round(random.uniform(48, 55), 3), "lon": round(random.uniform(-6, 5), 3),
       "track": round(random.uniform(0, 360), 1), "emergency": False, "focus": False}
      for _ in range(80)]


def cam_box(lat, lon, span=3.0):
    return [lon - span, lat - span, lon + span, lat + span]


def write(state):
    Path(STATE_PATH).write_text(json.dumps(state))


def ambient(focus_idx, caption):
    ac = [dict(a) for a in BG]
    ac[focus_idx]["focus"] = True
    return {"viewers": 1820, "airborne": 642, "busiest": "LHR · 138/hr",
            "segment": "ambient", "caption": caption, "tracking": None,
            "alerts": ["QFA7 over the Pacific", "A380 climbing out of Dubai"],
            "bounds": REGION, "camera": REGION, "aircraft": ac}


def emergency(lat, lon, alt, caption, phase="event"):
    ac = [dict(a) for a in BG]
    baw = {"lat": lat, "lon": lon, "track": 250.0, "emergency": True,
           "focus": True, "callsign": "BAW286"}
    ac.append(baw)
    return {"viewers": 1820, "airborne": 642, "busiest": "LHR · 138/hr",
            "segment": "event", "caption": caption,
            "tracking": {"callsign": "BAW286", "type": "Boeing 777-300ER",
                         "route": "San Francisco → London", "alt": alt, "speed": 470,
                         "squawk": "7700", "emergency": True, "lat": lat, "lon": lon},
            "alerts": ["QFA7 over the Pacific", "A380 climbing out of Dubai"],
            "bounds": REGION, "camera": cam_box(lat, lon), "aircraft": ac}


# (seconds_from_start, state) — camera changes drive the zoom transitions.
TIMELINE = [
    (0.0,  ambient(12, "A steady flow of traffic across the region this hour.")),
    (4.0,  ambient(40, "An easyJet A320 cruising over the Channel toward Gatwick.")),
    (8.0,  emergency(53.0, -1.2, 33000,
                     "Breaking: a British Airways 777 has declared an emergency over the Midlands.")),
    (12.0, emergency(52.4, -1.9, 24000,
                     "BAW286 is descending through twenty-four thousand feet, turning toward London.")),
    (16.0, emergency(51.5, -0.6, 9000,
                     "Now through nine thousand feet on what looks like an approach into Heathrow.")),
    (19.0, ambient(25, "BAW286 is down safely. Back to the wider picture across the region.")),
]


def writer():
    start = time.monotonic()
    for t, state in TIMELINE:
        while time.monotonic() - start < t:
            time.sleep(0.05)
        write(state)


write(TIMELINE[0][1])
cap = threading.Thread(target=capture_loop,
                       kwargs={"state_path": STATE_PATH, "frames_dir": FRAMES_DIR,
                               "fps": 12, "duration_s": 22.0})
cap.start()
writer()
cap.join()

subprocess.run(["ffmpeg", "-y", "-framerate", "12", "-i", f"{FRAMES_DIR}/frame_%06d.png",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", OUT], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(f"wrote {OUT}")

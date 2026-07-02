import random
import math
from typing import Any, Callable

def empty_sky(duration_s: float, fps: float) -> list[tuple[float, dict[str, Any]]]:
    random.seed(42)
    num_frames = int(duration_s * fps)
    interval_ms = 1000.0 / fps
    states = []
    for i in range(num_frames):
        t_ms = i * interval_ms
        state = {
            "generated": t_ms / 1000.0,
            "viewers": 1000,
            "airborne": 0,
            "busiest": "",
            "segment": "ambient",
            "caption": "",
            "bounds": [-8.0, 47.0, 6.0, 57.0],
            "camera": [-4.5, 49.5, 1.5, 55.5],
            "tracking": None,
            "alerts": [],
            "aircraft": []
        }
        states.append((t_ms, state))
    return states

def _generate_generic_50(duration_s: float, fps: float, camera_shift: bool = False, narration: bool = False) -> list[tuple[float, dict[str, Any]]]:
    random.seed(42)
    num_frames = int(duration_s * fps)
    interval_ms = 1000.0 / fps
    states = []
    
    captions = [
        "A British Airways Boeing 777 has squawked 7700 over the Midlands and is turning back toward London.",
        "Air traffic control has cleared the emergency flight for priority landing at Heathrow Airport.",
        "Ground crews and emergency services are standby at the runway.",
        "The pilot reports a cabin pressurization issue and is descending rapidly.",
        "Skywatch is monitoring the flight as it approaches London airspace."
    ]
    
    for i in range(num_frames):
        t_ms = i * interval_ms
        aircraft = []
        
        # Determine bounds and camera coordinates
        if camera_shift:
            # Shift translation & change zoom scale to defeat cache
            shift = (t_ms / 1000.0) * 0.5
            scale_factor = 1.0 + 0.05 * math.sin(t_ms / 500.0)
            bounds = [-8.0 + shift, 47.0 + shift, 6.0 + shift, 57.0 + shift]
            camera = [
                -4.5 * scale_factor + shift,
                49.5 * scale_factor + shift,
                1.5 * scale_factor + shift,
                55.5 * scale_factor + shift
            ]
            tracked_lat = 52.5 + shift
            tracked_lon = -1.5 + shift
        else:
            bounds = [-8.0, 47.0, 6.0, 57.0]
            camera = [-4.5, 49.5, 1.5, 55.5]
            tracked_lat = 52.5
            tracked_lon = -1.5

        # Generate 49 normal aircraft
        for _ in range(49):
            lat = random.uniform(bounds[1], bounds[3])
            lon = random.uniform(bounds[0], bounds[2])
            aircraft.append({
                "lat": round(lat, 3),
                "lon": round(lon, 3),
                "track": round(random.uniform(0, 360), 1),
                "emergency": False,
                "focus": False,
                "gs": 400.0,
            })
            
        # Add 1 tracked emergency aircraft
        tracked_aircraft = {
            "lat": tracked_lat,
            "lon": tracked_lon,
            "track": 95.0,
            "emergency": True,
            "focus": True,
            "callsign": "BAW286",
            "gs": 488.0,
        }
        aircraft.append(tracked_aircraft)
        
        caption_str = captions[i % len(captions)] if narration else ""
        
        state = {
            "generated": t_ms / 1000.0,
            "viewers": 1963,
            "airborne": 11482,
            "busiest": "LHR · 142/hr",
            "segment": "event",
            "caption": caption_str,
            "bounds": bounds,
            "camera": camera,
            "tracking": {
                "callsign": "BAW286",
                "type": "Boeing 777-300ER",
                "route": "San Francisco -> London",
                "alt": 37000,
                "speed": 488,
                "squawk": "7700",
                "emergency": True,
                "lat": tracked_lat,
                "lon": tracked_lon,
            },
            "alerts": [
                "QFA7 ultra-long-haul over the Pacific",
                "loss-of-signal near Reykjavik",
            ],
            "aircraft": aircraft
        }
        states.append((t_ms, state))
    return states

def moderate_50(duration_s: float, fps: float) -> list[tuple[float, dict[str, Any]]]:
    return _generate_generic_50(duration_s, fps, camera_shift=False, narration=False)

def camera_churn(duration_s: float, fps: float) -> list[tuple[float, dict[str, Any]]]:
    return _generate_generic_50(duration_s, fps, camera_shift=True, narration=False)

def narration_heavy(duration_s: float, fps: float) -> list[tuple[float, dict[str, Any]]]:
    return _generate_generic_50(duration_s, fps, camera_shift=False, narration=True)

def peak_200(duration_s: float, fps: float) -> list[tuple[float, dict[str, Any]]]:
    random.seed(42)
    num_frames = int(duration_s * fps)
    interval_ms = 1000.0 / fps
    states = []
    
    for i in range(num_frames):
        t_ms = i * interval_ms
        aircraft = []
        bounds = [-8.0, 47.0, 6.0, 57.0]
        camera = [-4.5, 49.5, 1.5, 55.5]
        
        # Generate 199 normal aircraft
        for _ in range(199):
            lat = random.uniform(bounds[1], bounds[3])
            lon = random.uniform(bounds[0], bounds[2])
            aircraft.append({
                "lat": round(lat, 3),
                "lon": round(lon, 3),
                "track": round(random.uniform(0, 360), 1),
                "emergency": False,
                "focus": False,
                "gs": 400.0,
            })
            
        # Add 1 tracked emergency aircraft
        tracked_aircraft = {
            "lat": 52.5,
            "lon": -1.5,
            "track": 95.0,
            "emergency": True,
            "focus": True,
            "callsign": "BAW286",
            "gs": 488.0,
        }
        aircraft.append(tracked_aircraft)
        
        state = {
            "generated": t_ms / 1000.0,
            "viewers": 2500,
            "airborne": 15000,
            "busiest": "LHR · 200/hr",
            "segment": "event",
            "caption": "",
            "bounds": bounds,
            "camera": camera,
            "tracking": {
                "callsign": "BAW286",
                "type": "Boeing 777-300ER",
                "route": "San Francisco -> London",
                "alt": 37000,
                "speed": 488,
                "squawk": "7700",
                "emergency": True,
                "lat": 52.5,
                "lon": -1.5,
            },
            "alerts": [
                "Alert 1",
                "Alert 2",
            ],
            "aircraft": aircraft
        }
        states.append((t_ms, state))
    return states

SCENARIOS: dict[str, Callable[[float, float], list[tuple[float, dict[str, Any]]]]] = {
    "empty_sky": empty_sky,
    "moderate_50": moderate_50,
    "peak_200": peak_200,
    "camera_churn": camera_churn,
    "narration_heavy": narration_heavy,
}

def list_scenarios() -> list[str]:
    return list(SCENARIOS.keys())

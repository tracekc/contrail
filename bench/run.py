import os
import sys
import json
import time
import argparse
import platform
import tempfile
import shutil
import threading
import subprocess
import skia
from typing import Any, Optional

# Add project root to sys.path so we can import contrail
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from bench.scenarios import SCENARIOS, list_scenarios
from bench.metrics import Result, percentile, print_result_table
from contrail.renderer.native import NativeRenderer
from contrail.stream import LiveStreamer, _make_fifo

# Ensure RENDERER=native is set in environment for imports and tests
os.environ["RENDERER"] = "native"

def read_stderr(proc, speeds):
    """Background thread worker to read ffmpeg stderr and extract speed ratio."""
    import re
    speed_pat = re.compile(r'speed=\s*(\d+\.?\d*)x')
    try:
        buffer = ""
        while True:
            chunk = proc.stderr.read(1024)
            if not chunk:
                break
            buffer += chunk.decode('utf-8', errors='ignore')
            matches = speed_pat.findall(buffer)
            if matches:
                for m in matches:
                    speeds.append(float(m))
            if len(buffer) > 2048:
                buffer = buffer[-200:]
    except Exception:
        pass

def feed_silence(fifo_path: str, stop_event: threading.Event):
    """Background thread worker to write raw audio silence into the ffmpeg audio FIFO."""
    sample_rate = 44100
    channels = 1
    bytes_per_sec = sample_rate * channels * 2
    chunk_s = 0.1
    silence_chunk = b"\x00" * int(bytes_per_sec * chunk_s)
    
    try:
        with open(fifo_path, "wb") as f:
            deadline = time.monotonic()
            while not stop_event.is_set():
                f.write(silence_chunk)
                f.flush()
                deadline += chunk_s
                sleep_time = deadline - time.monotonic()
                if sleep_time > 0:
                    time.sleep(sleep_time)
    except Exception:
        pass

def synth_worker(scenario_states: list, stop_event: threading.Event, synth_windows: list, loop_start: float, fps: float):
    """Background thread worker for contention tier to run Narrator TTS synthesis."""
    from contrail.narrate import Narrator
    try:
        narrator = Narrator()
    except Exception as e:
        print(f"Error initializing Narrator in background thread: {e}")
        return

    while not stop_event.is_set():
        # Sleep for ~8 seconds, check stop_event periodically to exit fast
        for _ in range(80):
            if stop_event.is_set():
                break
            time.sleep(0.1)
        if stop_event.is_set():
            break

        elapsed_s = time.monotonic() - loop_start
        state_idx = min(int(elapsed_s * fps), len(scenario_states) - 1)
        _, state = scenario_states[state_idx]
        caption = state.get("caption") or "A British Airways 777 has squawked 7700 over the Midlands."

        fd, tmpfile = tempfile.mkstemp(suffix=".wav", prefix="contrail_bench_")
        os.close(fd)

        start_time = time.monotonic()
        try:
            narrator.synth(caption, tmpfile)
        except Exception:
            pass
        end_time = time.monotonic()

        synth_windows.append((start_time, end_time))

        if os.path.exists(tmpfile):
            try:
                os.unlink(tmpfile)
            except Exception:
                pass

def run_micro(scenario: str, fps: float) -> dict[str, Any]:
    """Execute micro benchmark tier."""
    # Generate scenario states
    scenario_states = SCENARIOS[scenario](duration_s=300/fps, fps=fps)
    first_state = scenario_states[0][1]

    # Time render_frame over N=300 iterations (main test)
    renderer = NativeRenderer()
    renderer.apply_state(first_state, now=0.0)
    
    times = []
    for i in range(300):
        t_ms = i * (1000.0 / fps)
        start = time.perf_counter()
        renderer.render_frame(now_ms=t_ms, quality=92)
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000.0)

    # Discard warmup
    warmup_count = int(2.0 * fps)
    timed_times = times[warmup_count:]
    if not timed_times:
        timed_times = times
    
    p50 = percentile(timed_times, 50)
    p99 = percentile(timed_times, 99)

    # Cache-hit cost (static camera)
    hit_renderer = NativeRenderer()
    hit_renderer.apply_state(first_state, now=0.0)
    for i in range(50):
        hit_renderer.render_frame(now_ms=i * (1000.0 / fps), quality=92)

    hit_times = []
    for i in range(300):
        t_ms = 2000.0 + i * (1000.0 / fps)
        start = time.perf_counter()
        hit_renderer.render_frame(now_ms=t_ms, quality=92)
        elapsed = time.perf_counter() - start
        hit_times.append(elapsed * 1000.0)
    
    cache_hit_p50 = percentile(hit_times, 50)
    cache_hit_p99 = percentile(hit_times, 99)

    # Cache-bust cost (camera_churn scenario)
    bust_renderer = NativeRenderer()
    churn_states = SCENARIOS["camera_churn"](duration_s=300/fps, fps=fps)
    
    bust_times = []
    for i in range(300):
        t_ms, churn_state = churn_states[i]
        bust_renderer.apply_state(churn_state, now=t_ms)
        start = time.perf_counter()
        bust_renderer.render_frame(now_ms=t_ms, quality=92)
        elapsed = time.perf_counter() - start
        bust_times.append(elapsed * 1000.0)

    cache_bust_p50 = percentile(bust_times, 50)
    cache_bust_p99 = percentile(bust_times, 99)

    # JPEG encode cost & mean frame size (at quality=92)
    img = renderer.surface.makeImageSnapshot()
    encode_times = []
    sizes = []
    for _ in range(300):
        start = time.perf_counter()
        data = img.encodeToData(skia.kJPEG, 92)
        elapsed = time.perf_counter() - start
        encode_times.append(elapsed * 1000.0)
        sizes.append(len(bytes(data)))
    
    jpeg_encode_p50 = percentile(encode_times, 50)
    jpeg_encode_p99 = percentile(encode_times, 99)
    mean_frame_size = sum(sizes) / len(sizes)

    return {
        "p50_ms": p50,
        "p99_ms": p99,
        "cache_hit_p50_ms": cache_hit_p50,
        "cache_hit_p99_ms": cache_hit_p99,
        "cache_bust_p50_ms": cache_bust_p50,
        "cache_bust_p99_ms": cache_bust_p99,
        "jpeg_encode_p50_ms": jpeg_encode_p50,
        "jpeg_encode_p99_ms": jpeg_encode_p99,
        "mean_frame_size_bytes": mean_frame_size
    }

def run_pipeline_or_contention(scenario: str, duration_s: float, fps: float, is_contention: bool) -> dict[str, Any]:
    """Execute pipeline or contention benchmark tier."""
    scenario_states = SCENARIOS[scenario](duration_s, fps)

    # Create FIFO
    fifo_path = _make_fifo()

    # Create temp output mp4
    temp_mp4 = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    temp_mp4_path = temp_mp4.name
    temp_mp4.close()

    # Start audio thread
    stop_audio = threading.Event()
    audio_thread = threading.Thread(target=feed_silence, args=(fifo_path, stop_audio), daemon=True)
    audio_thread.start()

    # Build and launch ffmpeg
    streamer = LiveStreamer(target="test", fps=fps, test_out=temp_mp4_path, test_duration_s=duration_s)
    cmd = streamer._ffmpeg_cmd(fifo_path)

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    speeds = []
    stderr_thread = threading.Thread(target=read_stderr, args=(proc, speeds), daemon=True)
    stderr_thread.start()

    renderer = NativeRenderer()

    # Setup synth worker for contention
    synth_windows = []
    stop_synth = threading.Event()
    synth_thread = None

    loop_start = time.monotonic()

    if is_contention:
        synth_thread = threading.Thread(
            target=synth_worker,
            args=(scenario_states, stop_synth, synth_windows, loop_start, fps),
            daemon=True
        )
        synth_thread.start()

    # Deadline-paced loop
    write_times = []
    interval = 1.0 / fps
    deadline = loop_start

    try:
        for frame_idx in range(len(scenario_states)):
            now = time.monotonic()
            sleep_time = deadline - now
            if sleep_time > 0:
                time.sleep(sleep_time)

            t_ms = (time.monotonic() - loop_start) * 1000.0
            
            _, state = scenario_states[frame_idx]
            renderer.apply_state(state, now=t_ms)
            frame_bytes = renderer.render_frame(now_ms=t_ms, quality=92)

            proc.stdin.write(frame_bytes)
            proc.stdin.flush()
            
            write_times.append(time.monotonic())
            deadline += interval
    except (BrokenPipeError, ValueError):
        pass
    finally:
        # Shutdown synth thread
        if is_contention and synth_thread:
            stop_synth.set()
            synth_thread.join(timeout=1.0)

        # Close ffmpeg stdin and wait for finish
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        proc.wait()

        # Stop audio FIFO feed
        stop_audio.set()
        audio_thread.join(timeout=1.0)

        # Cleanup files
        if os.path.exists(temp_mp4_path):
            try:
                os.unlink(temp_mp4_path)
            except Exception:
                pass
        if os.path.exists(fifo_path):
            try:
                os.unlink(fifo_path)
            except Exception:
                pass
        fifo_dir = os.path.dirname(fifo_path)
        if os.path.exists(fifo_dir):
            try:
                shutil.rmtree(fifo_dir, ignore_errors=True)
            except Exception:
                pass

    # Compute metrics
    intervals = []
    for i in range(1, len(write_times)):
        intervals.append((write_times[i] - write_times[i - 1]) * 1000.0)

    # Discard first ~2s of frames as warmup
    warmup_count = int(2.0 * fps)
    timed_intervals = []
    timed_write_times = []

    for i in range(warmup_count, len(intervals)):
        timed_intervals.append(intervals[i])
        timed_write_times.append((write_times[i], write_times[i + 1]))

    if not timed_intervals:
        timed_intervals = intervals
        timed_write_times = [(write_times[i], write_times[i + 1]) for i in range(len(intervals))]

    p50 = percentile(timed_intervals, 50)
    p95 = percentile(timed_intervals, 95)
    p99 = percentile(timed_intervals, 99)
    val_max = max(timed_intervals) if timed_intervals else 0.0

    nominal_ms = (1.0 / fps) * 1000.0
    stalls = sum(1 for x in timed_intervals if x > 1.5 * nominal_ms)

    metrics = {
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "max_ms": val_max,
        "stalls": stalls
    }

    if is_contention:
        synth_intervals = []
        for interval_val, (t_prev, t_curr) in zip(timed_intervals, timed_write_times):
            in_synth = False
            for s_start, s_end in synth_windows:
                if t_curr >= s_start and t_prev <= s_end:
                    in_synth = True
                    break
            if in_synth:
                synth_intervals.append(interval_val)

        metrics["p99_overall_ms"] = p99
        metrics["p99_during_synth_ms"] = percentile(synth_intervals, 99) if synth_intervals else 0.0
        metrics["synth_count"] = len(synth_windows)
    else:
        avg_speed = sum(speeds) / len(speeds) if speeds else 0.0
        metrics["ffmpeg_speed"] = avg_speed

    return metrics

def main():
    parser = argparse.ArgumentParser(description="Contrail Benchmark Suite CLI")
    parser.add_argument("--tier", choices=["micro", "pipeline", "contention", "all"], default="all",
                        help="Benchmark tier to execute")
    parser.add_argument("--scenario", default="all",
                        help="Scenario to run (empty_sky|moderate_50|peak_200|camera_churn|narration_heavy|all)")
    parser.add_argument("--duration", type=float, default=60.0,
                        help="Duration in seconds (default 60 for pipeline/contention)")

    args = parser.parse_args()

    # Read STREAM_FPS
    fps_env = os.getenv("STREAM_FPS")
    fps = int(fps_env) if fps_env and fps_env.isdigit() else 12

    # Parse inputs
    if args.tier == "all":
        tiers = ["micro", "pipeline", "contention"]
    else:
        tiers = [args.tier]

    if args.scenario == "all":
        scenarios = list_scenarios()
    else:
        if args.scenario not in SCENARIOS:
            print(f"Unknown scenario name: {args.scenario}")
            print(f"Available scenarios: {list_scenarios()}")
            sys.exit(1)
        scenarios = [args.scenario]

    # Load baseline
    baseline_data = {}
    baseline_file = "bench/baseline.json"
    if os.path.exists(baseline_file):
        try:
            with open(baseline_file, "r") as f:
                baseline_data = json.load(f)
        except Exception:
            pass

    os.makedirs("bench/results", exist_ok=True)

    for tier in tiers:
        for scenario in scenarios:
            print(f"Running benchmark tier={tier} scenario={scenario} ...")
            
            if tier == "micro":
                # Micro tier doesn't use the duration flag in the same way, N is fixed at 300
                metrics = run_micro(scenario, fps)
            elif tier == "pipeline":
                metrics = run_pipeline_or_contention(scenario, args.duration, fps, is_contention=False)
            elif tier == "contention":
                metrics = run_pipeline_or_contention(scenario, args.duration, fps, is_contention=True)
            
            # Create result object
            result = Result.create(tier=tier, scenario=scenario, duration=args.duration if tier != "micro" else (300/fps), metrics=metrics)
            
            # Look up baseline
            baseline_result = None
            key = f"{result.host}:{tier}:{scenario}"
            if key in baseline_data:
                try:
                    baseline_result = Result.from_dict(baseline_data[key])
                except Exception:
                    pass

            # Print Table
            print_result_table(result, baseline_result)

            # Write output file
            base_timestamp = time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())
            filename = f"{result.host}-{base_timestamp}.json"
            filepath = os.path.join("bench/results", filename)
            counter = 1
            while os.path.exists(filepath):
                filename = f"{result.host}-{base_timestamp}_{counter}.json"
                filepath = os.path.join("bench/results", filename)
                counter += 1
            
            with open(filepath, "w") as f:
                json.dump(result.to_dict(), f, indent=2)
            print(f"Saved result to {filepath}")

if __name__ == "__main__":
    main()

# Contrail Benchmark Suite

This benchmark suite measures the performance, throughput, and stability of the Contrail streaming pipeline under various loads and contention levels.

## How-to Run

Execute the benchmark suite using the virtual environment's Python runner from the project root:

```bash
# Run all micro benchmarks
.venv/bin/python -m bench.run --tier micro --scenario all

# Run pipeline benchmark for a moderate load for 15 seconds
.venv/bin/python -m bench.run --tier pipeline --scenario moderate_50 --duration 15
```

## Metrics Explanation

- **p50 / p95 / p99 / max (ms)**: Percentiles and maximum wall-clock intervals or rendering times in milliseconds. Lower numbers indicate faster processing and greater frame delivery stability.
- **cache_hit_p50 / cache_hit_p99 (ms)**: Renderer execution cost when the camera is static, utilizing the pre-projected base-map cache.
- **cache_bust_p50 / cache_bust_p99 (ms)**: Renderer execution cost under camera movement/churn, forcing a full map re-projection on every frame.
- **jpeg_encode_p50 / jpeg_encode_p99 (ms)**: CPU cost strictly spent compressing the rendered frame canvas into JPEG bytes.
- **mean_frame_size_bytes**: The average size of the generated JPEG frames at quality=92.
- **stalls**: The count of intervals between successive frame writes to the ffmpeg pipe that exceeded 1.5x the nominal interval (e.g. >125ms at 12 FPS), indicating video encoding stutter.
- **ffmpeg_speed**: The average encoding speed ratio reported by ffmpeg. Greater than 1.0x indicates encoding faster than real-time.
- **p99_overall_ms**: Overall frame-interval p99 in the presence of resource contention.
- **p99_during_synth_ms**: Frame-interval p99 restricted only to the windows of active text-to-speech synthesis (e.g., Piper or macOS say).
- **synth_count**: The number of TTS synthesis runs executed during the benchmark.

**IMPORTANT NOTE: All benchmark metrics are host-relative. Results are only comparable when run on the same physical machine.**

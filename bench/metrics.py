import os
import json
import platform
import time
import math
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

def percentile(data: list[float], pct: float) -> float:
    """Calculate percentile in a list of numbers using nearest-rank or linear interpolation."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    idx = (n - 1) * (pct / 100.0)
    floor_idx = int(math.floor(idx))
    ceil_idx = int(math.ceil(idx))
    if floor_idx == ceil_idx:
        return float(sorted_data[floor_idx])
    d0 = sorted_data[floor_idx]
    d1 = sorted_data[ceil_idx]
    return float(d0 + (d1 - d0) * (idx - floor_idx))

@dataclass
class Result:
    host: str
    processor: str
    cpu_count: int
    timestamp: str
    tier: str
    scenario: str
    duration: float
    metrics: dict[str, Any]

    @classmethod
    def create(cls, tier: str, scenario: str, duration: float, metrics: dict[str, Any]) -> "Result":
        return cls(
            host=platform.node(),
            processor=platform.processor() or "Unknown",
            cpu_count=os.cpu_count() or 0,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            tier=tier,
            scenario=scenario,
            duration=duration,
            metrics=metrics
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Result":
        return cls(
            host=d["host"],
            processor=d["processor"],
            cpu_count=d["cpu_count"],
            timestamp=d["timestamp"],
            tier=d["tier"],
            scenario=d["scenario"],
            duration=d["duration"],
            metrics=d["metrics"]
        )

def print_result_table(result: Result, baseline: Optional[Result] = None) -> None:
    """Prints a pretty console table of the benchmark results with optional baseline comparison."""
    print("\n" + "=" * 70)
    print(f"BENCHMARK RESULT: {result.tier.upper()} ({result.scenario})")
    print("-" * 70)
    print(f"Host:      {result.host} ({result.cpu_count} CPUs, {result.processor})")
    print(f"Time:      {result.timestamp}")
    print(f"Duration:  {result.duration}s")
    print("=" * 70)
    
    if baseline:
        header = f"{'Metric':<30} | {'Current':>12} | {'Baseline':>12} | {'Delta (%)':>10}"
    else:
        header = f"{'Metric':<30} | {'Value':>12}"
    
    print(header)
    print("-" * len(header))
    
    for metric_name, current_val in result.metrics.items():
        if isinstance(current_val, float):
            curr_str = f"{current_val:.2f}"
        elif isinstance(current_val, int):
            curr_str = f"{current_val}"
        else:
            curr_str = str(current_val)
            
        if baseline and metric_name in baseline.metrics:
            base_val = baseline.metrics[metric_name]
            if isinstance(base_val, float):
                base_str = f"{base_val:.2f}"
            elif isinstance(base_val, int):
                base_str = f"{base_val}"
            else:
                base_str = str(base_val)
                
            if isinstance(current_val, (int, float)) and isinstance(base_val, (int, float)) and base_val != 0:
                delta = ((current_val - base_val) / base_val) * 100.0
                delta_str = f"{delta:+.1f}%"
            else:
                delta_str = "N/A"
            print(f"{metric_name:<30} | {curr_str:>12} | {base_str:>12} | {delta_str:>10}")
        else:
            print(f"{metric_name:<30} | {curr_str:>12}")
    print("=" * len(header) + "\n")

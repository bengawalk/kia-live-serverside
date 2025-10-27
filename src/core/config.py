from dataclasses import dataclass, field
from pathlib import Path
import os


def detect_available_memory() -> int:
    try:
        import psutil  # local import to keep module import light
        total_bytes = psutil.virtual_memory().total
        return max(100, min(4096, int(total_bytes / (1024 * 1024 * 4))))
    except Exception:
        return 512


def detect_cpu_cores() -> int:
    try:
        return max(1, int(os.cpu_count() or 1))
    except Exception:
        return 1


@dataclass
class ApplicationConfig:
    """Centralized configuration"""

    # Memory settings
    max_memory_mb: int = field(default_factory=lambda: detect_available_memory())

    # API settings
    bmtc_api_url: str = "https://bmtcmobileapi.karnataka.gov.in/WebAPI"
    request_timeout_seconds: int = 60
    rate_limit_delay_seconds: float = 5.0  # 20 seconds to prevent API overload
    
    # RT Feed settings
    rt_feed_update_interval_seconds: int = 5  # Frequent GTFS-RT updates (every 5 seconds)
    rt_feed_cache_ttl_seconds: int = 3  # Short cache for responsive updates

    # Processing settings
    max_concurrent_requests: int = field(default_factory=lambda: detect_cpu_cores() * 2)
    pipeline_buffer_size: int = 100
    # print('PATHS TTTT', os.listdir('./'))
    # Paths (maintain current structure)
    in_dir: Path = Path("./in")
    out_dir: Path = Path("./out")
    db_path: Path = Path("./db/database.db")

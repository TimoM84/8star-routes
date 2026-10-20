"""Configuration from environment variables (and an optional local .env file).

Secrets are only read here and never logged. `Config.describe()` returns a
sanitized view that is safe to show in the admin interface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo

SECRET_FIELDS = {"tomtom_api_key", "tomtom_map_key", "app_password", "session_secret", "api_read_token", "embed_token"}
ALLOWED_INTERVALS = (5, 10, 15, 20, 30, 60)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment wins."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return int(v)


def _str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None else v.strip()


@dataclass
class Config:
    app_mode: str = "live"  # live | demo
    data_dir: Path = Path("./data")
    port: int = 8080
    base_path: str = ""  # e.g. "/routes" when served under a sub path
    public_base_url: str = ""
    timezone: str = "Europe/Amsterdam"

    # Secrets
    tomtom_api_key: str = ""
    tomtom_map_key: str = ""  # optional separate, restricted key for direct browser tiles
    app_password: str = ""
    session_secret: str = ""
    api_read_token: str = ""
    embed_token: str = ""

    # Auth / HTTP
    auth_mode: str = "password"  # password | none
    cookie_secure: str = "auto"  # auto | true | false
    trusted_proxies: str = ""
    frame_ancestors: str = "'self'"

    # Collection
    collection_enabled: bool = True
    collection_interval_minutes: int = 15
    collection_late_tolerance_seconds: int = 120
    comparison_tolerance_seconds: int = 120
    stale_after_minutes: int = 40
    unrestricted_max_alternatives: int = 2
    request_timeout_seconds: int = 20
    max_retries: int = 1
    retry_delay_seconds: int = 8
    max_retry_after_seconds: int = 90
    store_raw_responses: bool = False

    # Budget
    allowance_period: str = "monthly"  # monthly | daily
    allowance_reset_timezone: str = "UTC"
    allowance_reset_grace_minutes: int = 60
    allowance_reset_confirmed: bool = False
    routing_monthly_allowance: int = 20000
    routing_safety_reserve: int = 2000
    routing_other_apps_allowance: int = 0
    routing_opening_usage: int = 0
    routing_hard_ceiling: int = 0  # 0 = allowance - reserve - other apps
    routing_warning_percent: int = 80
    map_tile_allowance: int = 200000
    traffic_tile_allowance: int = 200000
    tile_safety_reserve: int = 10000

    # Map
    map_tile_mode: str = "proxy"  # proxy | direct | off
    map_tile_url: str = "https://api.tomtom.com/map/1/tile/basic/main/{z}/{x}/{y}.png?tileSize=256&view=Unified"
    traffic_tile_url: str = "https://api.tomtom.com/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png?tileSize=256"
    traffic_refresh_minutes: int = 5

    # Storage
    observation_retention_days: int = 0  # 0 = keep forever
    backup_keep: int = 14
    daily_backup: bool = True

    extra: dict = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def is_demo(self) -> bool:
        return self.app_mode == "demo"

    @property
    def db_path(self) -> Path:
        return self.data_dir / ("demo.sqlite" if self.is_demo else "routes.sqlite")

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    def describe(self) -> dict:
        out = {}
        for f in fields(self):
            if f.name == "extra":
                continue
            v = getattr(self, f.name)
            if f.name in SECRET_FIELDS:
                out[f.name] = "set" if v else "not set"
            else:
                out[f.name] = str(v) if isinstance(v, Path) else v
        return out

    def validate(self) -> list[str]:
        problems = []
        if self.app_mode not in ("live", "demo"):
            problems.append("APP_MODE must be 'live' or 'demo'")
        if self.collection_interval_minutes not in ALLOWED_INTERVALS:
            problems.append(f"COLLECTION_INTERVAL_MINUTES must be one of {ALLOWED_INTERVALS}")
        if self.traffic_refresh_minutes < 5:
            problems.append("TRAFFIC_REFRESH_MINUTES must be at least 5")
        if self.auth_mode not in ("password", "none"):
            problems.append("AUTH_MODE must be 'password' or 'none'")
        if self.auth_mode == "password" and not self.app_password:
            problems.append("APP_PASSWORD is required when AUTH_MODE=password")
        if self.allowance_period not in ("monthly", "daily"):
            problems.append("ALLOWANCE_PERIOD must be 'monthly' or 'daily'")
        if self.map_tile_mode not in ("proxy", "direct", "off"):
            problems.append("MAP_TILE_MODE must be 'proxy', 'direct' or 'off'")
        for name in ("map_tile_url", "traffic_tile_url"):
            if not getattr(self, name).startswith("https://api.tomtom.com/"):
                problems.append(f"{name.upper()} must start with https://api.tomtom.com/")
        if self.base_path and (not self.base_path.startswith("/") or self.base_path.endswith("/")):
            problems.append("BASE_PATH must start with '/' and not end with '/' (or be empty)")
        try:
            ZoneInfo(self.timezone)
            ZoneInfo(self.allowance_reset_timezone)
        except Exception:
            problems.append("TIMEZONE / ALLOWANCE_RESET_TIMEZONE must be valid IANA zones")
        return problems


def load_config(env_file: str | None = None) -> Config:
    _load_dotenv(Path(env_file or os.environ.get("ENV_FILE", ".env")))
    c = Config(
        app_mode=_str("APP_MODE", "live").lower(),
        data_dir=Path(_str("DATA_DIR", "./data")),
        port=_int("PORT", 8080),
        base_path=_str("BASE_PATH", ""),
        public_base_url=_str("PUBLIC_BASE_URL", ""),
        timezone=_str("TIMEZONE", "Europe/Amsterdam"),
        tomtom_api_key=_str("TOMTOM_API_KEY"),
        tomtom_map_key=_str("TOMTOM_MAP_KEY"),
        app_password=_str("APP_PASSWORD"),
        session_secret=_str("SESSION_SECRET"),
        api_read_token=_str("API_READ_TOKEN"),
        embed_token=_str("EMBED_TOKEN"),
        auth_mode=_str("AUTH_MODE", "password").lower(),
        cookie_secure=_str("COOKIE_SECURE", "auto").lower(),
        trusted_proxies=_str("TRUSTED_PROXIES"),
        frame_ancestors=_str("FRAME_ANCESTORS", "'self'"),
        collection_enabled=_bool("COLLECTION_ENABLED", True),
        collection_interval_minutes=_int("COLLECTION_INTERVAL_MINUTES", 15),
        collection_late_tolerance_seconds=_int("COLLECTION_LATE_TOLERANCE_SECONDS", 120),
        comparison_tolerance_seconds=_int("COMPARISON_TOLERANCE_SECONDS", 120),
        stale_after_minutes=_int("STALE_AFTER_MINUTES", 40),
        unrestricted_max_alternatives=_int("UNRESTRICTED_MAX_ALTERNATIVES", 2),
        request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 20),
        max_retries=_int("MAX_RETRIES", 1),
        retry_delay_seconds=_int("RETRY_DELAY_SECONDS", 8),
        max_retry_after_seconds=_int("MAX_RETRY_AFTER_SECONDS", 90),
        store_raw_responses=_bool("STORE_RAW_RESPONSES", False),
        allowance_period=_str("ALLOWANCE_PERIOD", "monthly").lower(),
        allowance_reset_timezone=_str("ALLOWANCE_RESET_TIMEZONE", "UTC"),
        allowance_reset_grace_minutes=_int("ALLOWANCE_RESET_GRACE_MINUTES", 60),
        allowance_reset_confirmed=_bool("ALLOWANCE_RESET_CONFIRMED", False),
        routing_monthly_allowance=_int("ROUTING_MONTHLY_ALLOWANCE", 20000),
        routing_safety_reserve=_int("ROUTING_SAFETY_RESERVE", 2000),
        routing_other_apps_allowance=_int("ROUTING_OTHER_APPS_ALLOWANCE", 0),
        routing_opening_usage=_int("ROUTING_OPENING_USAGE", 0),
        routing_hard_ceiling=_int("ROUTING_HARD_CEILING", 0),
        routing_warning_percent=_int("ROUTING_WARNING_PERCENT", 80),
        map_tile_allowance=_int("MAP_TILE_ALLOWANCE", 200000),
        traffic_tile_allowance=_int("TRAFFIC_TILE_ALLOWANCE", 200000),
        tile_safety_reserve=_int("TILE_SAFETY_RESERVE", 10000),
        map_tile_mode=_str("MAP_TILE_MODE", "proxy").lower(),
        map_tile_url=_str("MAP_TILE_URL", Config.map_tile_url),
        traffic_tile_url=_str("TRAFFIC_TILE_URL", Config.traffic_tile_url),
        traffic_refresh_minutes=_int("TRAFFIC_REFRESH_MINUTES", 5),
        observation_retention_days=_int("OBSERVATION_RETENTION_DAYS", 0),
        backup_keep=_int("BACKUP_KEEP", 14),
        daily_backup=_bool("DAILY_BACKUP", True),
    )
    return c

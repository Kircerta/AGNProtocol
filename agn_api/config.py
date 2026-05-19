from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import os


@dataclass(frozen=True)
class AppConfig:
    ssot_dir: Path
    audit_log_path: Path
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    github_webhook_secret: str | None = None
    xcode_webhook_secret: str | None = None
    telegram_bot_token: str | None = None
    local_only_mode: bool = True



def load_config() -> AppConfig:
    ssot_dir = Path(os.getenv("SSOT_DIR", "./ssot"))
    audit_log_path = Path(os.getenv("AUDIT_LOG_PATH", "./audit/events.jsonl"))
    jwt_secret = os.getenv("JWT_SECRET", "")
    jwt_algorithm = os.getenv("JWT_ALGORITHM", "HS256")
    github_webhook_secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    xcode_webhook_secret = os.getenv("XCODE_WEBHOOK_SECRET")
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    local_only_mode = str(os.getenv("AGN_LOCAL_ONLY", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if not jwt_secret:
        logging.getLogger("agn_api.config").warning(
            "JWT_SECRET is not set — all authenticated API endpoints will reject requests. "
            "Set the JWT_SECRET environment variable before deploying to production."
        )
    return AppConfig(
        ssot_dir=ssot_dir,
        audit_log_path=audit_log_path,
        jwt_secret=jwt_secret,
        jwt_algorithm=jwt_algorithm,
        github_webhook_secret=github_webhook_secret,
        xcode_webhook_secret=xcode_webhook_secret,
        telegram_bot_token=telegram_bot_token,
        local_only_mode=local_only_mode,
    )

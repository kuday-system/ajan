# logger_setup.py v1.1
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAMES = ["app", "planner", "rules", "storage", "simulator", "ollama", "lockbox", "normalizer"]

LOG_FORMAT = "%(asctime)s | %(name)-8s | %(levelname)-7s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 1 * 1024 * 1024
BACKUP_COUNT = 3

# Handler'ları tanımlamak için marker — bu setup'a ait handler'ı ayırt eder
_HANDLER_MARKER = "agent_setup_v1"


def setup_logging(
    log_dir: Path,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    log_filename: str = "agent.log",
) -> None:
    """
    Merkezi logging kurulumu.
    Tekrar çağrılırsa mevcut agent handler'larını temizler, yeniden kurar.
    Dışarıdan eklenmiş handler'lara dokunmaz.
    """
    # Düzeltme 1: Klasör yoksa oluştur
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_filename

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    for name in LOGGER_NAMES:
        logger = logging.getLogger(name)

        # Düzeltme 2: Sadece bu setup'ın koyduğu handler'ları temizle
        existing = [h for h in logger.handlers if getattr(h, "_marker", None) == _HANDLER_MARKER]
        for h in existing:
            logger.removeHandler(h)
            h.close()

        logger.setLevel(logging.DEBUG)

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(console_level)
        console_handler.setFormatter(formatter)
        console_handler._marker = _HANDLER_MARKER

        # Rotating file handler
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        file_handler._marker = _HANDLER_MARKER

        logger.addHandler(console_handler)
        logger.addHandler(file_handler)
        logger.propagate = False


def truncate_for_log(text: str, max_len: int = 80) -> str:
    """Ham kullanıcı metni veya path'leri loglarda kısaltır."""
    if not isinstance(text, str):
        return "<not-str>"
    cleaned = text.replace("\n", " ").replace("\r", " ").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + f"… [{len(cleaned) - max_len} char daha]"
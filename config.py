# config.py v1.2.1
# Değişiklikler:
#   - ALLOWED_BASE_PATH kaldırıldı
#   - INTERNAL_BASE_PATH eklendi
#   - ALLOWED_USER_ZONES sistemden türetiliyor
#   - OneDrive redirection otomatik
#   - Var olmayan zone'lar listeye eklenmiyor
#   - Duplicate temizliği eklendi
#   - Tüm zone'lar .resolve() ile normalize ediliyor

import os
from pathlib import Path


# --- Proje İç Alanı ---

BASE_DIR = Path(__file__).resolve().parent
INTERNAL_BASE_PATH = BASE_DIR

LOG_DIR = (BASE_DIR / "logs").resolve()
DATA_DIR = (BASE_DIR / "data").resolve()
DB_PATH  = (DATA_DIR / "agent.db").resolve()


# --- Kullanıcı Zonu Türetici ---

def _get_shell_folder(folder_id: int, fallback_name: str) -> Path | None:
    """
    Windows shell'den gerçek klasör yolunu alır (SHGetFolderPathW).
    Başarısız veya klasör mevcut değilse USERPROFILE/fallback_name dener.
    Her ikisi de başarısızsa None döner — kör ekleme yapılmaz.
    """
    # Önce shell'den dene
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        result = ctypes.windll.shell32.SHGetFolderPathW(0, folder_id, 0, 0, buf)
        if result == 0:  # S_OK
            p = Path(buf.value).resolve()
            if p.exists() and p.is_dir():
                return p
    except Exception:
        pass

    # Fallback: USERPROFILE altında tahmin
    try:
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            p = (Path(userprofile) / fallback_name).resolve()
            if p.exists() and p.is_dir():
                return p
    except Exception:
        pass

    return None  # Hiçbiri çalışmadı — zone'a eklenmeyecek


def _get_downloads() -> Path | None:
    """
    Downloads klasörü Windows'ta standart shell ID'si yok.
    Önce registry'den okur, sonra USERPROFILE/Downloads dener.
    Mevcut değilse None döner.
    """
    # Registry'den dene
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        )
        val, _ = winreg.QueryValueEx(
            key,
            "{374DE290-123F-4565-9164-39C4925E467B}"
        )
        winreg.CloseKey(key)
        p = Path(val).resolve()
        if p.exists() and p.is_dir():
            return p
    except Exception:
        pass

    # Fallback: USERPROFILE/Downloads
    try:
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            p = (Path(userprofile) / "Downloads").resolve()
            if p.exists() and p.is_dir():
                return p
    except Exception:
        pass

    return None


# --- Zone Listesi Oluştur ---

def _build_allowed_zones() -> list[Path]:
    """
    Gerçek sistem yollarından zone listesi üretir.
    - Var olmayan zone'lar eklenmez
    - Duplicate'ler temizlenir
    - Tüm yollar resolve() ile normalize edilir
    """
    candidates = [
        _get_shell_folder(0, "Desktop"),    # Desktop (OneDrive dahil)
        _get_shell_folder(5, "Documents"),  # Documents (OneDrive dahil)
        _get_downloads(),                   # Downloads
    ]

    seen = set()
    zones = []
    for p in candidates:
        if p is None:
            continue
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            zones.append(p)

    return zones


# Dışarıya açılan zone sabitleri
_zones = _build_allowed_zones()

DESKTOP_DIR   = next((z for z in _zones if "desktop"   in str(z).lower() or
                                           "masaüstü"  in str(z).lower()), None)
DOCUMENTS_DIR = next((z for z in _zones if "documents" in str(z).lower() or
                                           "belgeler"  in str(z).lower()), None)
DOWNLOADS_DIR = next((z for z in _zones if "downloads" in str(z).lower() or
                                           "indirilenler" in str(z).lower()), None)

ALLOWED_USER_ZONES: list[Path] = _zones  # deduplicate + normalize edilmiş


# --- Uygulama Sabitleri ---

APP_NAME        = "Yerel Ajan v1.4"
DEFAULT_MODEL   = "qwen2.5:7b"
OLLAMA_URL      = "http://127.0.0.1:11434/api/generate"
OLLAMA_TIMEOUT  = 240 # saniye — 7b için artırıldı
OLLAMA_TEMPERATURE = 0.1  # düşük sıcaklık — tutarlı çıktı için
MAX_STEPS       = 5
MAX_RETRIES     = 3
DEFAULT_PERMISSION = "deny"
RISK_LEVELS     = ("low", "medium", "high", "critical")
PATH_NORMALIZE  = True


# --- Güvenlik Listeleri ---

FORBIDDEN_KEYWORDS = (
    "cmd", "powershell", "registry",
    "system32", "vm", "kali", "metasploitable",
)

FORBIDDEN_PATH_HINTS = (
    "c:\\windows",
    "c:\\system32",
    "c:\\program files",
    "programdata",
    "appdata",
)

FORBIDDEN_EXTENSIONS = (
    ".exe", ".bat", ".cmd", ".ps1", ".msi",
)
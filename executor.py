# executor.py v2.9
# Değişiklikler v2.8'e göre:
#   WEB_SEARCH handler v1.1:
#     - _SENSITIVE_QUERY_PATTERNS daraltıldı:
#         r"secret" → r"secret\s*="
#         r"token"  → r"token\s*="
#       "secret nedir" ve "token nasıl çalışır" artık bloklanmaz.
#     - _handle_web_search içinde whitespace normalize adımı
#       açık query_raw / query ayrımıyla yazıldı.
#
# [DIAG] Teşhis logları eklendi (davranış değişikliği yok):
#   - lockbox.verify sonrası, executor.run içinde step başında target repr logu
#   - handler çağrılmadan önce action + target repr logu
#   - _handle_open_url başında url, scheme, netloc, path repr logları

import json
import logging
import os
import re
import uuid
import webbrowser
from pathlib import Path, PureWindowsPath
from urllib.parse import quote_plus, urlparse

from config import DESKTOP_DIR, DOCUMENTS_DIR, DOWNLOADS_DIR
from lockbox import PlanLockbox
from models import AgentPlan, ActionType, ActionGroup, LockedPlan
from executor_models import (
    ExecutionResult,
    ExecutionStatus,
    RollbackCapability,
    StepExecutionResult,
)
from rule_engine import check_path_hardened, INTERNET_ACTIONS, INTERNET_SCOPE, FILE_SCOPES
from rollback_manager import RollbackManager

logger = logging.getLogger("executor")
_diag  = logging.getLogger("diag.executor")

# --- URL sabitleri ---
_ALLOWED_URL_SCHEMES = {"http", "https"}

# --- WEB_SEARCH sabitleri ---
_WEB_SEARCH_MAX_QUERY_LEN = 300
_SEARCH_BASE_URL = "https://www.google.com/search?q="

_SENSITIVE_QUERY_PATTERNS = [
    r"password\s*=",
    r"api[_\-]?key",
    r"secret\s*=",   # "secret nedir" geçer; "secret=abc123" bloklanır
    r"token\s*=",    # "token nasıl çalışır" geçer; "token=xyz" bloklanır
    r"C:\\Users\\",
    r"\.env",
    r"private\s*key",
]
_SENSITIVE_RE = re.compile("|".join(_SENSITIVE_QUERY_PATTERNS), re.IGNORECASE)


# --- Target Resolver ---

def _get_userprofile_dir() -> Path | None:
    up = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if up:
        p = Path(up)
        if p.exists() and p.is_dir():
            return p
    return None


_USERPROFILE_DIR = _get_userprofile_dir()

_SHORT_LABEL_MAP = {
    "desktop":   DESKTOP_DIR,
    "documents": DOCUMENTS_DIR,
    "downloads": DOWNLOADS_DIR,
    "userhome":  _USERPROFILE_DIR,
}

_ALLOWED_READ_ZONES   = {p for p in [DESKTOP_DIR, DOCUMENTS_DIR, DOWNLOADS_DIR] if p}
_ALLOWED_WRITE_ZONES  = {p for p in [DESKTOP_DIR, DOCUMENTS_DIR, DOWNLOADS_DIR] if p}
_ALLOWED_READ_SUFFIX  = ".txt"
_ALLOWED_WRITE_SUFFIX = ".txt"

_READ_FILE_MAX_CHARS         = 4_000
_WRITE_FILE_BACKUP_MAX_CHARS = 100_000


def _is_in_zone(path: Path, zones: set) -> bool:
    for zone in zones:
        try:
            path.relative_to(zone)
            return True
        except ValueError:
            continue
    return False


def _resolve_target(target: str) -> Path | None:
    expanded = os.path.expandvars(target.strip())
    normalized = expanded.lower().replace("\\", "/")

    for label, base_path in _SHORT_LABEL_MAP.items():
        if base_path is None:
            continue
        if normalized == label:
            return base_path
        if normalized.startswith(label + "/"):
            suffix = expanded[len(label):].lstrip("\\/")
            return base_path / suffix

    try:
        return Path(str(PureWindowsPath(expanded)))
    except Exception:
        return None


# --- Handler'lar ---

def _handle_create_dir(step) -> StepExecutionResult:
    target = step.target
    resolved = _resolve_target(target)
    if resolved is None:
        return StepExecutionResult(
            step_no=0, action=ActionType.CREATE_DIR.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef path çözümlenemedi: {target}", error="RESOLVE_FAILED",
        )

    _BARE_ZONES = {p for p in [DESKTOP_DIR, DOCUMENTS_DIR, DOWNLOADS_DIR, _USERPROFILE_DIR] if p}
    if resolved in _BARE_ZONES:
        return StepExecutionResult(
            step_no=0, action=ActionType.CREATE_DIR.value, target=target,
            status=ExecutionStatus.SKIPPED,
            message=f"Hedef sadece zone kökü, klasör adı eksik: {target}",
            error="BARE_ZONE_TARGET",
        )

    already_existed = resolved.exists()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        return StepExecutionResult(
            step_no=0, action=ActionType.CREATE_DIR.value, target=target,
            status=ExecutionStatus.SUCCESS,
            message=f"Klasör oluşturuldu: {resolved}",
            rollback_available=not already_existed,
            rollback_capability=(
                RollbackCapability.FULL if not already_existed else RollbackCapability.NONE
            ),
            rollback_metadata={"created_new": not already_existed, "resolved_path": str(resolved)},
        )
    except PermissionError as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.CREATE_DIR.value, target=target,
            status=ExecutionStatus.FAILED, message=f"İzin hatası: {e}", error=str(e),
        )
    except Exception as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.CREATE_DIR.value, target=target,
            status=ExecutionStatus.FAILED, message=f"Hata: {e}", error=str(e),
        )


def _handle_list_dir(step) -> StepExecutionResult:
    target = step.target
    resolved = _resolve_target(target)
    if resolved is None:
        return StepExecutionResult(
            step_no=0, action=ActionType.LIST_DIR.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef path çözümlenemedi: {target}", error="RESOLVE_FAILED",
        )
    try:
        if not resolved.exists():
            return StepExecutionResult(
                step_no=0, action=ActionType.LIST_DIR.value, target=target,
                status=ExecutionStatus.FAILED,
                message=f"Klasör bulunamadı: {resolved}", error="PATH_NOT_FOUND",
            )
        if not resolved.is_dir():
            return StepExecutionResult(
                step_no=0, action=ActionType.LIST_DIR.value, target=target,
                status=ExecutionStatus.FAILED,
                message=f"Hedef klasör değil: {resolved}", error="NOT_A_DIRECTORY",
            )
        entries = [e.name for e in resolved.iterdir()]
        return StepExecutionResult(
            step_no=0, action=ActionType.LIST_DIR.value, target=target,
            status=ExecutionStatus.SUCCESS,
            message=(
                f"{len(entries)} öğe listelendi: "
                f"{', '.join(entries[:10])}"
                f"{'...' if len(entries) > 10 else ''}"
            ),
            rollback_available=False,
            rollback_capability=RollbackCapability.NONE,
        )
    except Exception as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.LIST_DIR.value, target=target,
            status=ExecutionStatus.FAILED, message=f"Hata: {e}", error=str(e),
        )


def _handle_read_file(step) -> StepExecutionResult:
    target = step.target
    resolved = _resolve_target(target)
    if resolved is None:
        return StepExecutionResult(
            step_no=0, action=ActionType.READ_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef path çözümlenemedi: {target}", error="RESOLVE_FAILED",
        )
    if resolved.suffix.lower() != _ALLOWED_READ_SUFFIX:
        return StepExecutionResult(
            step_no=0, action=ActionType.READ_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"READ_FILE sadece {_ALLOWED_READ_SUFFIX} dosyasını okur: {target}",
            error="EXTENSION_DENIED",
        )
    if not _is_in_zone(resolved, _ALLOWED_READ_ZONES):
        return StepExecutionResult(
            step_no=0, action=ActionType.READ_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef izinli zone dışında: {target}", error="ZONE_DENIED",
        )
    if not resolved.exists():
        return StepExecutionResult(
            step_no=0, action=ActionType.READ_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Dosya bulunamadı: {resolved}", error="FILE_NOT_FOUND",
        )
    if not resolved.is_file():
        return StepExecutionResult(
            step_no=0, action=ActionType.READ_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef bir dosya değil: {resolved}", error="NOT_A_FILE",
        )
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > _READ_FILE_MAX_CHARS
        preview = content[:_READ_FILE_MAX_CHARS]
        return StepExecutionResult(
            step_no=0, action=ActionType.READ_FILE.value, target=target,
            status=ExecutionStatus.SUCCESS,
            message=(
                f"{resolved.name} okundu ({len(content)} karakter"
                f"{', ilk 4000 gösteriliyor [truncated]' if truncated else ''}):\n{preview}"
            ),
            rollback_available=False,
            rollback_capability=RollbackCapability.NONE,
        )
    except PermissionError as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.READ_FILE.value, target=target,
            status=ExecutionStatus.FAILED, message=f"İzin hatası: {e}", error=str(e),
        )
    except Exception as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.READ_FILE.value, target=target,
            status=ExecutionStatus.FAILED, message=f"Hata: {e}", error=str(e),
        )


def _handle_write_file(step) -> StepExecutionResult:
    target  = step.target
    content = step.content

    if not content or not content.strip():
        return StepExecutionResult(
            step_no=0, action=ActionType.WRITE_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message="WRITE_FILE için içerik (content) zorunlu.", error="CONTENT_MISSING",
        )
    resolved = _resolve_target(target)
    if resolved is None:
        return StepExecutionResult(
            step_no=0, action=ActionType.WRITE_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef path çözümlenemedi: {target}", error="RESOLVE_FAILED",
        )
    if resolved.suffix.lower() != _ALLOWED_WRITE_SUFFIX:
        return StepExecutionResult(
            step_no=0, action=ActionType.WRITE_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"WRITE_FILE sadece {_ALLOWED_WRITE_SUFFIX} dosyasına izin verir: {target}",
            error="EXTENSION_DENIED",
        )
    if not resolved.stem:
        return StepExecutionResult(
            step_no=0, action=ActionType.WRITE_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Dosya adı geçersiz (sadece uzantıdan ibaret): {target}",
            error="INVALID_FILENAME",
        )
    if not _is_in_zone(resolved, _ALLOWED_WRITE_ZONES):
        return StepExecutionResult(
            step_no=0, action=ActionType.WRITE_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef izinli zone dışında: {target}", error="ZONE_DENIED",
        )
    if not resolved.parent.exists():
        return StepExecutionResult(
            step_no=0, action=ActionType.WRITE_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Klasör mevcut değil, önce oluştur: {resolved.parent}",
            error="PARENT_NOT_FOUND",
        )
    if resolved.exists() and not resolved.is_file():
        return StepExecutionResult(
            step_no=0, action=ActionType.WRITE_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef bir klasör, dosya değil: {resolved}", error="IS_A_DIRECTORY",
        )

    file_existed_before = resolved.exists()
    previous_content    = None
    backup_truncated    = False

    if file_existed_before:
        try:
            raw = resolved.read_text(encoding="utf-8", errors="replace")
            if len(raw) > _WRITE_FILE_BACKUP_MAX_CHARS:
                previous_content = raw[:_WRITE_FILE_BACKUP_MAX_CHARS]
                backup_truncated = True
            else:
                previous_content = raw
        except Exception as e:
            return StepExecutionResult(
                step_no=0, action=ActionType.WRITE_FILE.value, target=target,
                status=ExecutionStatus.FAILED,
                message=f"Mevcut dosya okunamadı (backup alınamadı): {e}",
                error="BACKUP_FAILED",
            )

    rollback_action = (
        "restore_previous_content" if file_existed_before else "delete_created_file"
    )

    try:
        resolved.write_text(content, encoding="utf-8")
        return StepExecutionResult(
            step_no=0, action=ActionType.WRITE_FILE.value, target=target,
            status=ExecutionStatus.SUCCESS,
            message=(
                f"Dosya {'güncellendi' if file_existed_before else 'oluşturuldu'}: "
                f"{resolved} ({len(content)} karakter)"
            ),
            rollback_available=True,
            rollback_capability=RollbackCapability.FULL,
            rollback_metadata={
                "rollback_action":     rollback_action,
                "file_existed_before": file_existed_before,
                "previous_content":    previous_content,
                "resolved_path":       str(resolved),
                "backup_truncated":    backup_truncated,
            },
        )
    except PermissionError as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.WRITE_FILE.value, target=target,
            status=ExecutionStatus.FAILED, message=f"İzin hatası: {e}", error=str(e),
        )
    except Exception as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.WRITE_FILE.value, target=target,
            status=ExecutionStatus.FAILED, message=f"Hata: {e}", error=str(e),
        )


def _handle_append_file(step) -> StepExecutionResult:
    target  = step.target
    content = step.content

    if not content or not content.strip():
        return StepExecutionResult(
            step_no=0, action=ActionType.APPEND_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message="APPEND_FILE için içerik (content) zorunlu.", error="CONTENT_MISSING",
        )
    resolved = _resolve_target(target)
    if resolved is None:
        return StepExecutionResult(
            step_no=0, action=ActionType.APPEND_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef path çözümlenemedi: {target}", error="RESOLVE_FAILED",
        )
    if resolved.suffix.lower() != _ALLOWED_WRITE_SUFFIX:
        return StepExecutionResult(
            step_no=0, action=ActionType.APPEND_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"APPEND_FILE sadece {_ALLOWED_WRITE_SUFFIX} dosyasına izin verir: {target}",
            error="EXTENSION_DENIED",
        )
    if not resolved.stem:
        return StepExecutionResult(
            step_no=0, action=ActionType.APPEND_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Dosya adı geçersiz (sadece uzantıdan ibaret): {target}",
            error="INVALID_FILENAME",
        )
    if not _is_in_zone(resolved, _ALLOWED_WRITE_ZONES):
        return StepExecutionResult(
            step_no=0, action=ActionType.APPEND_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef izinli zone dışında: {target}", error="ZONE_DENIED",
        )
    if not resolved.exists():
        return StepExecutionResult(
            step_no=0, action=ActionType.APPEND_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Dosya bulunamadı, append için dosya mevcut olmalı: {resolved}",
            error="FILE_NOT_FOUND",
        )
    if not resolved.is_file():
        return StepExecutionResult(
            step_no=0, action=ActionType.APPEND_FILE.value, target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef bir dosya değil: {resolved}", error="NOT_A_FILE",
        )

    size_before = resolved.stat().st_size
    try:
        with resolved.open("a", encoding="utf-8") as f:
            if size_before > 0:
                f.write("\n")
            f.write(content)
        return StepExecutionResult(
            step_no=0, action=ActionType.APPEND_FILE.value, target=target,
            status=ExecutionStatus.SUCCESS,
            message=f"İçerik eklendi: {resolved} ({len(content)} karakter eklendi)",
            rollback_available=True,
            rollback_capability=RollbackCapability.PARTIAL,
            rollback_metadata={"resolved_path": str(resolved), "size_before": size_before},
        )
    except PermissionError as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.APPEND_FILE.value, target=target,
            status=ExecutionStatus.FAILED, message=f"İzin hatası: {e}", error=str(e),
        )
    except Exception as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.APPEND_FILE.value, target=target,
            status=ExecutionStatus.FAILED, message=f"Hata: {e}", error=str(e),
        )


def _handle_open_url(step) -> StepExecutionResult:
    url = step.target.strip() if step.target else ""

    # [DIAG] _handle_open_url başında — ham değerler
    _diag.debug(f"[DIAG] _handle_open_url | repr(url)={repr(url)}")

    if not url:
        return StepExecutionResult(
            step_no=0, action=ActionType.OPEN_URL.value, target=step.target,
            status=ExecutionStatus.FAILED,
            message="URL boş olamaz.", error="EMPTY_URL",
        )

    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        # [DIAG] urlparse sonuçları
        _diag.debug(
            f"[DIAG] urlparse | repr(scheme)={repr(parsed.scheme)} "
            f"| repr(netloc)={repr(parsed.netloc)} "
            f"| repr(path)={repr(parsed.path)}"
        )
    except Exception:
        return StepExecutionResult(
            step_no=0, action=ActionType.OPEN_URL.value, target=url,
            status=ExecutionStatus.FAILED,
            message=f"URL geçersiz format: {url}", error="MALFORMED_URL",
        )

    if not scheme:
        return StepExecutionResult(
            step_no=0, action=ActionType.OPEN_URL.value, target=url,
            status=ExecutionStatus.FAILED,
            message=f"URL scheme bulunamadı (http:// veya https:// gerekli): {url}",
            error="MALFORMED_URL",
        )

    if scheme not in _ALLOWED_URL_SCHEMES:
        return StepExecutionResult(
            step_no=0, action=ActionType.OPEN_URL.value, target=url,
            status=ExecutionStatus.FAILED,
            message=f"İzinsiz URL scheme: '{scheme}'. Sadece http ve https kabul edilir.",
            error="SCHEME_DENIED",
        )

    if not parsed.netloc:
        return StepExecutionResult(
            step_no=0, action=ActionType.OPEN_URL.value, target=url,
            status=ExecutionStatus.FAILED,
            message=f"URL geçersiz (host bulunamadı): {url}", error="MALFORMED_URL",
        )

    try:
        opened = webbrowser.open(url)
    except Exception as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.OPEN_URL.value, target=url,
            status=ExecutionStatus.FAILED,
            message=f"Tarayıcı açma hatası: {e}", error="BROWSER_OPEN_FAILED",
        )

    if not opened:
        return StepExecutionResult(
            step_no=0, action=ActionType.OPEN_URL.value, target=url,
            status=ExecutionStatus.FAILED,
            message=f"Tarayıcı açılamadı (webbrowser.open False): {url}",
            error="BROWSER_OPEN_FAILED",
        )

    return StepExecutionResult(
        step_no=0, action=ActionType.OPEN_URL.value, target=url,
        status=ExecutionStatus.SUCCESS,
        message=f"URL tarayıcıda açıldı: {url}",
        rollback_available=False,
        rollback_capability=RollbackCapability.PARTIAL,
        rollback_metadata={
            "opened_url": url,
            "note": "Tab kapatma garantisi yoktur; rollback mümkün değil.",
        },
    )


def _handle_web_search(step) -> StepExecutionResult:
    """
    WEB_SEARCH handler v1.1

    Güvenlik contract'ı:
      - query_raw = step.target veya ""
      - query     = query_raw.strip()
      - query boş / sadece whitespace → QUERY_EMPTY
      - query > 300 karakter → QUERY_TOO_LONG
      - hassas pattern eşleşirse → SENSITIVE_QUERY_BLOCKED
          Bloklanır : password=, api_key, secret=, token=, C:\\Users\\, .env, private key
          Geçer     : "secret nedir", "token nasıl çalışır"
      - query URL encode edilir, Google search URL'sine eklenir
      - webbrowser.open False → BROWSER_OPEN_FAILED
      - os.system / shell komutu kullanılmaz
      - rollback_available=False, rollback_capability=NONE
      - scope kontrolü executor.run'da yapılır
    """
    query_raw = step.target or ""
    query = query_raw.strip()

    if not query:
        return StepExecutionResult(
            step_no=0, action=ActionType.WEB_SEARCH.value, target=step.target,
            status=ExecutionStatus.FAILED,
            message="Arama sorgusu boş olamaz.", error="QUERY_EMPTY",
        )

    if len(query) > _WEB_SEARCH_MAX_QUERY_LEN:
        return StepExecutionResult(
            step_no=0, action=ActionType.WEB_SEARCH.value, target=step.target,
            status=ExecutionStatus.FAILED,
            message=f"Arama sorgusu çok uzun ({len(query)} karakter, max {_WEB_SEARCH_MAX_QUERY_LEN}).",
            error="QUERY_TOO_LONG",
        )

    if _SENSITIVE_RE.search(query):
        logger.warning(f"WEB_SEARCH hassas pattern | query={query[:60]}")
        return StepExecutionResult(
            step_no=0, action=ActionType.WEB_SEARCH.value, target=step.target,
            status=ExecutionStatus.FAILED,
            message="Arama sorgusu hassas veri pattern'i içeriyor.",
            error="SENSITIVE_QUERY_BLOCKED",
        )

    search_url = _SEARCH_BASE_URL + quote_plus(query)

    try:
        opened = webbrowser.open(search_url)
    except Exception as e:
        return StepExecutionResult(
            step_no=0, action=ActionType.WEB_SEARCH.value, target=step.target,
            status=ExecutionStatus.FAILED,
            message=f"Tarayıcı açma hatası: {e}", error="BROWSER_OPEN_FAILED",
        )

    if not opened:
        return StepExecutionResult(
            step_no=0, action=ActionType.WEB_SEARCH.value, target=step.target,
            status=ExecutionStatus.FAILED,
            message="Tarayıcı açılamadı (webbrowser.open False).",
            error="BROWSER_OPEN_FAILED",
        )

    logger.info(f"WEB_SEARCH | query={query[:60]} | url={search_url}")
    return StepExecutionResult(
        step_no=0, action=ActionType.WEB_SEARCH.value, target=step.target,
        status=ExecutionStatus.SUCCESS,
        message=f"Arama sayfası açıldı: {search_url}",
        rollback_available=False,
        rollback_capability=RollbackCapability.NONE,
        rollback_metadata={
            "query": query,
            "search_url": search_url,
            "note": "Arama sayfası açıldı; rollback yok.",
        },
    )


# --- Handler Map ---

_HANDLER_MAP = {
    ActionType.CREATE_DIR:  _handle_create_dir,
    ActionType.LIST_DIR:    _handle_list_dir,
    ActionType.READ_FILE:   _handle_read_file,
    ActionType.WRITE_FILE:  _handle_write_file,
    ActionType.APPEND_FILE: _handle_append_file,
    ActionType.OPEN_URL:    _handle_open_url,
    ActionType.WEB_SEARCH:  _handle_web_search,
}


# --- Action-Scope Guard ---

def _check_action_scope(action: ActionType, scope: str, step_no: int) -> StepExecutionResult | None:
    if action in INTERNET_ACTIONS and scope != INTERNET_SCOPE:
        return StepExecutionResult(
            step_no=step_no,
            action=action.value,
            target="",
            status=ExecutionStatus.FAILED,
            message=(
                f"{action.value} action'ı 'Internet' scope gerektirir. "
                f"Mevcut scope: '{scope}'. Silent fallback yok."
            ),
            error="SCOPE_REQUIRED_INTERNET",
        )
    if action in ActionGroup.USER_FILE and scope == INTERNET_SCOPE:
        return StepExecutionResult(
            step_no=step_no,
            action=action.value,
            target="",
            status=ExecutionStatus.FAILED,
            message=(
                f"Dosya action'ı ({action.value}) 'Internet' scope ile çalışamaz. "
                f"Silent fallback yok."
            ),
            error="SCOPE_MISMATCH_FILE_INTERNET",
        )
    return None


# --- Executor ---

class Executor:
    def __init__(self, lockbox: PlanLockbox):
        self.lockbox = lockbox

    def run(self, locked: LockedPlan) -> ExecutionResult:
        run_id = str(uuid.uuid4())[:8]
        logger.info(f"Execution başladı | run_id={run_id} | hash={locked.plan_hash[:12]}")

        if not self.lockbox.verify(locked):
            logger.error(f"Hash doğrulama başarısız | run_id={run_id}")
            return ExecutionResult(
                run_id=run_id,
                status=ExecutionStatus.FAILED,
                total_steps=0,
                completed_steps=0,
                step_results=[],
                summary="Hash doğrulaması başarısız. Execution durduruldu.",
            )

        plan         = AgentPlan(**json.loads(locked.canonical_plan_json))
        scope        = plan.permission_scope
        step_results = []
        completed    = 0
        rollback_mgr = RollbackManager()

        # [DIAG] Lockbox çözüldü — plan.steps[0].target repr (lockbox sonrası)
        try:
            if plan.steps:
                _diag.debug(
                    f"[DIAG] executor.run | lockbox sonrası plan.steps[0].target repr: "
                    f"{repr(plan.steps[0].target)}"
                )
        except Exception:
            pass

        for step in plan.steps:
            logger.info(
                f"Step başladı | run_id={run_id} | "
                f"step={step.step_no} | action={step.action.value}"
            )

            # [DIAG] Handler çağrılmadan önce — action + target repr
            _diag.debug(
                f"[DIAG] executor.run | pre-handler | "
                f"step={step.step_no} | action={repr(step.action.value)} | "
                f"target repr={repr(step.target)}"
            )

            if step.action not in _HANDLER_MAP:
                logger.warning(f"Action izinsiz | run_id={run_id} | action={step.action.value}")
                step_results.append(StepExecutionResult(
                    step_no=step.step_no,
                    action=step.action.value,
                    target=step.target,
                    status=ExecutionStatus.SKIPPED,
                    message=f"Action izin listesinde değil: {step.action.value}",
                ))
                break

            scope_guard = _check_action_scope(step.action, scope, step.step_no)
            if scope_guard is not None:
                logger.warning(
                    f"Scope guard reddi | run_id={run_id} | "
                    f"action={step.action.value} | scope={scope}"
                )
                step_results.append(scope_guard)
                break

            path_hard, _ = check_path_hardened(
                step.target, step.step_no, step.action
            )
            if path_hard:
                logger.warning(f"Path preflight başarısız | run_id={run_id} | {path_hard}")
                step_results.append(StepExecutionResult(
                    step_no=step.step_no,
                    action=step.action.value,
                    target=step.target,
                    status=ExecutionStatus.SKIPPED,
                    message=f"Preflight başarısız: {path_hard[0]}",
                ))
                break

            handler = _HANDLER_MAP[step.action]
            result  = handler(step)
            result.step_no = step.step_no
            step_results.append(result)

            if result.status == ExecutionStatus.SUCCESS:
                completed += 1
                if result.rollback_available:
                    rollback_mgr.register(result)
            else:
                logger.error(
                    f"Step başarısız | run_id={run_id} | "
                    f"step={step.step_no} | {result.error}"
                )
                break

        overall = (
            ExecutionStatus.SUCCESS
            if completed == len(plan.steps)
            else ExecutionStatus.FAILED
        )

        rollback_lines: list[str] = []
        if overall == ExecutionStatus.FAILED and rollback_mgr.has_registered():
            logger.info(f"Rollback başlatılıyor | run_id={run_id}")
            for rb in rollback_mgr.rollback_all():
                line = (
                    f"[Rollback] Adım {rb.step_no} ({rb.action}): "
                    f"{rb.status} — {rb.message}"
                )
                rollback_lines.append(line)
                logger.info(f"rollback sonuç | run_id={run_id} | {line}")

        logger.info(
            f"Execution bitti | run_id={run_id} | "
            f"status={overall.value} | completed={completed}/{len(plan.steps)}"
        )

        if overall == ExecutionStatus.SUCCESS:
            summary = f"Execution tamamlandı. {completed}/{len(plan.steps)} adım başarılı."
        else:
            summary = f"Execution durduruldu. {completed}/{len(plan.steps)} adım tamamlandı."
            if rollback_lines:
                summary += "\n" + "\n".join(rollback_lines)

        return ExecutionResult(
            run_id=run_id,
            status=overall,
            total_steps=len(plan.steps),
            completed_steps=completed,
            step_results=step_results,
            rolled_back=bool(rollback_lines),
            summary=summary,
        )

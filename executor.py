# executor.py v2.4
# Değişiklikler v2.3'e göre:
#   YENİ — RollbackManager entegrasyonu:
#           from rollback_manager import RollbackManager  ← import eklendi
#           rollback_mgr = RollbackManager()              ← run() başında
#           rollback_mgr.register(result)                 ← her SUCCESS sonrası
#           rollback_mgr.rollback_all()                   ← fail durumunda

import json
import logging
import os
import uuid
from pathlib import Path, PureWindowsPath

from config import DESKTOP_DIR, DOCUMENTS_DIR, DOWNLOADS_DIR
from lockbox import PlanLockbox
from models import AgentPlan, ActionType, LockedPlan
from executor_models import (
    ExecutionResult,
    ExecutionStatus,
    RollbackCapability,
    StepExecutionResult,
)
from rule_engine import check_path_hardened
from rollback_manager import RollbackManager          # ← YENİ SATIR 1

logger = logging.getLogger("executor")


# --- Target Resolver ---

def _get_userprofile_dir() -> Path | None:
    """Gerçek USERPROFILE dizinini sistemden alır."""
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

_READ_FILE_MAX_CHARS = 4000


def _is_in_zone(path: Path, zones: set) -> bool:
    """Path'in izinli zone içinde olup olmadığını Path.relative_to() ile kontrol eder."""
    for zone in zones:
        try:
            path.relative_to(zone)
            return True
        except ValueError:
            continue
    return False


def _resolve_target(target: str) -> Path | None:
    """
    Kısa etiket veya path string'ini gerçek Path'e çevirir.
    1) expandvars — %USERPROFILE% vb.
    2) Kısa etiket kontrolü
    3) Gerçek path — expand edilmiş string üzerinden
    """
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
    """
    CREATE_DIR handler.
    Rollback: oluşturulan boş klasörü silebilir.
    """
    target = step.target
    resolved = _resolve_target(target)
    if resolved is None:
        return StepExecutionResult(
            step_no=0,
            action=ActionType.CREATE_DIR.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef path çözümlenemedi: {target}",
            error="RESOLVE_FAILED",
        )

    _BARE_ZONES = {p for p in [DESKTOP_DIR, DOCUMENTS_DIR, DOWNLOADS_DIR, _USERPROFILE_DIR] if p}
    if resolved in _BARE_ZONES:
        logger.warning(f"CREATE_DIR bare zone | target={target} | resolved={resolved}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.CREATE_DIR.value,
            target=target,
            status=ExecutionStatus.SKIPPED,
            message=f"Hedef sadece zone kökü, klasör adı eksik: {target}",
            error="BARE_ZONE_TARGET",
        )

    already_existed = resolved.exists()

    try:
        resolved.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"CREATE_DIR | target={target} | resolved={resolved} | "
            f"already_existed={already_existed}"
        )
        return StepExecutionResult(
            step_no=0,
            action=ActionType.CREATE_DIR.value,
            target=target,
            status=ExecutionStatus.SUCCESS,
            message=f"Klasör oluşturuldu: {resolved}",
            rollback_available=not already_existed,
            rollback_capability=(
                RollbackCapability.FULL if not already_existed
                else RollbackCapability.NONE
            ),
            rollback_metadata={
                "created_new": not already_existed,
                "resolved_path": str(resolved),
            },
        )
    except PermissionError as e:
        logger.error(
            f"CREATE_DIR izin hatası | target={target} | resolved={resolved} | {e}"
        )
        return StepExecutionResult(
            step_no=0,
            action=ActionType.CREATE_DIR.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"İzin hatası: {e}",
            error=str(e),
        )
    except Exception as e:
        logger.error(
            f"CREATE_DIR hatası | target={target} | resolved={resolved} | {e}"
        )
        return StepExecutionResult(
            step_no=0,
            action=ActionType.CREATE_DIR.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hata: {e}",
            error=str(e),
        )


def _handle_list_dir(step) -> StepExecutionResult:
    """
    LIST_DIR handler.
    Rollback: yok (salt okunur).
    """
    target = step.target
    resolved = _resolve_target(target)
    if resolved is None:
        return StepExecutionResult(
            step_no=0,
            action=ActionType.LIST_DIR.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef path çözümlenemedi: {target}",
            error="RESOLVE_FAILED",
        )

    try:
        if not resolved.exists():
            return StepExecutionResult(
                step_no=0,
                action=ActionType.LIST_DIR.value,
                target=target,
                status=ExecutionStatus.FAILED,
                message=f"Klasör bulunamadı: {resolved}",
                error="PATH_NOT_FOUND",
            )

        if not resolved.is_dir():
            return StepExecutionResult(
                step_no=0,
                action=ActionType.LIST_DIR.value,
                target=target,
                status=ExecutionStatus.FAILED,
                message=f"Hedef klasör değil: {resolved}",
                error="NOT_A_DIRECTORY",
            )

        entries = [e.name for e in resolved.iterdir()]
        logger.info(
            f"LIST_DIR | target={target} | resolved={resolved} | count={len(entries)}"
        )
        return StepExecutionResult(
            step_no=0,
            action=ActionType.LIST_DIR.value,
            target=target,
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
        logger.error(
            f"LIST_DIR hatası | target={target} | resolved={resolved} | {e}"
        )
        return StepExecutionResult(
            step_no=0,
            action=ActionType.LIST_DIR.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hata: {e}",
            error=str(e),
        )


def _handle_read_file(step) -> StepExecutionResult:
    """
    READ_FILE handler v1.
    Kurallar:
      - Sadece izinli zone'lar: Desktop, Documents, Downloads
      - Sadece .txt uzantısı
      - Dosya yoksa FAIL
      - Hedef dosya değilse FAIL
      - İlk 4000 karakter gösterilir
      - Zone kontrolü Path.relative_to() ile yapılır
    """
    target = step.target
    resolved = _resolve_target(target)
    if resolved is None:
        return StepExecutionResult(
            step_no=0,
            action=ActionType.READ_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef path çözümlenemedi: {target}",
            error="RESOLVE_FAILED",
        )

    if resolved.suffix.lower() != _ALLOWED_READ_SUFFIX:
        logger.warning(f"READ_FILE uzantı reddi | target={target} | suffix={resolved.suffix}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.READ_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"READ_FILE sadece {_ALLOWED_READ_SUFFIX} dosyasını okur: {target}",
            error="EXTENSION_DENIED",
        )

    if not _is_in_zone(resolved, _ALLOWED_READ_ZONES):
        logger.warning(f"READ_FILE zone reddi | target={target} | resolved={resolved}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.READ_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef izinli zone dışında: {target}",
            error="ZONE_DENIED",
        )

    if not resolved.exists():
        return StepExecutionResult(
            step_no=0,
            action=ActionType.READ_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Dosya bulunamadı: {resolved}",
            error="FILE_NOT_FOUND",
        )

    if not resolved.is_file():
        return StepExecutionResult(
            step_no=0,
            action=ActionType.READ_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef bir dosya değil: {resolved}",
            error="NOT_A_FILE",
        )

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > _READ_FILE_MAX_CHARS
        preview = content[:_READ_FILE_MAX_CHARS]
        logger.info(
            f"READ_FILE | target={target} | resolved={resolved} | "
            f"chars={len(content)} | truncated={truncated}"
        )
        return StepExecutionResult(
            step_no=0,
            action=ActionType.READ_FILE.value,
            target=target,
            status=ExecutionStatus.SUCCESS,
            message=(
                f"{resolved.name} okundu ({len(content)} karakter"
                f"{', ilk 4000 gösteriliyor [truncated]' if truncated else ''}):\n{preview}"
            ),
            rollback_available=False,
            rollback_capability=RollbackCapability.NONE,
        )
    except PermissionError as e:
        logger.error(f"READ_FILE izin hatası | target={target} | {e}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.READ_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"İzin hatası: {e}",
            error=str(e),
        )
    except Exception as e:
        logger.error(f"READ_FILE hatası | target={target} | {e}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.READ_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hata: {e}",
            error=str(e),
        )


def _handle_write_file(step) -> StepExecutionResult:
    """
    WRITE_FILE handler v1.1
    Kurallar:
      - Sadece izinli zone'lar: Desktop, Documents, Downloads
      - Sadece .txt uzantısı
      - content yoksa FAIL
      - Dosya adı sadece uzantıdan ibaretse FAIL (.txt gibi)
      - Parent klasör yoksa FAIL
      - Mevcut dosyanın üstüne yazma: DENY
      - Zone kontrolü Path.relative_to() ile yapılır
    """
    target = step.target
    content = step.content

    if not content or not content.strip():
        logger.warning(f"WRITE_FILE içerik boş | target={target}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.WRITE_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message="WRITE_FILE için içerik (content) zorunlu.",
            error="CONTENT_MISSING",
        )

    resolved = _resolve_target(target)
    if resolved is None:
        return StepExecutionResult(
            step_no=0,
            action=ActionType.WRITE_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef path çözümlenemedi: {target}",
            error="RESOLVE_FAILED",
        )

    if resolved.suffix.lower() != _ALLOWED_WRITE_SUFFIX:
        logger.warning(f"WRITE_FILE uzantı reddi | target={target} | suffix={resolved.suffix}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.WRITE_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"WRITE_FILE sadece {_ALLOWED_WRITE_SUFFIX} dosyasına izin verir: {target}",
            error="EXTENSION_DENIED",
        )

    if not resolved.stem:
        logger.warning(f"WRITE_FILE geçersiz dosya adı | target={target} | resolved={resolved}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.WRITE_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Dosya adı geçersiz (sadece uzantıdan ibaret): {target}",
            error="INVALID_FILENAME",
        )

    if not _is_in_zone(resolved, _ALLOWED_WRITE_ZONES):
        logger.warning(f"WRITE_FILE zone reddi | target={target} | resolved={resolved}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.WRITE_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef izinli zone dışında: {target}",
            error="ZONE_DENIED",
        )

    if not resolved.parent.exists():
        logger.warning(f"WRITE_FILE parent yok | target={target} | parent={resolved.parent}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.WRITE_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Klasör mevcut değil, önce oluştur: {resolved.parent}",
            error="PARENT_NOT_FOUND",
        )

    if resolved.exists():
        logger.warning(f"WRITE_FILE overwrite reddi | target={target} | resolved={resolved}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.WRITE_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Dosya zaten mevcut, üstüne yazma bu sürümde desteklenmiyor: {resolved}",
            error="OVERWRITE_DENIED",
        )

    try:
        resolved.write_text(content, encoding="utf-8")
        logger.info(f"WRITE_FILE | target={target} | resolved={resolved} | chars={len(content)}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.WRITE_FILE.value,
            target=target,
            status=ExecutionStatus.SUCCESS,
            message=f"Dosya yazıldı: {resolved} ({len(content)} karakter)",
            rollback_available=True,
            rollback_capability=RollbackCapability.FULL,
            rollback_metadata={"resolved_path": str(resolved)},
        )
    except PermissionError as e:
        logger.error(f"WRITE_FILE izin hatası | target={target} | {e}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.WRITE_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"İzin hatası: {e}",
            error=str(e),
        )
    except Exception as e:
        logger.error(f"WRITE_FILE hatası | target={target} | {e}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.WRITE_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hata: {e}",
            error=str(e),
        )


def _handle_append_file(step) -> StepExecutionResult:
    """
    APPEND_FILE handler v1.
    Kurallar:
      - Sadece izinli zone'lar: Desktop, Documents, Downloads
      - Sadece .txt uzantısı
      - content yoksa FAIL
      - Dosya adı sadece uzantıdan ibaretse FAIL
      - Dosya yoksa FAIL (WRITE_FILE'dan farklı — append için dosya var olmalı)
      - Hedef dosya değilse FAIL
      - Zone kontrolü _is_in_zone / _ALLOWED_WRITE_ZONES ile yapılır
      - Rollback: PARTIAL (append öncesi boyut kaydedilir, içerik geri alınamaz)
    """
    target = step.target
    content = step.content

    if not content or not content.strip():
        logger.warning(f"APPEND_FILE içerik boş | target={target}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.APPEND_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message="APPEND_FILE için içerik (content) zorunlu.",
            error="CONTENT_MISSING",
        )

    resolved = _resolve_target(target)
    if resolved is None:
        return StepExecutionResult(
            step_no=0,
            action=ActionType.APPEND_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef path çözümlenemedi: {target}",
            error="RESOLVE_FAILED",
        )

    if resolved.suffix.lower() != _ALLOWED_WRITE_SUFFIX:
        logger.warning(f"APPEND_FILE uzantı reddi | target={target} | suffix={resolved.suffix}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.APPEND_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"APPEND_FILE sadece {_ALLOWED_WRITE_SUFFIX} dosyasına izin verir: {target}",
            error="EXTENSION_DENIED",
        )

    if not resolved.stem:
        logger.warning(f"APPEND_FILE geçersiz dosya adı | target={target} | resolved={resolved}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.APPEND_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Dosya adı geçersiz (sadece uzantıdan ibaret): {target}",
            error="INVALID_FILENAME",
        )

    if not _is_in_zone(resolved, _ALLOWED_WRITE_ZONES):
        logger.warning(f"APPEND_FILE zone reddi | target={target} | resolved={resolved}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.APPEND_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef izinli zone dışında: {target}",
            error="ZONE_DENIED",
        )

    if not resolved.exists():
        logger.warning(f"APPEND_FILE dosya yok | target={target} | resolved={resolved}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.APPEND_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Dosya bulunamadı, append için dosya mevcut olmalı: {resolved}",
            error="FILE_NOT_FOUND",
        )

    if not resolved.is_file():
        logger.warning(f"APPEND_FILE hedef dosya değil | target={target} | resolved={resolved}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.APPEND_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hedef bir dosya değil: {resolved}",
            error="NOT_A_FILE",
        )

    size_before = resolved.stat().st_size

    try:
        with resolved.open("a", encoding="utf-8") as f:
            if size_before > 0:
                f.write("\n")
            f.write(content)
        logger.info(
            f"APPEND_FILE | target={target} | resolved={resolved} | "
            f"appended_chars={len(content)} | size_before={size_before}"
        )
        return StepExecutionResult(
            step_no=0,
            action=ActionType.APPEND_FILE.value,
            target=target,
            status=ExecutionStatus.SUCCESS,
            message=f"İçerik eklendi: {resolved} ({len(content)} karakter eklendi)",
            rollback_available=True,
            rollback_capability=RollbackCapability.PARTIAL,
            rollback_metadata={
                "resolved_path": str(resolved),
                "size_before": size_before,
            },
        )
    except PermissionError as e:
        logger.error(f"APPEND_FILE izin hatası | target={target} | {e}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.APPEND_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"İzin hatası: {e}",
            error=str(e),
        )
    except Exception as e:
        logger.error(f"APPEND_FILE hatası | target={target} | {e}")
        return StepExecutionResult(
            step_no=0,
            action=ActionType.APPEND_FILE.value,
            target=target,
            status=ExecutionStatus.FAILED,
            message=f"Hata: {e}",
            error=str(e),
        )


# --- Handler Map ---

_HANDLER_MAP = {
    ActionType.CREATE_DIR:   _handle_create_dir,
    ActionType.LIST_DIR:     _handle_list_dir,
    ActionType.READ_FILE:    _handle_read_file,
    ActionType.WRITE_FILE:   _handle_write_file,
    ActionType.APPEND_FILE:  _handle_append_file,
}


# --- Executor ---

class Executor:
    def __init__(self, lockbox: PlanLockbox):
        self.lockbox = lockbox

    def run(self, locked: LockedPlan) -> ExecutionResult:
        run_id = str(uuid.uuid4())[:8]
        logger.info(
            f"Execution başladı | run_id={run_id} | hash={locked.plan_hash[:12]}"
        )

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
        step_results = []
        completed    = 0
        rollback_mgr = RollbackManager()              # ← YENİ SATIR 2

        for step in plan.steps:
            logger.info(
                f"Step başladı | run_id={run_id} | "
                f"step={step.step_no} | action={step.action.value}"
            )

            if step.action not in _HANDLER_MAP:
                logger.warning(
                    f"Action izinsiz | run_id={run_id} | action={step.action.value}"
                )
                step_results.append(StepExecutionResult(
                    step_no=step.step_no,
                    action=step.action.value,
                    target=step.target,
                    status=ExecutionStatus.SKIPPED,
                    message=f"Action izin listesinde değil: {step.action.value}",
                ))
                break

            path_hard, _ = check_path_hardened(
                step.target, step.step_no, step.action
            )
            if path_hard:
                logger.warning(
                    f"Path preflight başarısız | run_id={run_id} | {path_hard}"
                )
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

        # ← YENİ BLOK: fail durumunda rollback
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

# rollback_manager.py v1.2
# Yerel Güvenli Ajan v1.4
#
# Görev: Execution sırasında başarılı mutating adımları kayıt altına alır.
#         Sonraki bir adım fail olursa kayıtlı adımları ters sırada geri alır.
#
# Kapsam:
#   WRITE_FILE  → FULL    → rollback_action'a göre: dosya sil veya previous_content geri yaz
#   CREATE_DIR  → FULL    → klasör boşsa sil
#   APPEND_FILE → PARTIAL → size_before byte'ına truncate
#   READ_FILE, LIST_DIR → NONE → atla
#
# Değişiklikler v1.1'e göre:
#   register():
#     - step_result.status != ExecutionStatus.SUCCESS ise kayıt alınmıyor
#     - ExecutionStatus executor_models'dan import edildi
#
# Değişiklikler v1.0'a göre:
#   _rollback_write_file:
#     - rollback_action zorunlu okunuyor ("restore_previous_content" | "delete_created_file")
#     - "restore_previous_content": previous_content write_text ile geri yazılıyor
#     - previous_content=None ise FAILED dönüyor
#     - backup_truncated=True ise restore engelleniyor, açık FAILED mesajı veriliyor
#     - bilinmeyen rollback_action FAILED dönüyor
#   rollback_all:
#     - her handler çağrısı try/except içine alındı; exception rollback zincirini kırmıyor
#   _validate_rollback_path:
#     - resolved_path rollback sırasında zone'a karşı validate ediliyor
#     - zone dışı ise işlem yapılmıyor, FAILED dönüyor
#
# Kullanım (executor içinden):
#   manager = RollbackManager()
#   manager.register(step_result)          # her başarılı adımdan sonra
#   results = manager.rollback_all()       # fail durumunda

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from config import DESKTOP_DIR, DOCUMENTS_DIR, DOWNLOADS_DIR
from executor_models import ExecutionStatus, RollbackCapability, StepExecutionResult

logger = logging.getLogger("rollback_manager")

# ---------------------------------------------------------------------------
# Zone validation — executor'daki zone set'iyle senkron tutulmalı
# ---------------------------------------------------------------------------

_ALLOWED_WRITE_ZONES: set[Path] = {
    p for p in [DESKTOP_DIR, DOCUMENTS_DIR, DOWNLOADS_DIR] if p
}


def _validate_rollback_path(path: Path) -> tuple[bool, str]:
    """
    resolved_path'in izinli write zone içinde olup olmadığını doğrular.
    Döner: (geçerli_mi: bool, hata_mesajı: str)
    Zone içindeyse (True, "") döner.
    Zone dışındaysa (False, açıklama) döner.
    """
    for zone in _ALLOWED_WRITE_ZONES:
        try:
            path.relative_to(zone)
            return True, ""
        except ValueError:
            continue
    return False, f"resolved_path izinli zone dışında, rollback engellendi: {path}"


# ---------------------------------------------------------------------------
# RollbackResult
# ---------------------------------------------------------------------------

class RollbackStatus(str, Enum):
    SUCCESS = "ROLLBACK_SUCCESS"
    FAILED  = "ROLLBACK_FAILED"
    SKIPPED = "ROLLBACK_SKIPPED"


@dataclass
class RollbackResult:
    step_no:    int
    action:     str
    target:     str
    status:     RollbackStatus
    message:    str


# ---------------------------------------------------------------------------
# Action handler'ları
# ---------------------------------------------------------------------------

def _rollback_write_file(metadata: dict) -> tuple[RollbackStatus, str]:
    """
    WRITE_FILE rollback.

    rollback_action'a göre iki dal:
      "delete_created_file"      → dosyayı sil (yeni oluşturulmuş dosya)
      "restore_previous_content" → previous_content'i geri yaz (overwrite)

    Güvenlik kontratı:
      - resolved_path zone'a karşı validate edilir
      - backup_truncated=True ise restore yapılmaz, FAILED döner
      - previous_content=None ise restore yapılmaz, FAILED döner
      - bilinmeyen rollback_action FAILED döner
    """
    path_str = metadata.get("resolved_path")
    if not path_str:
        return RollbackStatus.FAILED, "rollback_metadata içinde resolved_path yok."

    path = Path(path_str)

    # Zone validation
    valid, reason = _validate_rollback_path(path)
    if not valid:
        logger.error(f"rollback | WRITE_FILE | zone ihlali | {reason}")
        return RollbackStatus.FAILED, reason

    rollback_action = metadata.get("rollback_action")

    # --- Dal 1: Yeni dosya sil ---
    if rollback_action == "delete_created_file":
        if not path.exists():
            return RollbackStatus.SKIPPED, f"Dosya zaten yok, rollback gerekmez: {path}"

        if not path.is_file():
            return RollbackStatus.FAILED, f"Hedef dosya değil, silinemez: {path}"

        path.unlink()
        logger.info(f"rollback | WRITE_FILE | silindi | path={path}")
        return RollbackStatus.SUCCESS, f"Dosya silindi: {path}"

    # --- Dal 2: Overwrite — previous_content geri yaz ---
    elif rollback_action == "restore_previous_content":
        # backup_truncated kontrolü — partial restore engellensin
        if metadata.get("backup_truncated"):
            msg = (
                f"Backup truncated (100_000 karakter limiti aşılmıştı), "
                f"partial restore engellendi — veri kaybı riski: {path}"
            )
            logger.error(f"rollback | WRITE_FILE | backup_truncated=True | path={path}")
            return RollbackStatus.FAILED, msg

        previous_content = metadata.get("previous_content")
        if previous_content is None:
            msg = f"previous_content yok, restore edilemez: {path}"
            logger.error(f"rollback | WRITE_FILE | previous_content=None | path={path}")
            return RollbackStatus.FAILED, msg

        path.write_text(previous_content, encoding="utf-8")
        logger.info(f"rollback | WRITE_FILE | restore edildi | path={path}")
        return RollbackStatus.SUCCESS, f"Dosya önceki içeriğe geri döndürüldü: {path}"

    # --- Bilinmeyen rollback_action ---
    else:
        msg = f"Bilinmeyen rollback_action: {rollback_action!r} — işlem yapılmadı: {path}"
        logger.error(f"rollback | WRITE_FILE | bilinmeyen rollback_action | path={path}")
        return RollbackStatus.FAILED, msg


def _rollback_create_dir(metadata: dict) -> tuple[RollbackStatus, str]:
    """
    CREATE_DIR rollback: klasör yeni oluşturulduysa ve boşsa sil.
    created_new=False ise (zaten vardı) → atla.
    """
    created_new = metadata.get("created_new", False)
    if not created_new:
        return RollbackStatus.SKIPPED, "Klasör zaten mevcuttu, rollback gerekmez."

    path_str = metadata.get("resolved_path")
    if not path_str:
        return RollbackStatus.FAILED, "rollback_metadata içinde resolved_path yok."

    path = Path(path_str)

    # Zone validation
    valid, reason = _validate_rollback_path(path)
    if not valid:
        logger.error(f"rollback | CREATE_DIR | zone ihlali | {reason}")
        return RollbackStatus.FAILED, reason

    if not path.exists():
        return RollbackStatus.SKIPPED, f"Klasör zaten yok: {path}"

    if not path.is_dir():
        return RollbackStatus.FAILED, f"Hedef klasör değil: {path}"

    # Boş değilse silme — güvenli taraf
    try:
        entries = list(path.iterdir())
    except Exception as e:
        return RollbackStatus.FAILED, f"Klasör içeriği okunamadı: {e}"

    if entries:
        logger.warning(f"rollback | CREATE_DIR | klasör dolu, silinmedi | path={path}")
        return (
            RollbackStatus.FAILED,
            f"Klasör boş değil ({len(entries)} öğe), rollback güvensiz — silinmedi: {path}",
        )

    try:
        path.rmdir()
        logger.info(f"rollback | CREATE_DIR | silindi | path={path}")
        return RollbackStatus.SUCCESS, f"Klasör silindi: {path}"
    except PermissionError as e:
        logger.error(f"rollback | CREATE_DIR | izin hatası | path={path} | {e}")
        return RollbackStatus.FAILED, f"İzin hatası, silinemedi: {path} — {e}"
    except Exception as e:
        logger.error(f"rollback | CREATE_DIR | hata | path={path} | {e}")
        return RollbackStatus.FAILED, f"Hata: {e}"


def _rollback_append_file(metadata: dict) -> tuple[RollbackStatus, str]:
    """
    APPEND_FILE rollback: dosyayı size_before byte'ına truncate et.
    Encoding edge case'i: byte truncate ile karakter sınırı çakışabilir.
    Truncate sonrası dosya UTF-8 decode edilebiliyorsa başarılı sayılır.
    """
    path_str    = metadata.get("resolved_path")
    size_before = metadata.get("size_before")

    if not path_str:
        return RollbackStatus.FAILED, "rollback_metadata içinde resolved_path yok."

    if size_before is None:
        return RollbackStatus.FAILED, "rollback_metadata içinde size_before yok."

    path = Path(path_str)

    # Zone validation
    valid, reason = _validate_rollback_path(path)
    if not valid:
        logger.error(f"rollback | APPEND_FILE | zone ihlali | {reason}")
        return RollbackStatus.FAILED, reason

    if not path.exists():
        return RollbackStatus.SKIPPED, f"Dosya yok, rollback gerekmez: {path}"

    if not path.is_file():
        return RollbackStatus.FAILED, f"Hedef dosya değil: {path}"

    try:
        with open(path, "r+b") as f:
            f.truncate(size_before)
        logger.info(
            f"rollback | APPEND_FILE | truncate | path={path} | size_before={size_before}"
        )

        # Truncate sonrası UTF-8 bütünlüğü kontrol et
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning(
                f"rollback | APPEND_FILE | truncate sonrası UTF-8 hatası | path={path}"
            )
            return (
                RollbackStatus.SUCCESS,
                f"Truncate yapıldı ama UTF-8 bütünlüğü bozulmuş olabilir: {path} "
                f"(size_before={size_before})",
            )

        return RollbackStatus.SUCCESS, f"İçerik geri alındı (truncate): {path}"
    except PermissionError as e:
        logger.error(f"rollback | APPEND_FILE | izin hatası | path={path} | {e}")
        return RollbackStatus.FAILED, f"İzin hatası: {path} — {e}"
    except Exception as e:
        logger.error(f"rollback | APPEND_FILE | hata | path={path} | {e}")
        return RollbackStatus.FAILED, f"Hata: {e}"


# ---------------------------------------------------------------------------
# RollbackManager
# ---------------------------------------------------------------------------

@dataclass
class RollbackManager:
    """
    Başarılı mutating adımları sırasıyla kayıt altına alır.
    rollback_all() → kayıtları ters sırada geri alır.
    """
    _registry: list[StepExecutionResult] = field(default_factory=list)

    def register(self, step_result: StepExecutionResult) -> None:
        """
        Başarılı ve rollback_available=True olan adımı kayıt altına al.
        Diğerleri sessizce atlanır.
        """
        if (
            step_result.status == ExecutionStatus.SUCCESS
            and step_result.rollback_available
            and step_result.rollback_capability != RollbackCapability.NONE
        ):
            self._registry.append(step_result)
            logger.debug(
                f"rollback kayıt | step={step_result.step_no} | "
                f"action={step_result.action} | cap={step_result.rollback_capability}"
            )

    def rollback_all(self) -> list[RollbackResult]:
        """
        Kayıtlı adımları ters sırada rollback et.
        Handler exception fırlatırsa loglanır, diğer adımlara devam edilir.
        """
        if not self._registry:
            return []

        results: list[RollbackResult] = []

        for step_result in reversed(self._registry):
            action  = step_result.action
            step_no = step_result.step_no
            target  = step_result.target
            meta    = step_result.rollback_metadata or {}
            cap     = step_result.rollback_capability

            logger.info(
                f"rollback başladı | step={step_no} | action={action} | cap={cap}"
            )

            try:
                if cap == RollbackCapability.FULL and action == "WRITE_FILE":
                    status, message = _rollback_write_file(meta)

                elif cap == RollbackCapability.FULL and action == "CREATE_DIR":
                    status, message = _rollback_create_dir(meta)

                elif cap == RollbackCapability.PARTIAL and action == "APPEND_FILE":
                    status, message = _rollback_append_file(meta)

                else:
                    status  = RollbackStatus.SKIPPED
                    message = f"Rollback handler yok: action={action}, cap={cap}"
                    logger.warning(f"rollback | handler yok | step={step_no} | {message}")

            except Exception as e:
                status  = RollbackStatus.FAILED
                message = f"Handler beklenmeyen exception | action={action} | {e}"
                logger.error(
                    f"rollback | exception | step={step_no} | action={action} | {e}",
                    exc_info=True,
                )

            results.append(RollbackResult(
                step_no=step_no,
                action=action,
                target=target,
                status=status,
                message=message,
            ))

            logger.info(
                f"rollback bitti | step={step_no} | status={status} | {message}"
            )

        return results

    def has_registered(self) -> bool:
        return bool(self._registry)

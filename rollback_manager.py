# rollback_manager.py v1.0
# Yerel Güvenli Ajan v1.4
#
# Görev: Execution sırasında başarılı mutating adımları kayıt altına alır.
#         Sonraki bir adım fail olursa kayıtlı adımları ters sırada geri alır.
#
# Kapsam:
#   WRITE_FILE  → FULL    → dosyayı sil
#   CREATE_DIR  → FULL    → klasör boşsa sil
#   APPEND_FILE → PARTIAL → size_before byte'ına truncate
#   READ_FILE, LIST_DIR → NONE → atla
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

from executor_models import RollbackCapability, StepExecutionResult

logger = logging.getLogger("rollback_manager")


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
    """WRITE_FILE rollback: dosyayı sil."""
    path_str = metadata.get("resolved_path")
    if not path_str:
        return RollbackStatus.FAILED, "rollback_metadata içinde resolved_path yok."

    path = Path(path_str)
    if not path.exists():
        return RollbackStatus.SKIPPED, f"Dosya zaten yok, rollback gerekmez: {path}"

    if not path.is_file():
        return RollbackStatus.FAILED, f"Hedef dosya değil, silinemez: {path}"

    try:
        path.unlink()
        logger.info(f"rollback | WRITE_FILE | silindi | path={path}")
        return RollbackStatus.SUCCESS, f"Dosya silindi: {path}"
    except PermissionError as e:
        logger.error(f"rollback | WRITE_FILE | izin hatası | path={path} | {e}")
        return RollbackStatus.FAILED, f"İzin hatası, silinemedi: {path} — {e}"
    except Exception as e:
        logger.error(f"rollback | WRITE_FILE | hata | path={path} | {e}")
        return RollbackStatus.FAILED, f"Hata: {e}"


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
    path_str   = metadata.get("resolved_path")
    size_before = metadata.get("size_before")

    if not path_str:
        return RollbackStatus.FAILED, "rollback_metadata içinde resolved_path yok."

    if size_before is None:
        return RollbackStatus.FAILED, "rollback_metadata içinde size_before yok."

    path = Path(path_str)
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
            step_result.rollback_available
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
        Rollback başarısız olursa loglanır ama diğer adımlara devam edilir.
        """
        if not self._registry:
            return []

        results: list[RollbackResult] = []

        for step_result in reversed(self._registry):
            action  = step_result.action          # str (executor'dan gelir)
            step_no = step_result.step_no
            target  = step_result.target
            meta    = step_result.rollback_metadata or {}
            cap     = step_result.rollback_capability

            logger.info(
                f"rollback başladı | step={step_no} | action={action} | cap={cap}"
            )

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

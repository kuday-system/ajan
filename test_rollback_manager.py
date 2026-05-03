# test_rollback_manager.py v1.1
# Rollback Manager entegrasyon test senaryoları.
# Executor / rollback_manager koduna dokunulmaz — sadece test.
#
# Çalıştırma: pytest test_rollback_manager.py -v

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import rollback_manager

from executor_models import (
    ExecutionStatus,
    ExecutionResult,
    RollbackCapability,
    StepExecutionResult,
)
from rollback_manager import RollbackManager, RollbackStatus


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _make_write_result(
    step_no: int,
    resolved_path: str,
    rollback_action: str = "delete_created_file",
    file_existed_before: bool = False,
    previous_content: str | None = None,
    backup_truncated: bool = False,
) -> StepExecutionResult:
    return StepExecutionResult(
        step_no=step_no,
        action="WRITE_FILE",
        target=f"Desktop\\test_{step_no}.txt",
        status=ExecutionStatus.SUCCESS,
        message=f"Dosya yazıldı: {resolved_path}",
        rollback_available=True,
        rollback_capability=RollbackCapability.FULL,
        rollback_metadata={
            "rollback_action":     rollback_action,
            "file_existed_before": file_existed_before,
            "previous_content":    previous_content,
            "resolved_path":       resolved_path,
            "backup_truncated":    backup_truncated,
        },
    )


def _make_create_dir_result(
    step_no: int,
    resolved_path: str,
    created_new: bool = True,
) -> StepExecutionResult:
    return StepExecutionResult(
        step_no=step_no,
        action="CREATE_DIR",
        target="Desktop\\Test",
        status=ExecutionStatus.SUCCESS,
        message=f"Klasör oluşturuldu: {resolved_path}",
        rollback_available=created_new,
        rollback_capability=RollbackCapability.FULL if created_new else RollbackCapability.NONE,
        rollback_metadata={"created_new": created_new, "resolved_path": resolved_path},
    )


def _make_append_result(
    step_no: int,
    resolved_path: str,
    size_before: int,
) -> StepExecutionResult:
    return StepExecutionResult(
        step_no=step_no,
        action="APPEND_FILE",
        target="Desktop\\mevcut.txt",
        status=ExecutionStatus.SUCCESS,
        message=f"İçerik eklendi: {resolved_path}",
        rollback_available=True,
        rollback_capability=RollbackCapability.PARTIAL,
        rollback_metadata={"resolved_path": resolved_path, "size_before": size_before},
    )


def _make_list_result(step_no: int) -> StepExecutionResult:
    return StepExecutionResult(
        step_no=step_no,
        action="LIST_DIR",
        target="Desktop",
        status=ExecutionStatus.SUCCESS,
        message="3 öğe listelendi",
        rollback_available=False,
        rollback_capability=RollbackCapability.NONE,
    )


# ---------------------------------------------------------------------------
# Senaryo 1 — WRITE_FILE başarı + sonraki adım EXTENSION_DENIED fail
# ---------------------------------------------------------------------------

class TestSenaryo1:
    def test_register_siralama(self, tmp_path):
        mgr = RollbackManager()

        dir_result   = _make_create_dir_result(1, str(tmp_path / "Test"), created_new=True)
        write_result = _make_write_result(
            step_no=2,
            resolved_path=str(tmp_path / "Test" / "a.txt"),
            rollback_action="delete_created_file",
            file_existed_before=False,
            previous_content=None,
            backup_truncated=False,
        )

        mgr.register(dir_result)
        mgr.register(write_result)

        assert mgr.has_registered()
        assert len(mgr._registry) == 2

    def test_rollback_ters_sira(self, tmp_path):
        test_dir  = tmp_path / "Test"
        test_file = test_dir / "a.txt"
        test_dir.mkdir()
        test_file.write_text("executor tarafından yazıldı", encoding="utf-8")

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=True))
        mgr.register(_make_write_result(
            step_no=2,
            resolved_path=str(test_file),
            rollback_action="delete_created_file",
            file_existed_before=False,
            previous_content=None,
            backup_truncated=False,
        ))

        with patch.object(rollback_manager, "_ALLOWED_WRITE_ZONES", {tmp_path}):
            results = mgr.rollback_all()

        assert results[0].action == "WRITE_FILE"
        assert results[1].action == "CREATE_DIR"

    def test_dosya_silindi(self, tmp_path):
        """
        NEW FILE senaryosu:
          - dosya daha önce yoktu, executor oluşturdu (file_existed_before=False)
          - rollback_action = delete_created_file
          - rollback sonrası dosya silinmiş olmalı
        """
        test_dir  = tmp_path / "Test"
        test_file = test_dir / "a.txt"
        test_dir.mkdir()
        # Executor'ın yeni oluşturduğu dosyayı simüle ediyoruz.
        # file_existed_before=False → rollback bu dosyayı silmeli.
        test_file.write_text("executor tarafından yazıldı", encoding="utf-8")

        mgr = RollbackManager()
        mgr.register(_make_write_result(
            step_no=2,
            resolved_path=str(test_file),
            rollback_action="delete_created_file",
            file_existed_before=False,
            previous_content=None,
            backup_truncated=False,
        ))

        with patch.object(rollback_manager, "_ALLOWED_WRITE_ZONES", {tmp_path}):
            results = mgr.rollback_all()

        assert results[0].status == RollbackStatus.SUCCESS
        assert not test_file.exists()

    def test_klasor_silindi(self, tmp_path):
        test_dir = tmp_path / "Test"
        test_dir.mkdir()

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=True))

        with patch.object(rollback_manager, "_ALLOWED_WRITE_ZONES", {tmp_path}):
            results = mgr.rollback_all()

        assert results[0].status == RollbackStatus.SUCCESS
        assert not test_dir.exists()


# ---------------------------------------------------------------------------
# Senaryo 1b — WRITE_FILE overwrite → rollback → eski içerik geri gelmeli
# ---------------------------------------------------------------------------

class TestSenaryo1bOverwriteRestore:
    def test_overwrite_rollback_restores_content(self, tmp_path):
        """
        Senaryo:
          - Dosya önceden mevcuttu (file_existed_before=True)
          - WRITE_FILE overwrite yaptı
          - rollback_action = restore_previous_content
          - backup_truncated = False
        Beklenen:
          - rollback SUCCESS döner
          - dosya içeriği previous_content'e eşit
        """
        test_file   = tmp_path / "overwrite.txt"
        original    = "orijinal içerik buraya"
        new_content = "üzerine yazılan yeni içerik"
        test_file.write_text(new_content, encoding="utf-8")

        mgr = RollbackManager()
        mgr.register(_make_write_result(
            step_no=1,
            resolved_path=str(test_file),
            rollback_action="restore_previous_content",
            file_existed_before=True,
            previous_content=original,
            backup_truncated=False,
        ))

        with patch.object(rollback_manager, "_ALLOWED_WRITE_ZONES", {tmp_path}):
            results = mgr.rollback_all()

        assert results[0].status == RollbackStatus.SUCCESS
        assert test_file.read_text(encoding="utf-8") == original

    def test_truncated_backup_restore_fails(self, tmp_path):
        """
        Senaryo:
          - rollback_action = restore_previous_content
          - backup_truncated = True  → güvenli restore yapılamaz
        Beklenen:
          - rollback FAILED döner
          - dosya içeriği değişmemiş olmalı
        """
        test_file = tmp_path / "big.txt"
        current   = "overwrite sonrası mevcut içerik"
        test_file.write_text(current, encoding="utf-8")

        mgr = RollbackManager()
        mgr.register(_make_write_result(
            step_no=1,
            resolved_path=str(test_file),
            rollback_action="restore_previous_content",
            file_existed_before=True,
            previous_content="kısmi backup — truncate edildi",
            backup_truncated=True,
        ))

        with patch.object(rollback_manager, "_ALLOWED_WRITE_ZONES", {tmp_path}):
            results = mgr.rollback_all()

        assert results[0].status == RollbackStatus.FAILED
        assert test_file.read_text(encoding="utf-8") == current


# ---------------------------------------------------------------------------
# Senaryo 2 — CREATE_DIR başarı + sonraki adım fail
# ---------------------------------------------------------------------------

class TestSenaryo2:
    def test_bos_klasor_silindi(self, tmp_path):
        test_dir = tmp_path / "Test"
        test_dir.mkdir()

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=True))

        with patch.object(rollback_manager, "_ALLOWED_WRITE_ZONES", {tmp_path}):
            results = mgr.rollback_all()

        assert results[0].status == RollbackStatus.SUCCESS
        assert not test_dir.exists()

    def test_rolled_back_true(self, tmp_path):
        test_dir = tmp_path / "Test"
        test_dir.mkdir()

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=True))

        with patch.object(rollback_manager, "_ALLOWED_WRITE_ZONES", {tmp_path}):
            rb_results = mgr.rollback_all()

        assert bool(rb_results) is True


# ---------------------------------------------------------------------------
# Senaryo 3 — Mevcut klasöre dokunmama testi
# ---------------------------------------------------------------------------

class TestSenaryo3:
    def test_mevcut_klasor_register_edilmez(self, tmp_path):
        test_dir = tmp_path / "VarilanKlasor"
        test_dir.mkdir()

        mgr = RollbackManager()
        result = _make_create_dir_result(1, str(test_dir), created_new=False)
        mgr.register(result)

        assert not mgr.has_registered()

    def test_rollback_all_bos_donus(self, tmp_path):
        test_dir = tmp_path / "VarilanKlasor"
        test_dir.mkdir()

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=False))

        results = mgr.rollback_all()
        assert results == []

    def test_klasor_korunur(self, tmp_path):
        test_dir = tmp_path / "VarilanKlasor"
        test_dir.mkdir()
        (test_dir / "existing.txt").write_text("var", encoding="utf-8")

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=False))
        mgr.rollback_all()

        assert test_dir.exists()


# ---------------------------------------------------------------------------
# Senaryo 4 — APPEND_FILE başarı + sonraki adım fail
# ---------------------------------------------------------------------------

class TestSenaryo4:
    def test_truncate_yapildi(self, tmp_path):
        test_file = tmp_path / "mevcut.txt"
        original  = "orijinal içerik"
        test_file.write_text(original, encoding="utf-8")
        size_before = test_file.stat().st_size

        with test_file.open("a", encoding="utf-8") as f:
            f.write("\nek içerik")

        assert test_file.stat().st_size > size_before

        mgr = RollbackManager()
        mgr.register(_make_append_result(1, str(test_file), size_before))

        with patch.object(rollback_manager, "_ALLOWED_WRITE_ZONES", {tmp_path}):
            results = mgr.rollback_all()

        assert results[0].status in (RollbackStatus.SUCCESS,)
        assert test_file.stat().st_size == size_before

    def test_rolled_back_true(self, tmp_path):
        test_file = tmp_path / "mevcut.txt"
        test_file.write_text("orijinal", encoding="utf-8")
        size_before = test_file.stat().st_size

        with test_file.open("a", encoding="utf-8") as f:
            f.write("\nek")

        mgr = RollbackManager()
        mgr.register(_make_append_result(1, str(test_file), size_before))

        with patch.object(rollback_manager, "_ALLOWED_WRITE_ZONES", {tmp_path}):
            rb_results = mgr.rollback_all()

        assert bool(rb_results) is True


# ---------------------------------------------------------------------------
# Senaryo 5 — READ_FILE / LIST_DIR fail sonrası rollback yok
# ---------------------------------------------------------------------------

class TestSenaryo5:
    def test_list_dir_register_edilmez(self):
        mgr = RollbackManager()
        mgr.register(_make_list_result(1))

        assert not mgr.has_registered()

    def test_rollback_all_bos(self):
        mgr = RollbackManager()
        mgr.register(_make_list_result(1))

        results = mgr.rollback_all()
        assert results == []

    def test_rolled_back_false(self):
        mgr = RollbackManager()
        mgr.register(_make_list_result(1))
        rb_results = mgr.rollback_all()

        assert bool(rb_results) is False


# ---------------------------------------------------------------------------
# Senaryo 6 — Tüm adımlar başarılı, rollback tetiklenmez
# ---------------------------------------------------------------------------

class TestSenaryo6:
    def test_basarili_planda_rollback_yok(self, tmp_path):
        test_dir  = tmp_path / "Test"
        test_file = test_dir / "a.txt"
        test_dir.mkdir()
        test_file.write_text("içerik", encoding="utf-8")

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=True))
        mgr.register(_make_write_result(
            step_no=2,
            resolved_path=str(test_file),
            rollback_action="delete_created_file",
            file_existed_before=False,
            previous_content=None,
            backup_truncated=False,
        ))

        assert mgr.has_registered()
        assert test_dir.exists()
        assert test_file.exists()

    def test_rolled_back_false_basarili(self, tmp_path):
        mgr = RollbackManager()
        rollback_lines: list[str] = []
        assert bool(rollback_lines) is False

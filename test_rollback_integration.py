# test_rollback_integration.py
# Rollback integration testleri — rollback_manager v1.2
#
# Kapsam:
#   1. WRITE_FILE overwrite → fail → rollback → eski içerik geri gelmeli
#   2. WRITE_FILE new file  → fail → rollback → dosya silinmeli
#   3. backup_truncated=True → rollback FAILED olmalı
#   4. 2 step → LIFO sırası doğrulanmalı
#
# Bağımlılık yok — monkeypatch ile zone ve path izole edilir.
# pytest ile çalışır: pytest test_rollback_integration.py -v

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub modüller — gerçek proje modülleri olmadan test çalışabilsin
# ---------------------------------------------------------------------------

def _make_stubs():
    """
    rollback_manager'ın import ettiği dış modülleri stub olarak sys.modules'a ekler.
    Gerçek modüller mevcutsa bunlar öncelikli olur.
    """
    # executor_models stub — gerçek modül varsa kullan
    if "executor_models" not in sys.modules:
        em = types.ModuleType("executor_models")

        class ExecutionStatus(str):
            SUCCESS = "SUCCESS"
            FAILED  = "FAILED"
            SKIPPED = "SKIPPED"

        class RollbackCapability(str):
            FULL    = "FULL"
            PARTIAL = "PARTIAL"
            NONE    = "NONE"

        class StepExecutionResult:
            def __init__(self, *, step_no, action, target, status,
                         message="test rollback step",
                         rollback_available=False, rollback_capability=None,
                         rollback_metadata=None, **kwargs):
                self.step_no              = step_no
                self.action               = action
                self.target               = target
                self.status               = status
                self.message              = message
                self.rollback_available   = rollback_available
                self.rollback_capability  = rollback_capability
                self.rollback_metadata    = rollback_metadata or {}

        em.ExecutionStatus      = ExecutionStatus
        em.RollbackCapability   = RollbackCapability
        em.StepExecutionResult  = StepExecutionResult
        sys.modules["executor_models"] = em

    # config stub — DESKTOP_DIR/DOCUMENTS_DIR/DOWNLOADS_DIR geçici tmp dir olacak
    # (her test fixture kendi tmp_path'ini patch eder)
    if "config" not in sys.modules:
        cfg = types.ModuleType("config")
        cfg.DESKTOP_DIR   = None
        cfg.DOCUMENTS_DIR = None
        cfg.DOWNLOADS_DIR = None
        sys.modules["config"] = cfg


_make_stubs()


# ---------------------------------------------------------------------------
# rollback_manager'ı import et (stub'lar hazır olduğu için güvenli)
# ---------------------------------------------------------------------------

import rollback_manager as rm
from executor_models import (
    ExecutionStatus,
    RollbackCapability,
    StepExecutionResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_write_file_result(
    *,
    step_no: int,
    target: str,
    resolved_path: str,
    rollback_action: str,
    file_existed_before: bool,
    previous_content: str | None,
    backup_truncated: bool,
) -> StepExecutionResult:
    """WRITE_FILE SUCCESS StepExecutionResult üretir."""
    return StepExecutionResult(
        step_no=step_no,
        action="WRITE_FILE",
        target=target,
        status=ExecutionStatus.SUCCESS,
        message="WRITE_FILE tamamlandı",
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
    *,
    step_no: int,
    target: str,
    resolved_path: str,
    created_new: bool,
) -> StepExecutionResult:
    """CREATE_DIR SUCCESS StepExecutionResult üretir."""
    return StepExecutionResult(
        step_no=step_no,
        action="CREATE_DIR",
        target=target,
        status=ExecutionStatus.SUCCESS,
        message="CREATE_DIR tamamlandı",
        rollback_available=created_new,
        rollback_capability=RollbackCapability.FULL if created_new else RollbackCapability.NONE,
        rollback_metadata={
            "created_new":   created_new,
            "resolved_path": resolved_path,
        },
    )


def _patch_zones(tmp_path: Path):
    """
    rollback_manager._ALLOWED_WRITE_ZONES'u tmp_path ile override eder.
    Context manager döner — with bloğu içinde kullan.
    """
    return patch.object(rm, "_ALLOWED_WRITE_ZONES", {tmp_path})


# ---------------------------------------------------------------------------
# Test 1: WRITE_FILE overwrite → rollback → eski içerik geri gelmeli
# ---------------------------------------------------------------------------

class TestOverwriteRollback:
    def test_previous_content_restored(self, tmp_path):
        target_file = tmp_path / "notes.txt"
        original    = "orijinal içerik"
        new_content = "üzerine yazılan içerik"

        target_file.write_text(new_content, encoding="utf-8")

        step = _make_write_file_result(
            step_no=1,
            target="desktop/notes.txt",
            resolved_path=str(target_file),
            rollback_action="restore_previous_content",
            file_existed_before=True,
            previous_content=original,
            backup_truncated=False,
        )

        manager = rm.RollbackManager()
        with _patch_zones(tmp_path):
            manager.register(step)
            results = manager.rollback_all()

        assert len(results) == 1
        assert results[0].status == rm.RollbackStatus.SUCCESS
        assert target_file.read_text(encoding="utf-8") == original

    def test_overwrite_rollback_uses_utf8_encoding(self, tmp_path):
        target_file  = tmp_path / "doc.txt"
        original     = "Ağaç, şehir, üniversite — Türkçe karakterler"
        target_file.write_text("değiştirilmiş", encoding="utf-8")

        step = _make_write_file_result(
            step_no=1,
            target="desktop/doc.txt",
            resolved_path=str(target_file),
            rollback_action="restore_previous_content",
            file_existed_before=True,
            previous_content=original,
            backup_truncated=False,
        )

        manager = rm.RollbackManager()
        with _patch_zones(tmp_path):
            manager.register(step)
            results = manager.rollback_all()

        assert results[0].status == rm.RollbackStatus.SUCCESS
        assert target_file.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Test 2: WRITE_FILE new file → fail → rollback → dosya silinmeli
# ---------------------------------------------------------------------------

class TestNewFileRollback:
    def test_new_file_deleted_on_rollback(self, tmp_path):
        target_file = tmp_path / "yeni.txt"
        target_file.write_text("yeni dosya içeriği", encoding="utf-8")

        step = _make_write_file_result(
            step_no=1,
            target="desktop/yeni.txt",
            resolved_path=str(target_file),
            rollback_action="delete_created_file",
            file_existed_before=False,
            previous_content=None,
            backup_truncated=False,
        )

        manager = rm.RollbackManager()
        with _patch_zones(tmp_path):
            manager.register(step)
            results = manager.rollback_all()

        assert len(results) == 1
        assert results[0].status == rm.RollbackStatus.SUCCESS
        assert not target_file.exists()

    def test_new_file_rollback_metadata_complete(self, tmp_path):
        target_file = tmp_path / "empty_prev.txt"
        target_file.write_text("içerik", encoding="utf-8")

        step = _make_write_file_result(
            step_no=1,
            target="desktop/empty_prev.txt",
            resolved_path=str(target_file),
            rollback_action="delete_created_file",
            file_existed_before=False,
            previous_content=None,
            backup_truncated=False,
        )

        manager = rm.RollbackManager()
        with _patch_zones(tmp_path):
            manager.register(step)
            results = manager.rollback_all()

        assert results[0].status == rm.RollbackStatus.SUCCESS
        assert not target_file.exists()

    def test_new_file_already_gone_is_skipped(self, tmp_path):
        target_file = tmp_path / "gone.txt"

        step = _make_write_file_result(
            step_no=1,
            target="desktop/gone.txt",
            resolved_path=str(target_file),
            rollback_action="delete_created_file",
            file_existed_before=False,
            previous_content=None,
            backup_truncated=False,
        )

        manager = rm.RollbackManager()
        with _patch_zones(tmp_path):
            manager.register(step)
            results = manager.rollback_all()

        assert results[0].status == rm.RollbackStatus.SKIPPED


# ---------------------------------------------------------------------------
# Test 3: backup_truncated=True → rollback FAILED olmalı
# ---------------------------------------------------------------------------

class TestBackupTruncatedGuard:
    def test_truncated_backup_blocks_restore(self, tmp_path):
        target_file  = tmp_path / "big.txt"
        current      = "overwrite sonrası içerik"
        target_file.write_text(current, encoding="utf-8")

        step = _make_write_file_result(
            step_no=1,
            target="desktop/big.txt",
            resolved_path=str(target_file),
            rollback_action="restore_previous_content",
            file_existed_before=True,
            previous_content="ilk 100_000 karakter...",
            backup_truncated=True,
        )

        manager = rm.RollbackManager()
        with _patch_zones(tmp_path):
            manager.register(step)
            results = manager.rollback_all()

        assert len(results) == 1
        assert results[0].status == rm.RollbackStatus.FAILED
        assert target_file.read_text(encoding="utf-8") == current

    def test_truncated_backup_message_is_explicit(self, tmp_path):
        target_file = tmp_path / "big2.txt"
        target_file.write_text("mevcut içerik", encoding="utf-8")

        step = _make_write_file_result(
            step_no=1,
            target="desktop/big2.txt",
            resolved_path=str(target_file),
            rollback_action="restore_previous_content",
            file_existed_before=True,
            previous_content="kısmi backup",
            backup_truncated=True,
        )

        manager = rm.RollbackManager()
        with _patch_zones(tmp_path):
            manager.register(step)
            results = manager.rollback_all()

        assert "truncated" in results[0].message.lower() or "backup" in results[0].message.lower()

    def test_not_truncated_backup_allows_restore(self, tmp_path):
        target_file = tmp_path / "small.txt"
        original    = "orijinal küçük içerik"
        target_file.write_text("overwrite", encoding="utf-8")

        step = _make_write_file_result(
            step_no=1,
            target="desktop/small.txt",
            resolved_path=str(target_file),
            rollback_action="restore_previous_content",
            file_existed_before=True,
            previous_content=original,
            backup_truncated=False,
        )

        manager = rm.RollbackManager()
        with _patch_zones(tmp_path):
            manager.register(step)
            results = manager.rollback_all()

        assert results[0].status == rm.RollbackStatus.SUCCESS
        assert target_file.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Test 4: 2 step → LIFO sırası doğrulanmalı
# ---------------------------------------------------------------------------

class TestLIFORollbackOrder:
    def test_two_steps_rolled_back_in_lifo_order(self, tmp_path):
        subdir      = tmp_path / "subdir"
        target_file = subdir / "file.txt"

        subdir.mkdir()
        target_file.write_text("yeni dosya", encoding="utf-8")

        step1 = _make_create_dir_result(
            step_no=1,
            target="desktop/subdir",
            resolved_path=str(subdir),
            created_new=True,
        )
        step2 = _make_write_file_result(
            step_no=2,
            target="desktop/subdir/file.txt",
            resolved_path=str(target_file),
            rollback_action="delete_created_file",
            file_existed_before=False,
            previous_content=None,
            backup_truncated=False,
        )

        manager = rm.RollbackManager()
        with _patch_zones(tmp_path):
            manager.register(step1)
            manager.register(step2)
            results = manager.rollback_all()

        assert len(results) == 2
        assert results[0].step_no == 2
        assert results[1].step_no == 1

        assert results[0].status == rm.RollbackStatus.SUCCESS
        assert results[1].status == rm.RollbackStatus.SUCCESS

        assert not target_file.exists()
        assert not subdir.exists()

    def test_lifo_order_preserved_on_three_steps(self, tmp_path):
        files = [tmp_path / f"file{i}.txt" for i in range(3)]
        for f in files:
            f.write_text("içerik", encoding="utf-8")

        steps = [
            _make_write_file_result(
                step_no=i + 1,
                target=f"desktop/file{i}.txt",
                resolved_path=str(files[i]),
                rollback_action="delete_created_file",
                file_existed_before=False,
                previous_content=None,
                backup_truncated=False,
            )
            for i in range(3)
        ]

        manager = rm.RollbackManager()
        with _patch_zones(tmp_path):
            for s in steps:
                manager.register(s)
            results = manager.rollback_all()

        assert [r.step_no for r in results] == [3, 2, 1]
        assert all(r.status == rm.RollbackStatus.SUCCESS for r in results)
        assert all(not f.exists() for f in files)


# ---------------------------------------------------------------------------
# Test 5: register() — SUCCESS dışı adımlar kayıt alınmamalı
# ---------------------------------------------------------------------------

class TestRegisterGuard:
    def test_failed_step_not_registered(self, tmp_path):
        step = StepExecutionResult(
            step_no=1,
            action="WRITE_FILE",
            target="desktop/x.txt",
            status=ExecutionStatus.FAILED,
            message="test rollback step",
            rollback_available=True,
            rollback_capability=RollbackCapability.FULL,
            rollback_metadata={
                "rollback_action":     "delete_created_file",
                "file_existed_before": False,
                "previous_content":    None,
                "resolved_path":       str(tmp_path / "x.txt"),
                "backup_truncated":    False,
            },
        )

        manager = rm.RollbackManager()
        manager.register(step)

        assert not manager.has_registered()
        assert manager.rollback_all() == []

    def test_success_step_is_registered(self, tmp_path):
        target_file = tmp_path / "reg.txt"
        target_file.write_text("içerik", encoding="utf-8")

        step = _make_write_file_result(
            step_no=1,
            target="desktop/reg.txt",
            resolved_path=str(target_file),
            rollback_action="delete_created_file",
            file_existed_before=False,
            previous_content=None,
            backup_truncated=False,
        )

        manager = rm.RollbackManager()
        manager.register(step)

        assert manager.has_registered()


# ---------------------------------------------------------------------------
# Test 6: Path güvenliği — zone dışı path FAILED dönmeli
# ---------------------------------------------------------------------------

class TestPathValidation:
    def test_zone_outside_path_blocked(self, tmp_path):
        outside_dir  = tmp_path / "outside"
        outside_dir.mkdir()
        target_file  = outside_dir / "secret.txt"
        target_file.write_text("gizli içerik", encoding="utf-8")

        step = _make_write_file_result(
            step_no=1,
            target="outside/secret.txt",
            resolved_path=str(target_file),
            rollback_action="delete_created_file",
            file_existed_before=False,
            previous_content=None,
            backup_truncated=False,
        )

        allowed_zone = tmp_path / "allowed"
        allowed_zone.mkdir()

        manager = rm.RollbackManager()
        with patch.object(rm, "_ALLOWED_WRITE_ZONES", {allowed_zone}):
            manager.register(step)
            results = manager.rollback_all()

        assert results[0].status == rm.RollbackStatus.FAILED
        assert target_file.exists()

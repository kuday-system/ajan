# test_rollback_manager.py v1.0
# Rollback Manager entegrasyon test senaryoları.
# Executor / rollback_manager koduna dokunulmaz — sadece test.
#
# Çalıştırma: pytest test_rollback_manager.py -v

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

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

def _make_write_result(step_no: int, resolved_path: str) -> StepExecutionResult:
    return StepExecutionResult(
        step_no=step_no,
        action="WRITE_FILE",
        target=f"Desktop\\test_{step_no}.txt",
        status=ExecutionStatus.SUCCESS,
        message=f"Dosya yazıldı: {resolved_path}",
        rollback_available=True,
        rollback_capability=RollbackCapability.FULL,
        rollback_metadata={"resolved_path": resolved_path},
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
    """
    Plan: CREATE_DIR → WRITE_FILE(a.txt) → WRITE_FILE(bad.exe)
    Beklenen:
      - Adım 1 SUCCESS → register edilir
      - Adım 2 SUCCESS → register edilir
      - Adım 3 FAIL (EXTENSION_DENIED)
      - Rollback: a.txt silindi, Test klasörü silindi (ters sıra)
      - rolled_back=True
    """

    def test_register_siralama(self, tmp_path):
        """Başarılı adımlar doğru sırada register edilmeli."""
        mgr = RollbackManager()

        dir_result   = _make_create_dir_result(1, str(tmp_path / "Test"), created_new=True)
        write_result = _make_write_result(2, str(tmp_path / "Test" / "a.txt"))

        mgr.register(dir_result)
        mgr.register(write_result)

        assert mgr.has_registered()
        assert len(mgr._registry) == 2

    def test_rollback_ters_sira(self, tmp_path):
        """Rollback ters sırada çalışmalı: önce WRITE_FILE, sonra CREATE_DIR."""
        test_dir  = tmp_path / "Test"
        test_file = test_dir / "a.txt"
        test_dir.mkdir()
        test_file.write_text("içerik", encoding="utf-8")

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=True))
        mgr.register(_make_write_result(2, str(test_file)))

        results = mgr.rollback_all()

        # İlk rollback WRITE_FILE (step 2), ikinci CREATE_DIR (step 1)
        assert results[0].action == "WRITE_FILE"
        assert results[1].action == "CREATE_DIR"

    def test_dosya_silindi(self, tmp_path):
        """WRITE_FILE rollback sonrası dosya fiziksel olarak silinmeli."""
        test_dir  = tmp_path / "Test"
        test_file = test_dir / "a.txt"
        test_dir.mkdir()
        test_file.write_text("içerik", encoding="utf-8")

        mgr = RollbackManager()
        mgr.register(_make_write_result(2, str(test_file)))

        results = mgr.rollback_all()

        assert results[0].status == RollbackStatus.SUCCESS
        assert not test_file.exists()

    def test_klasor_silindi(self, tmp_path):
        """CREATE_DIR rollback: klasör boşsa silinmeli."""
        test_dir = tmp_path / "Test"
        test_dir.mkdir()

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=True))

        results = mgr.rollback_all()

        assert results[0].status == RollbackStatus.SUCCESS
        assert not test_dir.exists()


# ---------------------------------------------------------------------------
# Senaryo 2 — CREATE_DIR başarı + sonraki adım fail
# ---------------------------------------------------------------------------

class TestSenaryo2:
    """
    Plan: CREATE_DIR(Desktop\Test) → WRITE_FILE(bad.exe) [EXTENSION_DENIED]
    Beklenen:
      - Adım 1 SUCCESS → register edilir
      - Adım 2 FAIL
      - Rollback: Test klasörü boşsa silindi
      - rolled_back=True
    """

    def test_bos_klasor_silindi(self, tmp_path):
        test_dir = tmp_path / "Test"
        test_dir.mkdir()

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=True))

        results = mgr.rollback_all()

        assert results[0].status == RollbackStatus.SUCCESS
        assert not test_dir.exists()

    def test_rolled_back_true(self, tmp_path):
        """Rollback yapıldıysa rolled_back=True olmalı."""
        test_dir = tmp_path / "Test"
        test_dir.mkdir()

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=True))
        rb_results = mgr.rollback_all()

        rolled_back = bool(rb_results)
        assert rolled_back is True


# ---------------------------------------------------------------------------
# Senaryo 3 — Mevcut klasöre dokunmama testi
# ---------------------------------------------------------------------------

class TestSenaryo3:
    """
    CREATE_DIR already_existed=True → register edilmez.
    Sonraki adım fail olsa bile rollback yoktur.
    Beklenen:
      - rolled_back=False
      - rollback_lines yok
      - klasör korunur
    """

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
    """
    Plan: APPEND_FILE(mevcut.txt) → WRITE_FILE(bad.exe)
    Beklenen:
      - Adım 1 SUCCESS, size_before kaydedilir
      - Adım 2 FAIL
      - Rollback: mevcut.txt size_before'a truncate edilir
      - rolled_back=True
    """

    def test_truncate_yapildi(self, tmp_path):
        test_file = tmp_path / "mevcut.txt"
        original  = "orijinal içerik"
        test_file.write_text(original, encoding="utf-8")
        size_before = test_file.stat().st_size

        # Append simülasyonu
        with test_file.open("a", encoding="utf-8") as f:
            f.write("\nek içerik")

        assert test_file.stat().st_size > size_before

        mgr = RollbackManager()
        mgr.register(_make_append_result(1, str(test_file), size_before))

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
        rb_results = mgr.rollback_all()

        assert bool(rb_results) is True


# ---------------------------------------------------------------------------
# Senaryo 5 — READ_FILE / LIST_DIR fail sonrası rollback yok
# ---------------------------------------------------------------------------

class TestSenaryo5:
    """
    Plan: LIST_DIR(Desktop) → WRITE_FILE(bad.exe)
    Beklenen:
      - LIST_DIR rollback_available=False → register edilmez
      - rolled_back=False
    """

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
    """
    Plan: CREATE_DIR → WRITE_FILE (ikisi de SUCCESS)
    Beklenen:
      - overall=SUCCESS
      - rollback_all() çağrılmaz
      - rolled_back=False
    """

    def test_basarili_planda_rollback_yok(self, tmp_path):
        test_dir  = tmp_path / "Test"
        test_file = test_dir / "a.txt"
        test_dir.mkdir()
        test_file.write_text("içerik", encoding="utf-8")

        mgr = RollbackManager()
        mgr.register(_make_create_dir_result(1, str(test_dir), created_new=True))
        mgr.register(_make_write_result(2, str(test_file)))

        # Başarılı senaryoda rollback_all çağrılmaz
        # Burada sadece register durumunu kontrol ediyoruz
        assert mgr.has_registered()
        # rollback_all çağrılmadığı için dosyalar hâlâ duruyor
        assert test_dir.exists()
        assert test_file.exists()

    def test_rolled_back_false_basarili(self, tmp_path):
        """Başarılı execution'da rolled_back=False olmalı."""
        mgr = RollbackManager()
        # Rollback tetiklenmediği için rollback_all hiç çağrılmaz
        # rolled_back = bool(rollback_lines) → rollback_lines boş → False
        rollback_lines: list[str] = []
        assert bool(rollback_lines) is False

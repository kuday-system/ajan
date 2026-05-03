"""
test_executor_write_file.py
Yerel Güvenli Ajan v2 — WRITE_FILE handler testleri

Kapsam kararları (açık):
  - Atomic write   : v1.4 kapsamı dışı, test edilmiyor.
  - Binary/encoding: kapsam dışı, previous_encoding metadata'da beklenmez.
  - Backup         : write'tan ÖNCE alınması zorunlu → doğrulanıyor.
  - Overwrite      : destekleniyor (yeni spec, v1.2).
  - Rollback meta  : implicit değil, executor açıkça üretmeli.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def make_locked_plan(
    target: str,
    content: str = "test içeriği",
    step_no: int = 1,
    requires_real_execution: bool = False,
    # requires_real_execution=False zorunlu:
    # WRITE_FILE MUTATING sayıldığı için rule_engine True olanı reddeder.
):
    from models import AgentPlan, PlanStep, ActionType
    from lockbox import PlanLockbox

    plan = AgentPlan(
        goal="Dosya yaz",
        summary="Belirtilen dosyaya içerik yazılır",
        steps=[
            PlanStep(
                step_no=step_no,
                action=ActionType.WRITE_FILE,
                target=target,
                reason="Kullanıcı dosya yazmak istiyor",
                content=content,
            )
        ],
        risk_level="low",
        risk_notes=[],
        permission_scope="Desktop",
        single_task_ok=True,
        forbidden_request_detected=False,
        requires_real_execution=requires_real_execution,
        clarification_needed=False,
        clarification_question=None,
    )
    lockbox = PlanLockbox()
    return lockbox.lock(plan), lockbox


def run_with_bypass(locked, lockbox, resolved_path):
    """_resolve_target + _is_in_zone + check_path_hardened patch'li çalıştırıcı."""
    from executor import Executor
    with patch("executor._resolve_target", return_value=resolved_path), \
         patch("executor._is_in_zone", return_value=True), \
         patch("executor.check_path_hardened", return_value=(None, None)):
        return Executor(lockbox=lockbox).run(locked)


def run_zone_denied(locked, lockbox, resolved_path):
    """_is_in_zone=False — zone reddi testi için."""
    from executor import Executor
    with patch("executor._resolve_target", return_value=resolved_path), \
         patch("executor._is_in_zone", return_value=False), \
         patch("executor.check_path_hardened", return_value=(None, None)):
        return Executor(lockbox=lockbox).run(locked)


def run_preflight_denied(locked, lockbox, resolved_path, deny_reason: str):
    """check_path_hardened hata döndüren çalıştırıcı."""
    from executor import Executor
    with patch("executor._resolve_target", return_value=resolved_path), \
         patch("executor._is_in_zone", return_value=True), \
         patch("executor.check_path_hardened", return_value=([deny_reason], [])):
        return Executor(lockbox=lockbox).run(locked)


# ---------------------------------------------------------------------------
# 1. Yeni dosya oluşturma
# ---------------------------------------------------------------------------

class TestWriteFileNewFile:

    def test_status_success(self, tmp_path):
        target = tmp_path / "yeni.txt"
        locked, lockbox = make_locked_plan(str(target), content="merhaba")
        result = run_with_bypass(locked, lockbox, target)
        assert result.status.value == "SUCCESS"

    def test_file_exists_on_disk(self, tmp_path):
        target = tmp_path / "yeni.txt"
        locked, lockbox = make_locked_plan(str(target), content="merhaba")
        run_with_bypass(locked, lockbox, target)
        assert target.exists()

    def test_file_content_on_disk(self, tmp_path):
        target = tmp_path / "yeni.txt"
        locked, lockbox = make_locked_plan(str(target), content="beklenen içerik")
        run_with_bypass(locked, lockbox, target)
        assert target.read_text(encoding="utf-8") == "beklenen içerik"

    def test_rollback_available_true(self, tmp_path):
        target = tmp_path / "yeni.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_with_bypass(locked, lockbox, target)
        assert result.step_results[0].rollback_available is True

    def test_rollback_action_is_delete(self, tmp_path):
        target = tmp_path / "yeni.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_with_bypass(locked, lockbox, target)
        meta = result.step_results[0].rollback_metadata
        assert meta is not None
        assert meta.get("rollback_action") == "delete_created_file"

    def test_rollback_metadata_file_existed_false(self, tmp_path):
        target = tmp_path / "yeni.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_with_bypass(locked, lockbox, target)
        meta = result.step_results[0].rollback_metadata
        assert meta.get("file_existed_before") is False

    def test_rollback_metadata_previous_content_none(self, tmp_path):
        target = tmp_path / "yeni.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_with_bypass(locked, lockbox, target)
        meta = result.step_results[0].rollback_metadata
        assert meta.get("previous_content") is None

    def test_rollback_metadata_resolved_path(self, tmp_path):
        target = tmp_path / "yeni.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_with_bypass(locked, lockbox, target)
        meta = result.step_results[0].rollback_metadata
        assert meta.get("resolved_path") == str(target)


# ---------------------------------------------------------------------------
# 2. Overwrite (mevcut dosyanın üstüne yazma)
# ---------------------------------------------------------------------------

class TestWriteFileOverwrite:

    def test_status_success(self, tmp_path):
        target = tmp_path / "var.txt"
        target.write_text("eski içerik", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target), content="yeni içerik")
        result = run_with_bypass(locked, lockbox, target)
        assert result.status.value == "SUCCESS"

    def test_file_content_overwritten_on_disk(self, tmp_path):
        target = tmp_path / "var.txt"
        target.write_text("eski içerik", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target), content="yeni içerik")
        run_with_bypass(locked, lockbox, target)
        assert target.read_text(encoding="utf-8") == "yeni içerik"

    def test_rollback_action_is_restore(self, tmp_path):
        target = tmp_path / "var.txt"
        target.write_text("eski içerik", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target), content="yeni içerik")
        result = run_with_bypass(locked, lockbox, target)
        meta = result.step_results[0].rollback_metadata
        assert meta is not None
        assert meta.get("rollback_action") == "restore_previous_content"

    def test_rollback_metadata_file_existed_true(self, tmp_path):
        target = tmp_path / "var.txt"
        target.write_text("eski içerik", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target), content="yeni içerik")
        result = run_with_bypass(locked, lockbox, target)
        meta = result.step_results[0].rollback_metadata
        assert meta.get("file_existed_before") is True

    def test_rollback_metadata_previous_content_correct(self, tmp_path):
        target = tmp_path / "var.txt"
        target.write_text("eski içerik", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target), content="yeni içerik")
        result = run_with_bypass(locked, lockbox, target)
        meta = result.step_results[0].rollback_metadata
        assert meta.get("previous_content") == "eski içerik"

    def test_rollback_available_true(self, tmp_path):
        target = tmp_path / "var.txt"
        target.write_text("eski içerik", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target), content="yeni içerik")
        result = run_with_bypass(locked, lockbox, target)
        assert result.step_results[0].rollback_available is True

    def test_backup_taken_before_write(self, tmp_path):
        """
        Backup write'tan önce alındığını kanıtlar:
        metadata'daki previous_content disk üzerindeki yeni içerikle eşleşmemeli.
        """
        target = tmp_path / "var.txt"
        target.write_text("orijinal", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target), content="üzerine yazıldı")
        result = run_with_bypass(locked, lockbox, target)
        meta = result.step_results[0].rollback_metadata
        disk_content = target.read_text(encoding="utf-8")
        assert meta.get("previous_content") != disk_content   # backup ≠ yeni içerik
        assert meta.get("previous_content") == "orijinal"


# ---------------------------------------------------------------------------
# 3. Boş içerik reddi
# ---------------------------------------------------------------------------

class TestWriteFileEmptyContent:

    def test_empty_string_fails(self, tmp_path):
        target = tmp_path / "bos.txt"
        locked, lockbox = make_locked_plan(str(target), content="")
        result = run_with_bypass(locked, lockbox, target)
        assert result.step_results[0].status.value == "FAILED"

    def test_whitespace_only_fails(self, tmp_path):
        target = tmp_path / "bos.txt"
        locked, lockbox = make_locked_plan(str(target), content="   \n  ")
        result = run_with_bypass(locked, lockbox, target)
        assert result.step_results[0].status.value == "FAILED"

    def test_empty_content_error_code(self, tmp_path):
        target = tmp_path / "bos.txt"
        locked, lockbox = make_locked_plan(str(target), content="")
        result = run_with_bypass(locked, lockbox, target)
        assert result.step_results[0].error == "CONTENT_MISSING"

    def test_empty_content_no_file_created(self, tmp_path):
        target = tmp_path / "bos.txt"
        locked, lockbox = make_locked_plan(str(target), content="")
        run_with_bypass(locked, lockbox, target)
        assert not target.exists()

    def test_empty_content_no_rollback(self, tmp_path):
        target = tmp_path / "bos.txt"
        locked, lockbox = make_locked_plan(str(target), content="")
        result = run_with_bypass(locked, lockbox, target)
        assert result.rolled_back is False


# ---------------------------------------------------------------------------
# 4. Klasör reddi
# ---------------------------------------------------------------------------

class TestWriteFileDirectoryTarget:

    def test_existing_directory_fails(self, tmp_path):
        locked, lockbox = make_locked_plan(str(tmp_path), content="içerik")
        result = run_with_bypass(locked, lockbox, tmp_path)
        assert result.step_results[0].status.value == "FAILED"

    def test_path_ending_with_txt_but_is_dir_fails(self, tmp_path):
        weird = tmp_path / "klasor.txt"
        weird.mkdir()
        locked, lockbox = make_locked_plan(str(weird), content="içerik")
        result = run_with_bypass(locked, lockbox, weird)
        assert result.step_results[0].status.value == "FAILED"

    def test_directory_target_error_set(self, tmp_path):
        locked, lockbox = make_locked_plan(str(tmp_path), content="içerik")
        result = run_with_bypass(locked, lockbox, tmp_path)
        assert result.step_results[0].error is not None

    def test_directory_target_no_rollback(self, tmp_path):
        locked, lockbox = make_locked_plan(str(tmp_path), content="içerik")
        result = run_with_bypass(locked, lockbox, tmp_path)
        assert result.rolled_back is False


# ---------------------------------------------------------------------------
# 5. Zone reddi — _is_in_zone bypass edilmez
# ---------------------------------------------------------------------------

class TestWriteFileZoneDenied:

    def test_zone_denied_fails(self, tmp_path):
        target = tmp_path / "gizli.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_zone_denied(locked, lockbox, target)
        assert result.step_results[0].status.value == "FAILED"

    def test_zone_denied_error_code(self, tmp_path):
        target = tmp_path / "gizli.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_zone_denied(locked, lockbox, target)
        assert result.step_results[0].error == "ZONE_DENIED"

    def test_zone_denied_no_file_created(self, tmp_path):
        target = tmp_path / "gizli.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        run_zone_denied(locked, lockbox, target)
        assert not target.exists()

    def test_zone_denied_no_rollback(self, tmp_path):
        target = tmp_path / "gizli.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_zone_denied(locked, lockbox, target)
        assert result.rolled_back is False


# ---------------------------------------------------------------------------
# 6. check_path_hardened — preflight write'tan önce çalışmalı
# ---------------------------------------------------------------------------

class TestWriteFilePreflightGuard:

    def test_preflight_deny_returns_skipped(self, tmp_path):
        target = tmp_path / "hedef.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_preflight_denied(
            locked, lockbox, target,
            deny_reason="[step 1] DENY_REASON=PATH_TRAVERSAL: '../hedef.txt'"
        )
        assert result.step_results[0].status.value == "SKIPPED"

    def test_preflight_deny_no_file_created(self, tmp_path):
        target = tmp_path / "hedef.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        run_preflight_denied(
            locked, lockbox, target,
            deny_reason="[step 1] DENY_REASON=UNC_PATH"
        )
        assert not target.exists()

    def test_preflight_deny_no_rollback(self, tmp_path):
        target = tmp_path / "hedef.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_preflight_denied(
            locked, lockbox, target,
            deny_reason="[step 1] DENY_REASON=OUTSIDE_ALLOWED_ZONES"
        )
        assert result.rolled_back is False

    def test_preflight_called_before_handler(self, tmp_path):
        """
        check_path_hardened handler'dan önce çağrılıyor mu?
        Mock ile çağrı sırasını doğrular.
        """
        target = tmp_path / "hedef.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")

        call_order = []

        def mock_preflight(*args, **kwargs):
            call_order.append("preflight")
            return (None, None)

        original_write = target.__class__.write_text

        def mock_write(self, *args, **kwargs):
            call_order.append("write")
            return original_write(self, *args, **kwargs)

        from executor import Executor
        with patch("executor._resolve_target", return_value=target), \
             patch("executor._is_in_zone", return_value=True), \
             patch("executor.check_path_hardened", side_effect=mock_preflight), \
             patch.object(target.__class__, "write_text", mock_write):
            Executor(lockbox=lockbox).run(locked)

        assert "preflight" in call_order
        assert "write" in call_order
        assert call_order.index("preflight") < call_order.index("write")


# ---------------------------------------------------------------------------
# 7. Rollback metadata bütünlüğü
# ---------------------------------------------------------------------------

class TestWriteFileRollbackMetadata:

    def test_new_file_metadata_all_keys_present(self, tmp_path):
        target = tmp_path / "yeni.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")
        result = run_with_bypass(locked, lockbox, target)
        meta = result.step_results[0].rollback_metadata
        assert meta is not None
        for key in ("rollback_action", "file_existed_before", "previous_content", "resolved_path"):
            assert key in meta, f"Eksik metadata anahtarı: {key}"

    def test_overwrite_metadata_all_keys_present(self, tmp_path):
        target = tmp_path / "var.txt"
        target.write_text("eski", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target), content="yeni")
        result = run_with_bypass(locked, lockbox, target)
        meta = result.step_results[0].rollback_metadata
        assert meta is not None
        for key in ("rollback_action", "file_existed_before", "previous_content", "resolved_path"):
            assert key in meta, f"Eksik metadata anahtarı: {key}"

    def test_rollback_manager_register_called_on_success(self, tmp_path):
        target = tmp_path / "yeni.txt"
        locked, lockbox = make_locked_plan(str(target), content="içerik")

        from executor import Executor
        with patch("executor._resolve_target", return_value=target), \
             patch("executor._is_in_zone", return_value=True), \
             patch("executor.check_path_hardened", return_value=(None, None)), \
             patch("executor.RollbackManager") as mock_rm:
            mock_rm.return_value.has_registered.return_value = False
            mock_rm.return_value.rollback_all.return_value = []
            Executor(lockbox=lockbox).run(locked)

        mock_rm.return_value.register.assert_called_once()

    def test_rollback_manager_not_called_on_failure(self, tmp_path):
        target = tmp_path / "bos.txt"
        locked, lockbox = make_locked_plan(str(target), content="")

        from executor import Executor
        with patch("executor._resolve_target", return_value=target), \
             patch("executor._is_in_zone", return_value=True), \
             patch("executor.check_path_hardened", return_value=(None, None)), \
             patch("executor.RollbackManager") as mock_rm:
            mock_rm.return_value.has_registered.return_value = False
            mock_rm.return_value.rollback_all.return_value = []
            Executor(lockbox=lockbox).run(locked)

        mock_rm.return_value.register.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Integration — bypass'sız akış
#
# Gerçek _resolve_target + _is_in_zone + check_path_hardened çalışır.
# requires_real_execution=False zorunlu: rule_engine MUTATING planı reddeder.
# tmp_path ALLOWED_WRITE_ZONES dışında → zone veya preflight reddi beklenir.
# Bu testler başarı senaryosu DEĞİL — güvenlik katmanının devre dışı
# bırakılmadığını kanıtlar.
# ---------------------------------------------------------------------------

class TestWriteFileIntegration:

    def test_integration_zone_denied_without_bypass(self, tmp_path):
        """
        Bypass yok. tmp_path izinli zone dışında → FAILED veya SKIPPED beklenir.
        """
        target = tmp_path / "entegrasyon.txt"
        locked, lockbox = make_locked_plan(
            str(target),
            content="entegrasyon içeriği",
            requires_real_execution=False,
        )
        from executor import Executor
        result = Executor(lockbox=lockbox).run(locked)
        assert result.step_results[0].status.value in ("FAILED", "SKIPPED")

    def test_integration_no_file_created_when_zone_denied(self, tmp_path):
        target = tmp_path / "entegrasyon.txt"
        locked, lockbox = make_locked_plan(
            str(target),
            content="entegrasyon içeriği",
            requires_real_execution=False,
        )
        from executor import Executor
        Executor(lockbox=lockbox).run(locked)
        assert not target.exists()

    def test_integration_preflight_traversal_denied(self, tmp_path):
        """
        Path traversal → check_path_hardened yakalamalı.
        Bypass yok; tam akış çalışır.
        """
        locked, lockbox = make_locked_plan(
            "../../../etc/passwd",
            content="zararlı içerik",
            requires_real_execution=False,
        )
        from executor import Executor
        result = Executor(lockbox=lockbox).run(locked)
        assert result.step_results[0].status.value in ("FAILED", "SKIPPED")

"""
test_executor_read_file.py — Executor READ_FILE handler testleri
Yerel Güvenli Ajan v2 projesi

Strateji:
    - _resolve_target patch'lenir → gerçek path resolution bypass
    - _is_in_zone patch'lenir     → zone kontrolü bypass
    - Sadece READ_FILE handler davranışı test edilir
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def make_locked_plan(target: str, step_no: int = 1):
    from models import AgentPlan, PlanStep, ActionType
    from lockbox import PlanLockbox

    plan = AgentPlan(
        goal="Dosya oku",
        summary="Belirtilen dosyanın içeriği okunur",
        steps=[
            PlanStep(
                step_no=step_no,
                action=ActionType.READ_FILE,
                target=target,
                reason="Kullanıcı dosyayı okumak istiyor",
                content=None,
            )
        ],
        risk_level="low",
        risk_notes=[],
        permission_scope="Desktop",
        single_task_ok=True,
        forbidden_request_detected=False,
        requires_real_execution=True,
        clarification_needed=False,
        clarification_question=None,
    )
    lockbox = PlanLockbox()
    return lockbox.lock(plan), lockbox


def run_with_bypass(locked, lockbox, resolved_path):
    from executor import Executor
    with patch("executor._resolve_target", return_value=resolved_path), \
         patch("executor._is_in_zone", return_value=True), \
         patch("executor.check_path_hardened", return_value=(None, None)):
        return Executor(lockbox=lockbox).run(locked)


class TestReadFileSuccess:

    def test_status_success(self, tmp_path):
        target = tmp_path / "test.txt"
        target.write_text("merhaba dünya", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target))
        result = run_with_bypass(locked, lockbox, target)
        assert result.status.value == "SUCCESS"

    def test_output_contains_content(self, tmp_path):
        target = tmp_path / "test.txt"
        target.write_text("merhaba dünya", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target))
        result = run_with_bypass(locked, lockbox, target)
        assert "merhaba dünya" in result.step_results[0].message

    def test_step_status_success(self, tmp_path):
        target = tmp_path / "test.txt"
        target.write_text("içerik", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target))
        result = run_with_bypass(locked, lockbox, target)
        assert result.step_results[0].status.value == "SUCCESS"


class TestReadFileMissing:

    def test_status_failure(self, tmp_path):
        target = tmp_path / "yok.txt"
        locked, lockbox = make_locked_plan(str(target))
        result = run_with_bypass(locked, lockbox, target)
        assert result.step_results[0].status.value in ("FAILURE", "FAILED")

    def test_overall_status_failure(self, tmp_path):
        target = tmp_path / "yok.txt"
        locked, lockbox = make_locked_plan(str(target))
        result = run_with_bypass(locked, lockbox, target)
        assert result.status.value in ("FAILURE", "FAILED")

    def test_no_rollback_on_missing(self, tmp_path):
        target = tmp_path / "yok.txt"
        locked, lockbox = make_locked_plan(str(target))
        result = run_with_bypass(locked, lockbox, target)
        assert result.rolled_back is False


class TestReadFileDirectory:

    def test_directory_returns_failure(self, tmp_path):
        locked, lockbox = make_locked_plan(str(tmp_path))
        result = run_with_bypass(locked, lockbox, tmp_path)
        assert result.step_results[0].status.value in ("FAILURE", "FAILED")

    def test_no_rollback_on_directory(self, tmp_path):
        target = tmp_path / "klasor.txt"
        target.mkdir()
        locked, lockbox = make_locked_plan(str(target))
        result = run_with_bypass(locked, lockbox, target)
        assert result.rolled_back is False


class TestReadFileTruncate:

    def test_large_file_truncated(self, tmp_path):
        target = tmp_path / "large.txt"
        target.write_text("A" * 100_000, encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target))
        result = run_with_bypass(locked, lockbox, target)
        assert result.step_results[0].status.value == "SUCCESS"
        assert len(result.step_results[0].message) < 100_000

    def test_truncation_marker_present(self, tmp_path):
        target = tmp_path / "large.txt"
        target.write_text("B" * 100_000, encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target))
        result = run_with_bypass(locked, lockbox, target)
        assert "[truncated]" in result.step_results[0].message


class TestReadFileNoRollback:

    def test_successful_read_no_rollback(self, tmp_path):
        target = tmp_path / "test.txt"
        target.write_text("içerik", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target))
        result = run_with_bypass(locked, lockbox, target)
        assert result.rolled_back is False

    def test_rollback_manager_not_called_for_read(self, tmp_path):
        target = tmp_path / "test.txt"
        target.write_text("içerik", encoding="utf-8")
        locked, lockbox = make_locked_plan(str(target))

        from executor import Executor
        with patch("executor._resolve_target", return_value=target), \
             patch("executor._is_in_zone", return_value=True), \
             patch("executor.check_path_hardened", return_value=(None, None)), \
             patch("executor.RollbackManager") as mock_rm:
            mock_rm.return_value.has_registered.return_value = False
            mock_rm.return_value.rollback_all.return_value = []
            result = Executor(lockbox=lockbox).run(locked)

        mock_rm.return_value.register.assert_not_called()

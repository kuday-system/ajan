# test_executor_rollback_integration.py v1.2
# Değişiklikler v1.1'e göre:
#   FIX — _patch_zones içine rollback_manager._ALLOWED_WRITE_ZONES patch'i eklendi.
#          Rollback sırasında manager tmp_path'i izinli zone olarak tanımalı.
#
# Kapsam:
#   - Executor.run() fail aldığında rollback_all tetikleniyor mu?
#   - ExecutionResult.rolled_back=True dönüyor mu?
#   - summary içinde rollback satırları var mı?
#   - Başarılı execution'da rolled_back=False mı?
#
# Çalıştırma: pytest test_executor_rollback_integration.py -v

import pytest
from pathlib import Path
from unittest.mock import patch

import rollback_manager
from models import AgentPlan, ActionType, PlanStep, LockedPlan
from executor import Executor
from lockbox import PlanLockbox
from executor_models import ExecutionStatus


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _make_plan(steps: list[dict]) -> AgentPlan:
    return AgentPlan(
        goal="Test",
        summary="Entegrasyon test planı",
        steps=[PlanStep(**s) for s in steps],
        risk_level="low",
        risk_notes=[],
        permission_scope="Desktop",
        single_task_ok=True,
        forbidden_request_detected=False,
        requires_real_execution=False,
        clarification_needed=False,
        clarification_question=None,
    )


def _lock_and_verify(plan: AgentPlan) -> tuple[PlanLockbox, "LockedPlan"]:
    """Plan kilitle, verify et, ikisini döndür."""
    lockbox = PlanLockbox()
    locked  = lockbox.lock(plan)
    assert lockbox.verify(locked) is True, "Lockbox verify başarısız — hash bozuk."
    return lockbox, locked


def _patch_zones(tmp_path: Path):
    """
    DESKTOP_DIR → tmp_path olarak patch edilir.
    rollback_manager._ALLOWED_WRITE_ZONES da aynı tmp_path ile patch edilir;
    böylece rollback sırasında manager zone doğrulamasını geçer.
    """
    return [
        patch("executor.DESKTOP_DIR",          tmp_path),
        patch("executor.DOCUMENTS_DIR",        tmp_path),
        patch("executor.DOWNLOADS_DIR",        tmp_path),
        patch("executor._USERPROFILE_DIR",     tmp_path),
        patch("executor._ALLOWED_WRITE_ZONES", {tmp_path}),
        patch("executor._ALLOWED_READ_ZONES",  {tmp_path}),
        patch("executor._SHORT_LABEL_MAP", {
            "desktop":   tmp_path,
            "documents": tmp_path,
            "downloads": tmp_path,
            "userhome":  tmp_path,
        }),
        patch("rule_engine.ALLOWED_USER_ZONES", [tmp_path]),
        patch("rule_engine.DESKTOP_DIR",        tmp_path),
        patch("rule_engine.DOCUMENTS_DIR",      tmp_path),
        patch("rule_engine.DOWNLOADS_DIR",      tmp_path),
        # FIX: rollback_manager zone'u da aynı tmp_path'e yönlendir
        patch.object(rollback_manager, "_ALLOWED_WRITE_ZONES", {tmp_path}),
    ]


# ---------------------------------------------------------------------------
# Senaryo A — CREATE_DIR başarı + WRITE_FILE EXTENSION_DENIED
# rolled_back=True, klasör silinmeli
# ---------------------------------------------------------------------------

class TestSenaryoA:
    def test_rolled_back_true(self, tmp_path):
        plan = _make_plan([
            {
                "step_no": 1,
                "action": ActionType.CREATE_DIR,
                "target": "desktop/Test",
                "reason": "Test klasörü oluştur",
            },
            {
                "step_no": 2,
                "action": ActionType.WRITE_FILE,
                "target": "desktop/Test/bad.exe",
                "reason": "İzinsiz uzantı — EXTENSION_DENIED bekleniyor",
                "content": "içerik",
            },
        ])

        patches = _patch_zones(tmp_path)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], patches[9], \
             patches[10], patches[11]:

            lockbox, locked = _lock_and_verify(plan)
            executor = Executor(lockbox=lockbox)
            result = executor.run(locked)

        assert result.status == ExecutionStatus.FAILED
        assert result.rolled_back is True

    def test_summary_rollback_satiri_icerir(self, tmp_path):
        plan = _make_plan([
            {
                "step_no": 1,
                "action": ActionType.CREATE_DIR,
                "target": "desktop/Test",
                "reason": "Test klasörü oluştur",
            },
            {
                "step_no": 2,
                "action": ActionType.WRITE_FILE,
                "target": "desktop/Test/bad.exe",
                "reason": "İzinsiz uzantı",
                "content": "içerik",
            },
        ])

        patches = _patch_zones(tmp_path)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], patches[9], \
             patches[10], patches[11]:

            lockbox, locked = _lock_and_verify(plan)
            executor = Executor(lockbox=lockbox)
            result = executor.run(locked)

        assert "[Rollback]" in result.summary

    def test_klasor_silindi(self, tmp_path):
        test_dir = tmp_path / "Test"

        plan = _make_plan([
            {
                "step_no": 1,
                "action": ActionType.CREATE_DIR,
                "target": "desktop/Test",
                "reason": "Test klasörü oluştur",
            },
            {
                "step_no": 2,
                "action": ActionType.WRITE_FILE,
                "target": "desktop/Test/bad.exe",
                "reason": "İzinsiz uzantı",
                "content": "içerik",
            },
        ])

        patches = _patch_zones(tmp_path)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], patches[9], \
             patches[10], patches[11]:

            lockbox, locked = _lock_and_verify(plan)
            executor = Executor(lockbox=lockbox)
            result = executor.run(locked)

        assert not test_dir.exists()


# ---------------------------------------------------------------------------
# Senaryo B — WRITE_FILE başarı + sonraki adım EXTENSION_DENIED
# rolled_back=True, dosya silinmeli
# ---------------------------------------------------------------------------

class TestSenaryoB:
    def test_dosya_silindi(self, tmp_path):
        good_file = tmp_path / "a.txt"

        plan = _make_plan([
            {
                "step_no": 1,
                "action": ActionType.WRITE_FILE,
                "target": "desktop/a.txt",
                "reason": "Geçerli dosya",
                "content": "MERHABA",
            },
            {
                "step_no": 2,
                "action": ActionType.WRITE_FILE,
                "target": "desktop/bad.exe",
                "reason": "İzinsiz uzantı",
                "content": "içerik",
            },
        ])

        patches = _patch_zones(tmp_path)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], patches[9], \
             patches[10], patches[11]:

            lockbox, locked = _lock_and_verify(plan)
            executor = Executor(lockbox=lockbox)
            result = executor.run(locked)

        assert result.rolled_back is True
        assert not good_file.exists()

    def test_rolled_back_true(self, tmp_path):
        plan = _make_plan([
            {
                "step_no": 1,
                "action": ActionType.WRITE_FILE,
                "target": "desktop/a.txt",
                "reason": "Geçerli dosya",
                "content": "MERHABA",
            },
            {
                "step_no": 2,
                "action": ActionType.WRITE_FILE,
                "target": "desktop/bad.exe",
                "reason": "İzinsiz uzantı",
                "content": "içerik",
            },
        ])

        patches = _patch_zones(tmp_path)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], patches[9], \
             patches[10], patches[11]:

            lockbox, locked = _lock_and_verify(plan)
            executor = Executor(lockbox=lockbox)
            result = executor.run(locked)

        assert result.rolled_back is True


# ---------------------------------------------------------------------------
# Senaryo C — LIST_DIR başarı + sonraki adım fail
# rolled_back=False (LIST_DIR register edilmez)
# ---------------------------------------------------------------------------

class TestSenaryoC:
    def test_rolled_back_false(self, tmp_path):
        plan = _make_plan([
            {
                "step_no": 1,
                "action": ActionType.LIST_DIR,
                "target": "desktop",
                "reason": "Listele",
            },
            {
                "step_no": 2,
                "action": ActionType.WRITE_FILE,
                "target": "desktop/bad.exe",
                "reason": "İzinsiz uzantı",
                "content": "içerik",
            },
        ])

        patches = _patch_zones(tmp_path)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], patches[9], \
             patches[10], patches[11]:

            lockbox, locked = _lock_and_verify(plan)
            executor = Executor(lockbox=lockbox)
            result = executor.run(locked)

        assert result.rolled_back is False
        assert "[Rollback]" not in result.summary


# ---------------------------------------------------------------------------
# Senaryo D — Tüm adımlar başarılı
# rolled_back=False, dosyalar fiziksel olarak duruyor
# ---------------------------------------------------------------------------

class TestSenaryoD:
    def test_basarili_execution(self, tmp_path):
        test_dir  = tmp_path / "Test"
        test_file = test_dir / "a.txt"

        plan = _make_plan([
            {
                "step_no": 1,
                "action": ActionType.CREATE_DIR,
                "target": "desktop/Test",
                "reason": "Klasör oluştur",
            },
            {
                "step_no": 2,
                "action": ActionType.WRITE_FILE,
                "target": "desktop/Test/a.txt",
                "reason": "Dosya yaz",
                "content": "MERHABA",
            },
        ])

        patches = _patch_zones(tmp_path)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7], patches[8], patches[9], \
             patches[10], patches[11]:

            lockbox, locked = _lock_and_verify(plan)
            executor = Executor(lockbox=lockbox)
            result = executor.run(locked)

        assert result.status == ExecutionStatus.SUCCESS
        assert result.rolled_back is False
        assert test_dir.exists()
        assert test_file.exists()
        assert test_file.read_text(encoding="utf-8") == "MERHABA"

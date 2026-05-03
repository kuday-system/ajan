"""
test_executor_open_url.py v1.0
OPEN_URL handler + scope guard testleri

Senaryolar:
  1.  https:// → SUCCESS
  2.  http://  → SUCCESS
  3.  file://  → SCHEME_DENIED
  4.  javascript: → SCHEME_DENIED
  5.  ftp://   → SCHEME_DENIED
  6.  boş URL  → ValidationError (PlanStep.target min_length=1, executor'a ulaşmaz)
  7.  malformed (scheme yok) → MALFORMED_URL
  8.  malformed (netloc yok) → MALFORMED_URL
  9.  webbrowser.open mock çağrılıyor mu
  10. webbrowser.open False → BROWSER_OPEN_FAILED
  11. rollback_available=False
  12. rollback_metadata opened_url içeriyor
  13. check_path_hardened OPEN_URL için ([], []) döner (path validation yok)
  14. scope != Internet → SCOPE_REQUIRED_INTERNET (executor guard)
  15. FILE action + Internet scope → SCOPE_MISMATCH_FILE_INTERNET (executor guard)
  16. scope = Internet → guard geçer
"""

import pytest
from unittest.mock import patch
from pydantic import ValidationError
from models import AgentPlan, PlanStep, ActionType
from lockbox import PlanLockbox
from executor import Executor
from executor_models import ExecutionStatus
from rule_engine import check_path_hardened


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def make_locked_plan(url: str, scope: str = "Internet", step_no: int = 1):
    plan = AgentPlan(
        goal="URL aç",
        summary="Tarayıcıda URL açılır",
        steps=[PlanStep(
            step_no=step_no,
            action=ActionType.OPEN_URL,
            target=url,
            reason="Kullanıcı URL açmak istiyor",
            content=None,
        )],
        risk_level="low",
        risk_notes=[],
        permission_scope=scope,
        single_task_ok=True,
        forbidden_request_detected=False,
        requires_real_execution=False,
        clarification_needed=False,
        clarification_question=None,
    )
    lockbox = PlanLockbox()
    return lockbox.lock(plan), lockbox


def make_locked_file_plan(scope: str = "Internet"):
    """FILE action + verilen scope → scope guard testi için."""
    plan = AgentPlan(
        goal="Dosya oku",
        summary="Dosya okunur",
        steps=[PlanStep(
            step_no=1,
            action=ActionType.LIST_DIR,
            target="Desktop",
            reason="Test",
            content=None,
        )],
        risk_level="low",
        risk_notes=[],
        permission_scope=scope,
        single_task_ok=True,
        forbidden_request_detected=False,
        requires_real_execution=False,
        clarification_needed=False,
        clarification_question=None,
    )
    lockbox = PlanLockbox()
    return lockbox.lock(plan), lockbox


def run_open_url(url: str, scope: str = "Internet", browser_return: bool = True):
    locked, lockbox = make_locked_plan(url, scope=scope)
    with patch("executor.webbrowser.open", return_value=browser_return):
        return Executor(lockbox=lockbox).run(locked)


# ---------------------------------------------------------------------------
# 1-2. Geçerli scheme'ler
# ---------------------------------------------------------------------------

class TestValidSchemes:

    def test_https_success(self):
        result = run_open_url("https://www.youtube.com")
        assert result.status == ExecutionStatus.SUCCESS

    def test_http_success(self):
        result = run_open_url("http://example.com")
        assert result.status == ExecutionStatus.SUCCESS

    def test_https_step_status_success(self):
        result = run_open_url("https://www.google.com")
        assert result.step_results[0].status == ExecutionStatus.SUCCESS


# ---------------------------------------------------------------------------
# 3-5. Yasak scheme'ler
# ---------------------------------------------------------------------------

class TestBlockedSchemes:

    def test_file_scheme_denied(self):
        result = run_open_url("file:///C:/Windows/System32/cmd.exe")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SCHEME_DENIED"

    def test_javascript_scheme_denied(self):
        result = run_open_url("javascript:alert(1)")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SCHEME_DENIED"

    def test_ftp_scheme_denied(self):
        result = run_open_url("ftp://files.example.com")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SCHEME_DENIED"

    def test_blocked_scheme_no_browser_call(self):
        locked, lockbox = make_locked_plan("file:///etc/passwd")
        with patch("executor.webbrowser.open") as mock_wb:
            Executor(lockbox=lockbox).run(locked)
        mock_wb.assert_not_called()


# ---------------------------------------------------------------------------
# 6-8. Geçersiz URL formatları
# ---------------------------------------------------------------------------

class TestInvalidUrls:

    def test_empty_url_fails(self):
        # Boş URL PlanStep.target validation'ında (min_length=1) reddedilir;
        # executor'a hiç ulaşmaz → ValidationError beklenir.
        with pytest.raises(ValidationError):
            make_locked_plan("")

    def test_no_scheme_malformed(self):
        result = run_open_url("www.google.com")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "MALFORMED_URL"

    def test_no_netloc_malformed(self):
        result = run_open_url("https://")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "MALFORMED_URL"


# ---------------------------------------------------------------------------
# 9. webbrowser.open mock
# ---------------------------------------------------------------------------

class TestWebbrowserMock:

    def test_webbrowser_open_called(self):
        locked, lockbox = make_locked_plan("https://www.youtube.com")
        with patch("executor.webbrowser.open", return_value=True) as mock_wb:
            Executor(lockbox=lockbox).run(locked)
        mock_wb.assert_called_once_with("https://www.youtube.com")

    def test_webbrowser_open_called_with_exact_url(self):
        url = "https://www.google.com/search?q=test"
        locked, lockbox = make_locked_plan(url)
        with patch("executor.webbrowser.open", return_value=True) as mock_wb:
            Executor(lockbox=lockbox).run(locked)
        mock_wb.assert_called_once_with(url)


# ---------------------------------------------------------------------------
# 10. webbrowser.open False
# ---------------------------------------------------------------------------

class TestWebbrowserFalse:

    def test_browser_open_false_fails(self):
        result = run_open_url("https://example.com", browser_return=False)
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "BROWSER_OPEN_FAILED"

    def test_browser_open_false_overall_failed(self):
        result = run_open_url("https://example.com", browser_return=False)
        assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# 11. rollback_available=False
# ---------------------------------------------------------------------------

class TestRollback:

    def test_rollback_available_false(self):
        result = run_open_url("https://example.com")
        assert result.step_results[0].rollback_available is False

    def test_rolled_back_false(self):
        result = run_open_url("https://example.com")
        assert result.rolled_back is False


# ---------------------------------------------------------------------------
# 12. rollback_metadata opened_url
# ---------------------------------------------------------------------------

class TestRollbackMetadata:

    def test_metadata_contains_opened_url(self):
        url = "https://www.youtube.com"
        result = run_open_url(url)
        meta = result.step_results[0].rollback_metadata
        assert meta is not None
        assert meta.get("opened_url") == url

    def test_metadata_contains_note(self):
        result = run_open_url("https://example.com")
        meta = result.step_results[0].rollback_metadata
        assert "note" in meta


# ---------------------------------------------------------------------------
# 13. check_path_hardened OPEN_URL için path validation yapmıyor
# ---------------------------------------------------------------------------

class TestPathHardenedSkip:

    def test_open_url_returns_empty_hard(self):
        hard, warnings = check_path_hardened(
            "https://www.youtube.com", 1, ActionType.OPEN_URL
        )
        assert hard == []

    def test_open_url_file_scheme_no_path_violation(self):
        """file:// URL path violation olarak değil, scheme violation olarak yakalanmalı."""
        hard, warnings = check_path_hardened(
            "file:///C:/Windows/System32", 1, ActionType.OPEN_URL
        )
        assert hard == []

    def test_open_url_traversal_string_no_path_violation(self):
        """../../etc/passwd gibi string URL'de path traversal değil scheme hatası beklenir."""
        hard, warnings = check_path_hardened(
            "../../etc/passwd", 1, ActionType.OPEN_URL
        )
        assert hard == []


# ---------------------------------------------------------------------------
# 14. Scope guard — OPEN_URL + scope != Internet
# ---------------------------------------------------------------------------

class TestScopeGuardInternetRequired:

    def test_desktop_scope_open_url_fails(self):
        result = run_open_url("https://example.com", scope="Desktop")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SCOPE_REQUIRED_INTERNET"

    def test_documents_scope_open_url_fails(self):
        result = run_open_url("https://example.com", scope="Documents")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SCOPE_REQUIRED_INTERNET"

    def test_user_scope_open_url_fails(self):
        result = run_open_url("https://example.com", scope="User")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SCOPE_REQUIRED_INTERNET"

    def test_wrong_scope_no_browser_call(self):
        locked, lockbox = make_locked_plan("https://example.com", scope="Desktop")
        with patch("executor.webbrowser.open") as mock_wb:
            Executor(lockbox=lockbox).run(locked)
        mock_wb.assert_not_called()

    def test_wrong_scope_overall_failed(self):
        result = run_open_url("https://example.com", scope="Desktop")
        assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# 15. Scope guard — FILE action + Internet scope
# ---------------------------------------------------------------------------

class TestScopeGuardFileInternetMismatch:

    def test_list_dir_internet_scope_fails(self):
        locked, lockbox = make_locked_file_plan(scope="Internet")
        with patch("executor.webbrowser.open", return_value=True):
            result = Executor(lockbox=lockbox).run(locked)
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SCOPE_MISMATCH_FILE_INTERNET"

    def test_list_dir_internet_scope_overall_failed(self):
        locked, lockbox = make_locked_file_plan(scope="Internet")
        result = Executor(lockbox=lockbox).run(locked)
        assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# 16. Scope guard — Internet scope geçer
# ---------------------------------------------------------------------------

class TestScopeGuardPass:

    def test_internet_scope_open_url_passes_guard(self):
        result = run_open_url("https://example.com", scope="Internet")
        assert result.step_results[0].error != "SCOPE_REQUIRED_INTERNET"

    def test_internet_scope_success(self):
        result = run_open_url("https://example.com", scope="Internet")
        assert result.status == ExecutionStatus.SUCCESS

"""
test_executor_web_search.py v1.2
WEB_SEARCH handler + scope guard testleri

Değişiklikler v1.1'e göre:
  - test_whitespace_query_fails      → ValidationError bekleyecek şekilde düzeltildi
  - test_mixed_whitespace_query_fails → ValidationError bekleyecek şekilde düzeltildi
  PlanStep.normalize() strip sonrası boş target'ı executor'a ulaşmadan reddeder.

Senaryolar:
  1.  normal query → SUCCESS
  2.  Türkçe query URL encode edilir
  3.  boş query → ValidationError (PlanStep.target min_length=1, executor'a ulaşmaz)
  4.  whitespace-only query → ValidationError (PlanStep.normalize strip → boş)
  4b. mixed whitespace (\n\t) → ValidationError (PlanStep.normalize strip → boş)
  5.  uzun query → QUERY_TOO_LONG
  6.  password= içeren query → SENSITIVE_QUERY_BLOCKED
  7.  api_key içeren query → SENSITIVE_QUERY_BLOCKED
  8.  C:\\Users\\ içeren query → SENSITIVE_QUERY_BLOCKED
  8b. secret=abc123 → SENSITIVE_QUERY_BLOCKED
  8c. token=xyz → SENSITIVE_QUERY_BLOCKED
  8d. "secret nedir" → SUCCESS  (daraltılmış pattern)
  8e. "token nasıl çalışır" → SUCCESS  (daraltılmış pattern)
  9.  webbrowser.open doğru search_url ile çağrılır
  10. webbrowser.open False → BROWSER_OPEN_FAILED
  11. scope Desktop → SCOPE_REQUIRED_INTERNET
  12. scope Internet → SUCCESS
  13. check_path_hardened WEB_SEARCH için [] döner
  14. rollback_available=False
  15. rollback_capability NONE
  16. metadata query ve search_url içerir
"""

import pytest
from unittest.mock import patch
from urllib.parse import quote_plus
from pydantic import ValidationError

from models import AgentPlan, PlanStep, ActionType
from lockbox import PlanLockbox
from executor import Executor
from executor_models import ExecutionStatus, RollbackCapability
from rule_engine import check_path_hardened


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def make_locked_plan(query: str, scope: str = "Internet", step_no: int = 1):
    plan = AgentPlan(
        goal="Arama yap",
        summary="Tarayıcıda arama yapılır",
        steps=[PlanStep(
            step_no=step_no,
            action=ActionType.WEB_SEARCH,
            target=query,
            reason="Kullanıcı arama yapmak istiyor",
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


def run_web_search(query: str, scope: str = "Internet", browser_return: bool = True):
    locked, lockbox = make_locked_plan(query, scope=scope)
    with patch("executor.webbrowser.open", return_value=browser_return):
        return Executor(lockbox=lockbox).run(locked)


_BASE_URL = "https://www.google.com/search?q="


# ---------------------------------------------------------------------------
# 1. Normal query
# ---------------------------------------------------------------------------

class TestNormalQuery:

    def test_success(self):
        result = run_web_search("lofi müzik")
        assert result.status == ExecutionStatus.SUCCESS

    def test_step_status_success(self):
        result = run_web_search("python tutorial")
        assert result.step_results[0].status == ExecutionStatus.SUCCESS


# ---------------------------------------------------------------------------
# 2. Türkçe query encode
# ---------------------------------------------------------------------------

class TestQueryEncoding:

    def test_turkish_query_encoded_in_url(self):
        query = "istanbul hava durumu"
        expected_url = _BASE_URL + quote_plus(query)
        locked, lockbox = make_locked_plan(query)
        with patch("executor.webbrowser.open", return_value=True) as mock_wb:
            Executor(lockbox=lockbox).run(locked)
        mock_wb.assert_called_once_with(expected_url)

    def test_special_chars_encoded(self):
        query = "C++ nedir?"
        expected_url = _BASE_URL + quote_plus(query)
        locked, lockbox = make_locked_plan(query)
        with patch("executor.webbrowser.open", return_value=True) as mock_wb:
            Executor(lockbox=lockbox).run(locked)
        mock_wb.assert_called_once_with(expected_url)


# ---------------------------------------------------------------------------
# 3. Boş query → ValidationError (PlanStep katmanında)
# ---------------------------------------------------------------------------

class TestEmptyQuery:

    def test_empty_query_validation_error(self):
        # PlanStep.target min_length=1 → executor'a ulaşmaz
        with pytest.raises(ValidationError):
            make_locked_plan("")


# ---------------------------------------------------------------------------
# 4. Whitespace-only query → ValidationError (PlanStep katmanında)
# PlanStep.normalize() strip sonrası boş string kaldığı için
# ValidationError fırlatır; executor'a hiç ulaşmaz.
# ---------------------------------------------------------------------------

class TestWhitespaceQuery:

    def test_whitespace_only_validation_error(self):
        with pytest.raises(ValidationError):
            make_locked_plan("   ")

    def test_mixed_whitespace_validation_error(self):
        with pytest.raises(ValidationError):
            make_locked_plan("\n\t   ")


# ---------------------------------------------------------------------------
# 5. Uzun query → QUERY_TOO_LONG
# ---------------------------------------------------------------------------

class TestLongQuery:

    def test_301_char_query_fails(self):
        result = run_web_search("a" * 301)
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "QUERY_TOO_LONG"

    def test_300_char_query_succeeds(self):
        result = run_web_search("a" * 300)
        assert result.step_results[0].status == ExecutionStatus.SUCCESS


# ---------------------------------------------------------------------------
# 6-8. Hassas pattern → SENSITIVE_QUERY_BLOCKED
# ---------------------------------------------------------------------------

class TestSensitiveQuery:

    def test_password_pattern_blocked(self):
        result = run_web_search("password=123456")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SENSITIVE_QUERY_BLOCKED"

    def test_api_key_pattern_blocked(self):
        result = run_web_search("api_key bul")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SENSITIVE_QUERY_BLOCKED"

    def test_c_users_pattern_blocked(self):
        result = run_web_search(r"C:\Users\ dosyaları")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SENSITIVE_QUERY_BLOCKED"

    def test_dotenv_pattern_blocked(self):
        result = run_web_search(".env dosyası nedir")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SENSITIVE_QUERY_BLOCKED"

    # --- daraltılmış pattern: secret\s*= ---

    def test_secret_eq_blocked(self):
        # "secret=" içerdiği için bloklanmalı
        result = run_web_search("secret=abc123")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SENSITIVE_QUERY_BLOCKED"

    def test_secret_word_only_passes(self):
        # "secret" kelimesi tek başına artık bloklanmaz
        result = run_web_search("secret nedir")
        assert result.step_results[0].status == ExecutionStatus.SUCCESS

    # --- daraltılmış pattern: token\s*= ---

    def test_token_eq_blocked(self):
        # "token=" içerdiği için bloklanmalı
        result = run_web_search("token=xyz")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SENSITIVE_QUERY_BLOCKED"

    def test_token_word_only_passes(self):
        # "token" kelimesi tek başına artık bloklanmaz
        result = run_web_search("token nasıl çalışır")
        assert result.step_results[0].status == ExecutionStatus.SUCCESS


# ---------------------------------------------------------------------------
# 9. webbrowser.open doğru URL ile çağrılır
# ---------------------------------------------------------------------------

class TestWebbrowserCall:

    def test_called_with_correct_search_url(self):
        query = "python asyncio"
        expected_url = _BASE_URL + quote_plus(query)
        locked, lockbox = make_locked_plan(query)
        with patch("executor.webbrowser.open", return_value=True) as mock_wb:
            Executor(lockbox=lockbox).run(locked)
        mock_wb.assert_called_once_with(expected_url)


# ---------------------------------------------------------------------------
# 10. webbrowser.open False → BROWSER_OPEN_FAILED
# ---------------------------------------------------------------------------

class TestWebbrowserFalse:

    def test_browser_false_fails(self):
        result = run_web_search("python", browser_return=False)
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "BROWSER_OPEN_FAILED"

    def test_browser_false_overall_failed(self):
        result = run_web_search("python", browser_return=False)
        assert result.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# 11. Scope guard
# ---------------------------------------------------------------------------

class TestScopeGuard:

    def test_desktop_scope_fails(self):
        result = run_web_search("python", scope="Desktop")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SCOPE_REQUIRED_INTERNET"

    def test_documents_scope_fails(self):
        result = run_web_search("python", scope="Documents")
        assert result.step_results[0].status == ExecutionStatus.FAILED
        assert result.step_results[0].error == "SCOPE_REQUIRED_INTERNET"

    def test_wrong_scope_no_browser_call(self):
        locked, lockbox = make_locked_plan("python", scope="Desktop")
        with patch("executor.webbrowser.open") as mock_wb:
            Executor(lockbox=lockbox).run(locked)
        mock_wb.assert_not_called()


# ---------------------------------------------------------------------------
# 12. scope Internet → SUCCESS
# ---------------------------------------------------------------------------

class TestScopeInternet:

    def test_internet_scope_success(self):
        result = run_web_search("python", scope="Internet")
        assert result.status == ExecutionStatus.SUCCESS


# ---------------------------------------------------------------------------
# 13. check_path_hardened WEB_SEARCH için [] döner
# ---------------------------------------------------------------------------

class TestPathHardenedSkip:

    def test_web_search_returns_empty_hard(self):
        hard, warnings = check_path_hardened("lofi müzik", 1, ActionType.WEB_SEARCH)
        assert hard == []

    def test_web_search_returns_empty_warnings(self):
        hard, warnings = check_path_hardened("lofi müzik", 1, ActionType.WEB_SEARCH)
        assert warnings == []


# ---------------------------------------------------------------------------
# 14-15. rollback
# ---------------------------------------------------------------------------

class TestRollback:

    def test_rollback_available_false(self):
        result = run_web_search("python")
        assert result.step_results[0].rollback_available is False

    def test_rollback_capability_none(self):
        result = run_web_search("python")
        assert result.step_results[0].rollback_capability == RollbackCapability.NONE

    def test_rolled_back_false(self):
        result = run_web_search("python")
        assert result.rolled_back is False


# ---------------------------------------------------------------------------
# 16. metadata
# ---------------------------------------------------------------------------

class TestMetadata:

    def test_metadata_contains_query(self):
        query = "lofi müzik"
        result = run_web_search(query)
        meta = result.step_results[0].rollback_metadata
        assert meta is not None
        assert meta.get("query") == query

    def test_metadata_contains_search_url(self):
        query = "lofi müzik"
        result = run_web_search(query)
        meta = result.step_results[0].rollback_metadata
        expected_url = _BASE_URL + quote_plus(query)
        assert meta.get("search_url") == expected_url

    def test_metadata_contains_note(self):
        result = run_web_search("python")
        meta = result.step_results[0].rollback_metadata
        assert "note" in meta

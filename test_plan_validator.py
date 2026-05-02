"""
test_plan_validator.py — PlanValidator test senaryoları
Yerel Güvenli Ajan v1.4 projesi

Çalıştır:
    pytest test_plan_validator.py -v

Bağımlılık notu:
    config.MAX_STEPS mock'lanır — gerçek config gerekmez.
    AgentPlan mock'u duck-type ile test edilir (model_dump).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# config mock — gerçek config olmadan çalışsın
# ---------------------------------------------------------------------------
_config_mock = types.ModuleType("config")
_config_mock.MAX_STEPS = 5
sys.modules.setdefault("config", _config_mock)

from plan_validator import PlanValidator, ValidationStatus  # noqa: E402

validator = PlanValidator()


# ---------------------------------------------------------------------------
# Yardımcı
# ---------------------------------------------------------------------------

def make_step(step_no=1, action="READ_FILE", target="notes/todo.txt", **kwargs):
    return {"step_no": step_no, "action": action, "target": target, **kwargs}


def make_plan(*steps):
    return {"steps": list(steps)}


# ===========================================================================
# 1. VALID senaryolar
# ===========================================================================

class TestValid:

    def test_tek_adim_temiz(self):
        plan = make_plan(make_step())
        r = validator.validate(plan)
        assert r.status == ValidationStatus.VALID
        assert r.reasons == []

    def test_cok_adim_temiz(self):
        plan = make_plan(
            make_step(1, "READ_FILE",   "data/input.csv"),
            make_step(2, "WRITE_FILE",  "data/output.csv", content="çıktı içeriği"),
            make_step(3, "DELETE_FILE", "data/tmp.csv"),
        )
        r = validator.validate(plan)
        assert r.status == ValidationStatus.VALID

    def test_step_no_sirali_degil_sadece_warning(self):
        """Sırasız step_no yapısal olarak sadece warning üretir.
        Ancak target 'a.txt' doğal dil pattern'ına (\ba\b) takılabileceğinden
        sistem conservative davranır → ASK_CLARIFICATION beklenir.
        """
        plan = make_plan(
            make_step(2, "READ_FILE",  "a.txt"),
            make_step(1, "WRITE_FILE", "b.txt", content="içerik"),
        )
        r = validator.validate(plan)
        assert r.status == ValidationStatus.ASK_CLARIFICATION
        assert any("sıralı değil" in w for w in r.warnings)

    def test_step_no_sirali_degil_temiz_target_valid(self):
        """Sırasız step_no + doğal dil pattern tetiklemeyen target → VALID + warning."""
        plan = make_plan(
            make_step(2, "READ_FILE",  "input.csv"),
            make_step(1, "WRITE_FILE", "output.csv", content="içerik"),
        )
        r = validator.validate(plan)
        assert r.status == ValidationStatus.VALID
        assert any("sıralı değil" in w for w in r.warnings)

    def test_model_dump_duck_type(self):
        """AgentPlan mock'u — model_dump() destekli obje."""
        mock_plan = MagicMock()
        mock_plan.model_dump.return_value = make_plan(make_step())
        r = validator.validate(mock_plan)
        assert r.status == ValidationStatus.VALID
        mock_plan.model_dump.assert_called_once()

    def test_dict_girdi(self):
        """Saf dict girdi — model_dump() çağrılmamalı."""
        plan = make_plan(make_step())
        r = validator.validate(plan)
        assert r.status == ValidationStatus.VALID


# ===========================================================================
# 2. INVALID — Yapısal hatalar
# ===========================================================================

class TestInvalidStructural:

    def test_plan_dict_degil(self):
        r = validator.validate("bu bir string")
        assert r.status == ValidationStatus.INVALID
        assert any("dict değil" in reason or "model_dump" in reason for reason in r.reasons)

    def test_steps_eksik(self):
        r = validator.validate({})
        assert r.status == ValidationStatus.INVALID
        assert any("steps" in reason for reason in r.reasons)

    def test_steps_bos_liste(self):
        r = validator.validate({"steps": []})
        assert r.status == ValidationStatus.INVALID
        assert any("steps" in reason for reason in r.reasons)

    def test_steps_liste_degil(self):
        r = validator.validate({"steps": "read file"})
        assert r.status == ValidationStatus.INVALID

    def test_zorunlu_alan_eksik_action(self):
        step = {"step_no": 1, "target": "file.txt"}   # action yok
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID
        assert any("action" in reason for reason in r.reasons)

    def test_zorunlu_alan_eksik_target(self):
        step = {"step_no": 1, "action": "READ_FILE"}        # target yok
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID
        assert any("target" in reason for reason in r.reasons)

    def test_zorunlu_alan_eksik_step_no(self):
        step = {"action": "READ_FILE", "target": "file.txt"}
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID
        assert any("step_no" in reason for reason in r.reasons)

    def test_step_no_int_olmayan(self):
        step = make_step(step_no="bir")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID
        assert any("int" in reason for reason in r.reasons)

    def test_target_str_olmayan(self):
        step = make_step(target=42)
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID
        assert any("str" in reason for reason in r.reasons)

    def test_step_no_duplicate(self):
        plan = make_plan(
            make_step(1, "READ_FILE",  "a.txt"),
            make_step(1, "WRITE_FILE", "b.txt", content="içerik"),   # aynı step_no
        )
        r = validator.validate(plan)
        assert r.status == ValidationStatus.INVALID
        assert any("unique" in reason for reason in r.reasons)

    def test_max_steps_asimi(self):
        steps = [make_step(i, "READ_FILE", f"file{i}.txt") for i in range(1, 8)]  # 7 adım > MAX_STEPS=5
        r = validator.validate({"steps": steps})
        assert r.status == ValidationStatus.INVALID
        assert any("limit" in reason for reason in r.reasons)

    def test_step_dict_degil(self):
        r = validator.validate({"steps": ["bu string bir step değil"]})
        assert r.status == ValidationStatus.INVALID

    def test_model_dump_yok_dict_degil(self):
        """Ne dict ne model_dump — INVALID dönmeli."""
        r = validator.validate(12345)
        assert r.status == ValidationStatus.INVALID
        assert any("model_dump" in reason for reason in r.reasons)


# ===========================================================================
# 3. INVALID — Güvenlik pattern
# ===========================================================================

class TestInvalidSecurityPattern:

    def test_windows_path_buyuk_harf(self):
        step = make_step(target=r"C:\Users\admin\secret.txt")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID
        assert any("Windows path" in reason for reason in r.reasons)

    def test_windows_path_kucuk_harf(self):
        step = make_step(target=r"c:\windows\system32\cmd.exe")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID

    def test_env_var_unix(self):
        step = make_step(target="$HOME/documents/file.txt")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID
        assert any("Environment variable" in reason for reason in r.reasons)

    def test_env_var_windows(self):
        step = make_step(target="%APPDATA%\\config.ini")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID

    def test_target_bos_string(self):
        step = make_step(target="")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID
        assert any("boş" in reason for reason in r.reasons)

    def test_target_sadece_bosluk(self):
        step = make_step(target="   ")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID


# ===========================================================================
# 4. ASK_CLARIFICATION senaryolar
# ===========================================================================

class TestAskClarification:

    def test_dogal_dil_target_the(self):
        step = make_step(target="the config file")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.ASK_CLARIFICATION
        assert any("doğal dil" in reason for reason in r.reasons)

    def test_dogal_dil_target_this(self):
        step = make_step(target="this document")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.ASK_CLARIFICATION

    def test_multi_target_virgul(self):
        step = make_step(target="file1.txt, file2.txt")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.ASK_CLARIFICATION
        assert any("birden fazla" in reason for reason in r.reasons)

    def test_multi_target_and(self):
        step = make_step(target="notes.txt and backup.txt")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.ASK_CLARIFICATION

    def test_multi_target_ve(self):
        step = make_step(target="rapor.txt ve yedek.txt")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.ASK_CLARIFICATION

    def test_multi_target_semicolon(self):
        step = make_step(target="a.txt; b.txt")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.ASK_CLARIFICATION

    def test_write_file_content_yok(self):
        """WRITE_FILE content boşsa ASK_CLARIFICATION dönmeli."""
        step = make_step(action="WRITE_FILE", target="Desktop\\not.txt")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.ASK_CLARIFICATION
        assert any("content" in reason for reason in r.reasons)

    def test_append_file_content_yok(self):
        """APPEND_FILE content boşsa ASK_CLARIFICATION dönmeli."""
        step = make_step(action="APPEND_FILE", target="Desktop\\not.txt")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.ASK_CLARIFICATION
        assert any("content" in reason for reason in r.reasons)


# ===========================================================================
# 5. Öncelik sırası — INVALID her zaman ASK_CLARIFICATION'dan önce gelir
# ===========================================================================

class TestOncelikSirasi:

    def test_hem_invalid_hem_clarification_invalid_kazanir(self):
        """
        Windows path (→ INVALID) + doğal dil target (→ ASK) aynı anda.
        Beklenti: INVALID döner.
        """
        step = make_step(target=r"C:\the documents\file.txt")
        r = validator.validate(make_plan(step))
        assert r.status == ValidationStatus.INVALID

    def test_yapisal_hata_varsa_semantige_bakilmaz(self):
        """
        step_no duplicate (→ INVALID yapısal) + multi-target (→ ASK).
        Beklenti: INVALID döner, ASK üretilmez.
        """
        plan = make_plan(
            make_step(1, "READ_FILE",  "a.txt and b.txt"),
            make_step(1, "WRITE_FILE", "c.txt", content="içerik"),
        )
        r = validator.validate(plan)
        assert r.status == ValidationStatus.INVALID


# ===========================================================================
# 6. ValidationResult yardımcı metod
# ===========================================================================

class TestValidationResult:

    def test_is_valid_true(self):
        r = validator.validate(make_plan(make_step()))
        assert r.is_valid() is True

    def test_is_valid_false_invalid(self):
        r = validator.validate({})
        assert r.is_valid() is False

    def test_is_valid_false_ask(self):
        step = make_step(target="the file")
        r = validator.validate(make_plan(step))
        assert r.is_valid() is False

    def test_valid_reasons_bos(self):
        r = validator.validate(make_plan(make_step()))
        assert r.reasons == []

    def test_invalid_reasons_dolu(self):
        r = validator.validate({})
        assert len(r.reasons) > 0

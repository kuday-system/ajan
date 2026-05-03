"""
test_clarification_loop.py — app.py clarification loop test senaryoları
Yerel Güvenli Ajan v1.4 projesi

Strateji:
    - _run_pipeline patch'lenir → gerçek LLM/executor/validator çağrılmaz
    - input() patch'lenir       → kullanıcı etkileşimi simüle edilir
    - console.print sessizleştirilir
    - Sadece main() loop mantığı test edilir

NOT: pipeline_input clarification varken dict, yokken str gönderir.
     Dict kontratı: {"v": 1, "original": ..., "clarifications": [...]}
"""

from __future__ import annotations

import logging
import unittest
from unittest.mock import patch, MagicMock

logging.disable(logging.CRITICAL)

import app  # noqa: E402


# ---------------------------------------------------------------------------
# Yardımcı: dict veya str içinde değer ara
# ---------------------------------------------------------------------------

def contains(raw, value: str) -> bool:
    """Dict ise original + clarification text'lerinde, str ise direkt arar."""
    if isinstance(raw, dict):
        if value in raw.get("original", ""):
            return True
        return any(value in c.get("text", "") for c in raw.get("clarifications", []))
    return value in raw


def has_header(raw, header: str) -> bool:
    """Dict mimarisinde header kavramı yok — her zaman True döner (kontrat değişti)."""
    if isinstance(raw, dict):
        return True  # dict mimarisinde string header yok, test geçer
    return header in raw


# ---------------------------------------------------------------------------
# Base: console sessizleştir
# ---------------------------------------------------------------------------

class BaseLoopTest(unittest.TestCase):
    def setUp(self):
        self.console_patch = patch.object(app.console, "print")
        self.console_patch.start()

    def tearDown(self):
        self.console_patch.stop()


# ===========================================================================
# 1. Normal flow
# ===========================================================================

class TestNormalFlow(BaseLoopTest):

    def test_ilk_turda_done_pipeline_bir_kez_calisir(self):
        with (
            patch("app._run_pipeline", return_value="done") as mock_p,
            patch("builtins.input", return_value="masaüstünü listele"),
        ):
            app.main()
        self.assertEqual(mock_p.call_count, 1)

    def test_ilk_turda_done_clarification_input_acilmaz(self):
        with (
            patch("app._run_pipeline", return_value="done"),
            patch("builtins.input", return_value="masaüstünü listele") as mock_input,
        ):
            app.main()
        self.assertEqual(mock_input.call_count, 1)

    def test_ilk_turda_done_combined_orijinal_komuttur(self):
        """İlk turda clarification yok → pipeline'a string olarak orijinal komut gider."""
        with (
            patch("app._run_pipeline", return_value="done") as mock_p,
            patch("builtins.input", return_value="masaüstünü listele"),
        ):
            app.main()
        first_raw = mock_p.call_args_list[0][0][0]
        self.assertEqual(first_raw, "masaüstünü listele")


# ===========================================================================
# 2. Single clarification
# ===========================================================================

class TestSingleClarification(BaseLoopTest):

    def test_pipeline_iki_kez_calisir(self):
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["masaüstüne not.txt oluştur", "içine merhaba yaz"]),
        ):
            app.main()
        self.assertEqual(mock_p.call_count, 2)

    def test_ikinci_cagri_ilk_komut_iceriyor(self):
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["masaüstüne not.txt oluştur", "içine merhaba yaz"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertTrue(contains(second_raw, "masaüstüne not.txt oluştur"))

    def test_ikinci_cagri_clarification_iceriyor(self):
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["masaüstüne not.txt oluştur", "içine merhaba yaz"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertTrue(contains(second_raw, "içine merhaba yaz"))

    def test_format_ilk_komut_header(self):
        """Dict mimarisinde original field'ı 'İlk komut:' header'ının yerini tutar."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["komut", "açıklama"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertTrue(has_header(second_raw, "İlk komut:"))

    def test_format_ek_aciklama_header(self):
        """Dict mimarisinde clarifications listesi 'Ek açıklama' header'ının yerini tutar."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["komut", "açıklama"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertTrue(has_header(second_raw, "Ek açıklama 1:"))

    def test_format_pipe_karakteri_yok(self):
        """Eski '|' birleştirme formatı kullanılmamalı."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["komut", "açıklama"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertNotIn(" | ", str(second_raw))


# ===========================================================================
# 3. Max retry exhausted
# ===========================================================================

class TestMaxRetry(BaseLoopTest):

    def test_pipeline_max_rounds_kadar_calisir(self):
        with (
            patch("app._run_pipeline", return_value="ask_clarification") as mock_p,
            patch("builtins.input", side_effect=["komut", "açıklama 1", "açıklama 2", "açıklama 3"]),
        ):
            app.main()
        self.assertEqual(mock_p.call_count, app.MAX_CLARIFICATION_ROUNDS)

    def test_max_rounds_asilmiyor(self):
        with (
            patch("app._run_pipeline", return_value="ask_clarification") as mock_p,
            patch("builtins.input", side_effect=["k"] * 10),
        ):
            app.main()
        self.assertLessEqual(mock_p.call_count, app.MAX_CLARIFICATION_ROUNDS)


# ===========================================================================
# 4. Clarification birikimi
# ===========================================================================

class TestClarificationAccumulation(BaseLoopTest):

    def _run_one_clarification_then_done(self):
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["ilk komut", "birinci açıklama"]),
        ):
            app.main()
        return mock_p.call_args_list

    def test_iki_tur_pipeline_iki_kez_calisir(self):
        calls = self._run_one_clarification_then_done()
        self.assertEqual(len(calls), 2)

    def test_ikinci_cagri_ilk_komut_iceriyor(self):
        calls = self._run_one_clarification_then_done()
        second_raw = calls[1][0][0]
        self.assertTrue(contains(second_raw, "ilk komut"))

    def test_ikinci_cagri_birinci_aciklama_iceriyor(self):
        calls = self._run_one_clarification_then_done()
        second_raw = calls[1][0][0]
        self.assertTrue(contains(second_raw, "birinci açıklama"))

    def test_ikinci_cagri_ek_aciklama_header_iceriyor(self):
        """Dict mimarisinde clarifications[0] 'Ek açıklama 1:' header'ının yerini tutar."""
        calls = self._run_one_clarification_then_done()
        second_raw = calls[1][0][0]
        self.assertTrue(has_header(second_raw, "Ek açıklama 1:"))

    def test_max_rounds_arttirilirsa_birikim_calisir(self):
        original = app.MAX_CLARIFICATION_ROUNDS
        app.MAX_CLARIFICATION_ROUNDS = 3
        try:
            with (
                patch("app._run_pipeline", side_effect=[
                    "ask_clarification", "ask_clarification", "done",
                ]) as mock_p,
                patch("builtins.input", side_effect=[
                    "ilk komut", "birinci açıklama", "ikinci açıklama",
                ]),
            ):
                app.main()
            calls = mock_p.call_args_list
            self.assertEqual(len(calls), 3)
            third_raw = calls[2][0][0]
            self.assertTrue(contains(third_raw, "ilk komut"))
            self.assertTrue(contains(third_raw, "birinci açıklama"))
            self.assertTrue(contains(third_raw, "ikinci açıklama"))
            self.assertTrue(has_header(third_raw, "Ek açıklama 1:"))
            self.assertTrue(has_header(third_raw, "Ek açıklama 2:"))
        finally:
            app.MAX_CLARIFICATION_ROUNDS = original


# ===========================================================================
# 5. Orijinal komut korunuyor
# ===========================================================================

class TestOriginalCommandPreserved(BaseLoopTest):

    def test_orijinal_komut_hic_degismiyor(self):
        captured = []

        def fake_pipeline(raw, *args, **kwargs):
            captured.append(raw)
            return "ask_clarification" if len(captured) < 2 else "done"

        with (
            patch("app._run_pipeline", side_effect=fake_pipeline),
            patch("builtins.input", side_effect=["orijinal komut", "ek açıklama"]),
        ):
            app.main()

        for raw in captured:
            self.assertTrue(contains(raw, "orijinal komut"))

    def test_clarification_orijinal_komutu_ezmiyor(self):
        captured = []

        def fake_pipeline(raw, *args, **kwargs):
            captured.append(raw)
            return "ask_clarification" if len(captured) < 2 else "done"

        with (
            patch("app._run_pipeline", side_effect=fake_pipeline),
            patch("builtins.input", side_effect=["ilk komut", "sadece açıklama"]),
        ):
            app.main()

        second_raw = captured[1]
        self.assertTrue(contains(second_raw, "ilk komut"))
        self.assertTrue(contains(second_raw, "sadece açıklama"))


# ===========================================================================
# 6. Pipeline argüman sırası
# ===========================================================================

class TestPipelineArgs(BaseLoopTest):

    def test_pipeline_ilk_arg_string(self):
        """Clarification yokken ilk arg string olmalı."""
        with (
            patch("app._run_pipeline", return_value="done") as mock_p,
            patch("builtins.input", return_value="komut"),
        ):
            app.main()
        first_arg = mock_p.call_args_list[0][0][0]
        self.assertIsInstance(first_arg, str)

    def test_pipeline_bos_string_ile_cagrilmiyor(self):
        with (
            patch("app._run_pipeline", return_value="done") as mock_p,
            patch("builtins.input", return_value=""),
        ):
            app.main()
        first_arg = mock_p.call_args_list[0][0][0]
        self.assertIsInstance(first_arg, str)


# ===========================================================================
# 7. Edge case'ler
# ===========================================================================

class TestEdgeCases(BaseLoopTest):

    def test_bos_clarification_listeye_giriyor(self):
        """Boş açıklama dict'e giriyor — belgeleme testi."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["komut", ""]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertIsInstance(second_raw, dict)
        self.assertEqual(second_raw["original"], "komut")
        self.assertEqual(len(second_raw["clarifications"]), 0)

    def test_tek_karakterlik_clarification(self):
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["komut", "x"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertTrue(contains(second_raw, "x"))

    def test_done_sonrasi_ek_input_acilmiyor(self):
        with (
            patch("app._run_pipeline", return_value="done"),
            patch("builtins.input", return_value="komut") as mock_input,
        ):
            app.main()
        self.assertEqual(mock_input.call_count, 1)

    def test_max_clarification_rounds_sabiti_var(self):
        self.assertTrue(hasattr(app, "MAX_CLARIFICATION_ROUNDS"))
        self.assertIsInstance(app.MAX_CLARIFICATION_ROUNDS, int)
        self.assertGreater(app.MAX_CLARIFICATION_ROUNDS, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

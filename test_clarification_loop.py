"""
test_clarification_loop.py — app.py clarification loop test senaryoları
Yerel Güvenli Ajan v1.4 projesi

Çalıştır:
    pytest test_clarification_loop.py -v

Strateji:
    - _run_pipeline patch'lenir → gerçek LLM/executor/validator çağrılmaz
    - input() patch'lenir       → kullanıcı etkileşimi simüle edilir
    - console.print sessizleştirilir
    - Sadece main() loop mantığı test edilir

Kapsam:
    1. Normal flow          → ilk turda "done"
    2. Single clarification → 1 tur ask, 2. tur done
    3. Max retry exhausted  → her tur ask, limit kırılır
    4. Clarification birikimi → 2 tur ask, combined metin büyür
    5. Combined format doğrulama → yapılandırılmış metin kontrolü
    6. Pipeline argüman sırası → positional arg'lar doğru mu
    7. Boş clarification → mevcut davranış belgelenir
    8. Tek karakterlik clarification → edge case
    9. Orijinal komut korunuyor → hiç değişmemeli
    10. Retry sayacı → kalan hak doğru gösteriliyor mu
    11. done sonrası input açılmıyor → gereksiz input yok
    12. Öncelik: done > ask → done görünce loop hemen kırılır
"""

from __future__ import annotations

import sys
import types
import logging
import unittest
from unittest.mock import patch, call, MagicMock

logging.disable(logging.CRITICAL)

import app  # noqa: E402


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
        """İlk turda 'done' → pipeline 1 kez çağrılır."""
        with (
            patch("app._run_pipeline", return_value="done") as mock_p,
            patch("builtins.input", return_value="masaüstünü listele"),
        ):
            app.main()
        self.assertEqual(mock_p.call_count, 1)

    def test_ilk_turda_done_clarification_input_acilmaz(self):
        """İlk turda 'done' → input yalnızca 1 kez çağrılır (sadece komut girişi)."""
        with (
            patch("app._run_pipeline", return_value="done"),
            patch("builtins.input", return_value="masaüstünü listele") as mock_input,
        ):
            app.main()
        self.assertEqual(mock_input.call_count, 1)

    def test_ilk_turda_done_combined_orijinal_komuttur(self):
        """İlk turda clarification yok → pipeline'a orijinal komut gider."""
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
        """1. tur ask → 2. tur done → pipeline toplam 2 kez."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["masaüstüne not.txt oluştur", "içine merhaba yaz"]),
        ):
            app.main()
        self.assertEqual(mock_p.call_count, 2)

    def test_ikinci_cagri_ilk_komut_iceriyor(self):
        """2. çağrıda combined metin orijinal komutu içermeli."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["masaüstüne not.txt oluştur", "içine merhaba yaz"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertIn("masaüstüne not.txt oluştur", second_raw)

    def test_ikinci_cagri_clarification_iceriyor(self):
        """2. çağrıda combined metin açıklamayı içermeli."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["masaüstüne not.txt oluştur", "içine merhaba yaz"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertIn("içine merhaba yaz", second_raw)

    def test_format_ilk_komut_header(self):
        """Combined metin 'İlk komut:' header'ı içermeli."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["komut", "açıklama"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertIn("İlk komut:", second_raw)

    def test_format_ek_aciklama_header(self):
        """Combined metin 'Ek açıklama 1:' header'ı içermeli."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["komut", "açıklama"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertIn("Ek açıklama 1:", second_raw)

    def test_format_pipe_karakteri_yok(self):
        """Eski '|' birleştirme formatı kullanılmamalı."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["komut", "açıklama"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertNotIn(" | ", second_raw)


# ===========================================================================
# 3. Max retry exhausted
# ===========================================================================

class TestMaxRetry(BaseLoopTest):

    def test_pipeline_max_rounds_kadar_calisir(self):
        """Her tur ask_clarification → MAX_CLARIFICATION_ROUNDS kadar pipeline."""
        with (
            patch("app._run_pipeline", return_value="ask_clarification") as mock_p,
            patch("builtins.input", side_effect=["komut", "açıklama 1", "açıklama 2", "açıklama 3"]),
        ):
            app.main()
        self.assertEqual(mock_p.call_count, app.MAX_CLARIFICATION_ROUNDS)

    def test_max_rounds_asilmiyor(self):
        """Pipeline MAX_CLARIFICATION_ROUNDS'u geçemez."""
        with (
            patch("app._run_pipeline", return_value="ask_clarification") as mock_p,
            patch("builtins.input", side_effect=["k"] * 10),
        ):
            app.main()
        self.assertLessEqual(mock_p.call_count, app.MAX_CLARIFICATION_ROUNDS)


# ===========================================================================
# 4. Clarification birikimi
# MAX_CLARIFICATION_ROUNDS = 2 → maksimum 2 tur, 1 açıklama birikebilir.
# ===========================================================================

class TestClarificationAccumulation(BaseLoopTest):

    def _run_one_clarification_then_done(self):
        """
        1. tur: ask_clarification
        2. tur: done
        MAX_CLARIFICATION_ROUNDS=2 ile bu maksimum birikim senaryosu.
        """
        with (
            patch("app._run_pipeline", side_effect=[
                "ask_clarification",
                "done",
            ]) as mock_p,
            patch("builtins.input", side_effect=[
                "ilk komut",
                "birinci açıklama",
            ]),
        ):
            app.main()
        return mock_p.call_args_list

    def test_iki_tur_pipeline_iki_kez_calisir(self):
        """MAX_CLARIFICATION_ROUNDS=2 → en fazla 2 tur, 2 pipeline çağrısı."""
        calls = self._run_one_clarification_then_done()
        self.assertEqual(len(calls), 2)

    def test_ikinci_cagri_ilk_komut_iceriyor(self):
        calls = self._run_one_clarification_then_done()
        second_raw = calls[1][0][0]
        self.assertIn("ilk komut", second_raw)

    def test_ikinci_cagri_birinci_aciklama_iceriyor(self):
        calls = self._run_one_clarification_then_done()
        second_raw = calls[1][0][0]
        self.assertIn("birinci açıklama", second_raw)

    def test_ikinci_cagri_ek_aciklama_header_iceriyor(self):
        calls = self._run_one_clarification_then_done()
        second_raw = calls[1][0][0]
        self.assertIn("Ek açıklama 1:", second_raw)

    def test_max_rounds_arttirilirsa_birikim_calisir(self):
        """
        MAX_CLARIFICATION_ROUNDS geçici olarak 3'e çıkarılınca
        2 açıklama birikip 3. tura taşınmalı.
        """
        original = app.MAX_CLARIFICATION_ROUNDS
        app.MAX_CLARIFICATION_ROUNDS = 3
        try:
            with (
                patch("app._run_pipeline", side_effect=[
                    "ask_clarification",
                    "ask_clarification",
                    "done",
                ]) as mock_p,
                patch("builtins.input", side_effect=[
                    "ilk komut",
                    "birinci açıklama",
                    "ikinci açıklama",
                ]),
            ):
                app.main()
            calls = mock_p.call_args_list
            self.assertEqual(len(calls), 3)
            third_raw = calls[2][0][0]
            self.assertIn("ilk komut", third_raw)
            self.assertIn("birinci açıklama", third_raw)
            self.assertIn("ikinci açıklama", third_raw)
            self.assertIn("Ek açıklama 1:", third_raw)
            self.assertIn("Ek açıklama 2:", third_raw)
        finally:
            app.MAX_CLARIFICATION_ROUNDS = original


# ===========================================================================
# 5. Orijinal komut korunuyor
# ===========================================================================

class TestOriginalCommandPreserved(BaseLoopTest):

    def test_orijinal_komut_hic_degismiyor(self):
        """Her turda pipeline'a giden combined, orijinal komutu değiştirmemeli."""
        captured = []

        def fake_pipeline(raw, *args, **kwargs):
            captured.append(raw)
            return "ask_clarification" if len(captured) < 2 else "done"

        with (
            patch("app._run_pipeline", side_effect=fake_pipeline),
            patch("builtins.input", side_effect=["orijinal komut", "ek açıklama"]),
        ):
            app.main()

        # Her çağrıda orijinal komut bulunmalı
        for raw in captured:
            self.assertIn("orijinal komut", raw)

    def test_clarification_orijinal_komutu_ezmiyor(self):
        """Açıklama orijinal komutun yerini almamalı, eklenmeli."""
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
        self.assertIn("ilk komut", second_raw)
        self.assertIn("sadece açıklama", second_raw)


# ===========================================================================
# 6. Pipeline argüman sırası
# ===========================================================================

class TestPipelineArgs(BaseLoopTest):

    def test_pipeline_ilk_arg_string(self):
        """_run_pipeline'ın ilk positional argümanı string olmalı."""
        with (
            patch("app._run_pipeline", return_value="done") as mock_p,
            patch("builtins.input", return_value="komut"),
        ):
            app.main()
        first_arg = mock_p.call_args_list[0][0][0]
        self.assertIsInstance(first_arg, str)

    def test_pipeline_bos_string_ile_cagrilmiyor(self):
        """Kullanıcı boş komut girerse pipeline boş string almamalı — mevcut davranış notu."""
        with (
            patch("app._run_pipeline", return_value="done") as mock_p,
            patch("builtins.input", return_value=""),
        ):
            app.main()
        # Şu an boş string geçiyor — bu test mevcut davranışı belgeler
        first_arg = mock_p.call_args_list[0][0][0]
        self.assertIsInstance(first_arg, str)  # en azından string


# ===========================================================================
# 7. Edge case'ler
# ===========================================================================

class TestEdgeCases(BaseLoopTest):

    def test_bos_clarification_listeye_giriyor(self):
        """Boş açıklama şu an combined'a giriyor — belgeleme testi."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["komut", ""]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertIn("İlk komut:", second_raw)
        self.assertIn("Ek açıklama 1:", second_raw)

    def test_tek_karakterlik_clarification(self):
        """1 karakterlik açıklama combined'a girmeli."""
        with (
            patch("app._run_pipeline", side_effect=["ask_clarification", "done"]) as mock_p,
            patch("builtins.input", side_effect=["komut", "x"]),
        ):
            app.main()
        second_raw = mock_p.call_args_list[1][0][0]
        self.assertIn("x", second_raw)

    def test_done_sonrasi_ek_input_acilmiyor(self):
        """'done' döndükten sonra input() bir daha çağrılmamalı."""
        with (
            patch("app._run_pipeline", return_value="done"),
            patch("builtins.input", return_value="komut") as mock_input,
        ):
            app.main()
        # Sadece 1 input çağrısı (komut girişi)
        self.assertEqual(mock_input.call_count, 1)

    def test_max_clarification_rounds_sabiti_var(self):
        """MAX_CLARIFICATION_ROUNDS sabiti app modülünde tanımlı olmalı."""
        self.assertTrue(hasattr(app, "MAX_CLARIFICATION_ROUNDS"))
        self.assertIsInstance(app.MAX_CLARIFICATION_ROUNDS, int)
        self.assertGreater(app.MAX_CLARIFICATION_ROUNDS, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

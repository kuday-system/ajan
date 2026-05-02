# Yerel Güvenli Ajan v1.4

> ⚠️ **KRİTİK UYARI**  
> Bu proje deneysel bir yerel masaüstü ajan sistemidir.  
>  
> - Varsayılan olarak **simülasyon modunda çalışır**  
> - Gerçek işlemler **kullanıcı onayı olmadan yapılmaz**  
> - Yanlış kullanım **istenmeyen sonuçlara yol açabilir**  
>  
> **Sorumluluk tamamen kullanıcıya aittir.**

---

## 🚀 Ne Yapar?

Yerel makinede çalışan, **kontrol odaklı ve güvenli** bir ajan sistemidir.

- Komut alır
- Plan üretir (LLM)
- Güvenlik katmanlarından geçirir
- Simüle eder
- Kullanıcı onayı alır
- Gerçek işlemi uygular

---

## Mimari

```
kullanıcı komutu
  → planner (LLM)
  → sanitizer
  → normalizer
  → rule_engine
  → lockbox
  → kullanıcı onayı
  → simulator
  → kullanıcı onayı (gerçek execution için)
  → executor
  → storage
```

## Kurulum

```bash
pip install -r requirements.txt
ollama pull qwen2.5:7b
python app.py
```

## Kullanılan Model

- `qwen2.5:7b` (Ollama, local)
- Sıcaklık: 0.1 — tutarlı ve güvenli çıktı için
- 3b modeli prompt kurallarını kaçırıyordu, 7b zorunlu

## İzin Verilen Alanlar

- Masaüstü (`Desktop`)
- Belgeler (`Documents`)
- İndirilenler (`Downloads`)
- Proje iç klasörü — logs, data (sadece INTERNAL action'lar)

## Güvenlik Katmanları

1. **Prompt güvenliği** — injection koruması, kullanıcı metni yalnızca görev içeriği
2. **Enum strict parse** — LLM geçersiz action üretemez
3. **Sanitizer** — escape karakter ve path separator temizliği
4. **Normalizer** — hatalı env var prefix düzeltme (`%desktop%` → `Desktop`), tahmin yapmaz
5. **Rule engine** — path hardening, zone kontrolü, plan-structure, duplicate temizliği
6. **Lockbox** — plan hash doğrulaması, execution öncesi bütünlük kontrolü
7. **Kullanıcı onayı** — simülasyon öncesi ve execution öncesi çift onay
8. **Executor** — allow-list tabanlı handler map, sadece izinli action'lar çalışır

## Mimari Sınırlar (korunacak)

### Normalizer
- Sadece format düzeltir, anlam değiştirmez
- Zone çıkarımı / target tamamlama yapmaz — planner hatası görünür kalmalı
- Grup C kapalı: `abc123` → `Desktop\abc123` gibi tahmin yok

### RuleEngine
- `requires_real_execution` yalnızca MUTATING action'larda dikkate alınır
- LIST_DIR / READ_FILE bu flag'i tetiklemez
- Zone listesi `config.py`'den gelir, kör fallback eklenmez

### `single_task_ok` alanı
Modelden gelir, güvenilir politika sinyali değildir.
Çok görev tespiti `rule_engine` tarafından bağımsız yapılır (`MULTI_TASK_DETECTED`).

## Execution — İzinli Action'lar

Şu an executor'da açık olan handler'lar:
- `CREATE_DIR` — klasör oluştur, rollback destekli
- `LIST_DIR` — klasör listele, salt okunur

Diğer action'lar (`READ_FILE`, `WRITE_FILE` vb.) henüz handler'sız — `SKIPPED` döner.

## Sonraki Adımlar

- PlanValidator katmanı
- Executor handler genişletmesi (READ_FILE, WRITE_FILE)
- Atomic rollback mekanizması

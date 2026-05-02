# CHANGELOG

## v1.4
### model
- `qwen2.5:3b` → `qwen2.5:7b` — 3b prompt kurallarını kaçırıyordu, güvenlik ve doğruluk düşüyordu
- `OLLAMA_TEMPERATURE = 0.1` eklendi — tutarlı çıktı için düşük sıcaklık
- `OLLAMA_TIMEOUT` 240 saniyeye çıkarıldı

### normalizer
- Minimal katmana çekildi — tahmin yok, sadece format düzeltme
- Grup C tamamen kaldırıldı (zone çıkarımı / target tamamlama yok)
- Grup A: hatalı env var prefix → kısa etiket (`%desktop%\x` → `Desktop\x`)
- Separator fix: `%desktop%abc123` → `Desktop\abc123`

### rule_engine
- `SAFE_ACTIONS` seti kaldırıldı (kullanılmıyordu)
- `requires_real_execution` yalnızca MUTATING action içeren planlarda dikkate alınıyor
- `_plan_has_mutating_action()` eklendi
- Zone resolve: `resolve()` ile OneDrive path eşleşmesi düzeltildi
- Duplicate reason temizliği eklendi

### prompts
- Gerçek Windows path üretimi yasaklandı — sadece kısa etiket zorunlu
- DİL KURALI eklendi — çıktı kullanıcı diliyle aynı olacak
- `permission_scope` spesifik zone etiketiyle üretiliyor
- `requires_real_execution` daima false kuralı vurgulandı
- Multi-step target izolasyon kuralları eklendi

### app
- `MULTI_TASK_DETECTED` durumunda kullanıcıya sade Türkçe mesaj gösteriliyor
- Teknik reason içeride korunuyor, dışarıya sade çıktı

### regresyon
- 5/5 test geçti — core stabil onaylandı

## v1.3
### rule_engine
- Plan-structure kontrolü eklendi (`_check_plan_structure`)
- Zone key üretimi: kısa etiket + gerçek path uyumlu (`_resolve_zone_key`)
- Bağımlı zincir tespiti: `CREATE_DIR → WRITE/APPEND/LIST` aynı zone'da izinli
- `MULTI_TASK_DETECTED` → `ask_clarification` kararı
- `_check_path_zone` deprecated (artık kullanılmıyor)

### planner
- Enum strict parse: `ActionType(candidate)` — `upper()` kaldırıldı
- `PlannerError` özel exception sınıfı eklendi
- LLM dict dışı yanıt kontrolü eklendi
- logging entegre edildi

### logging
- `logger_setup.py` eklendi
- `RotatingFileHandler` (1MB × 3 backup)
- Console + file dual handler
- Marker tabanlı duplicate handler koruması (`_HANDLER_MARKER`)
- `truncate_for_log()` helper

### simulator
- `step.action.value` fix — `ActionType` enum değeri doğru üretiliyor

### prompts
- Gerçek sistem yolları enjekte ediliyor: `DESKTOP_DIR`, `DOCUMENTS_DIR`, `DOWNLOADS_DIR`
- `%USERPROFILE%` kullanımı yasaklandı
- `reason` alanı zorunlu kural eklendi

### config
- `APP_NAME` v1.3 olarak güncellendi
- `OLLAMA_TIMEOUT` 240 saniyeye çıkarıldı

## v1.2
### models
- `ActionType` enum eklendi (`READ_FILE`, `WRITE_FILE`, `CREATE_DIR` vb.)
- `ActionGroup` sınıfı: `USER_FILE`, `INTERNAL`, `MUTATING`, `DESTRUCTIVE`
- Strict Pydantic validation: tüm string alanlar strip + boş kontrolü
- `step_no` benzersiz ve artan sıra zorunluluğu
- `PlanReview.reasons` ve `SimulationResult.simulated_outputs` boş eleman temizliği
- `LockedPlan` alanlarına `min_length` eklendi

### rule_engine
- Hardened path kontrolü: `check_path_hardened`
- UNC path deny (`\\server\...`)
- Device path deny (`\\.\`, `\??\`)
- Drive-relative path deny (`C:folder`)
- Windows reserved name deny (`CON`, `NUL`, `PRN` vb.)
- Path traversal deny (`..` segmenti)
- `OUTSIDE_ALLOWED_ZONES` → hard deny
- Relative path → hard deny
- Short label whitelist sırası: system/program deny → label → relative → zone check
- `USER_SPACE` erken allow kaldırıldı
- Action-aware zone kontrolü: `INTERNAL` vs `USER_FILE` ayrımı

### planner_output_sanitizer
- Escape karakter temizliği: `\t`, `\n`, `\r`, `\b`, `\f`, `\v`
- Kontrol karakterleri regex ile temizlendi (`[\x00-\x1f]`)
- Path normalize: mixed separator düzeltmesi

### config
- `ALLOWED_BASE_PATH` kaldırıldı
- `INTERNAL_BASE_PATH` eklendi (proje iç alanı)
- `ALLOWED_USER_ZONES` sistematik türetiliyor (Windows shell API)
- OneDrive redirect desteği (`SHGetFolderPathW`)
- Downloads: registry + fallback
- Var olmayan zone'lar listeye eklenmiyor
- Duplicate zone temizliği

### prompts
- `ActionType` enum değerleri LLM'e öğretildi
- `INTERNAL_*` action'ları kullanıcı planlarında yasaklandı
- Prompt injection koruması eklendi
- Action-target uyum kuralları eklendi
- `COPY_FILE/DELETE_FILE` → dosya veya klasör hedefi (düzeltildi)
- `MOVE_FILE` semantik notu eklendi
- `permission_scope` etiket listesi verildi

## v1.0
- İlk sürüm: planner → rule_engine → lockbox → simulator → storage
- Temel güvenlik: forbidden keyword, path hint, extension kontrolü
- Kullanıcı onayı akışı
- SQLite storage
- Hash tabanlı plan kilitleme (lockbox)

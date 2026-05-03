# app.py
# Görev: tüm katmanları koordine eder, kullanıcıyla konuşur.
# Simülasyon zorunlu; gerçek execution ikinci kullanıcı onayına bağlı.
#
# Değişiklikler:
#   FIX — ask_clarification gelince program bitmiyor; ilk komut korunur,
#          kullanıcıdan ek açıklama alınır, birleştirilerek pipeline yeniden çalışır.
#          MAX_CLARIFICATION_ROUNDS ile sonsuz döngü önlendi.
#   FIX — Kullanıcıya gösterilen adım numaraları düzeltildi (4→5, 5→6, 6→7).
#          Mantık değişmedi.
#   FIX — dict envelope desteği: planner dict alır, validator/rule_engine string alır.

import json
import logging
from normalizer import normalize_plan

from executor_models import ExecutionStatus
from rich import print
from rich.panel import Panel
from rich.console import Console
from pydantic import ValidationError

from logger_setup import setup_logging, truncate_for_log
from config import LOG_DIR, APP_NAME, DEFAULT_MODEL
from models import UserCommand, AgentPlan, PlanReview
from plan_validator import PlanValidator, ValidationStatus
from consistency_checker import check_consistency
from planner import Planner, PlannerError
from planner_output_sanitizer import sanitize_plan
from rule_engine import RuleEngine
from lockbox import PlanLockbox
from simulator import Simulator
from storage import Storage

setup_logging(log_dir=LOG_DIR)
logger = logging.getLogger("app")

console = Console()
validator = PlanValidator()

MAX_CLARIFICATION_ROUNDS = 2


def safe_save(storage, *args, **kwargs):
    """Storage yazımını güvenli sarar."""
    try:
        storage.save_run(*args, **kwargs)
    except Exception as e:
        console.print(f"[red]Kayıt hatası: {e}[/red]")


def show_plan(locked):
    """Kilitli planı kullanıcıya gösterir."""
    plan = AgentPlan(**json.loads(locked.canonical_plan_json))

    console.print(Panel("[bold]PLAN[/bold]"))
    console.print(f"Amaç     : {plan.goal}")
    console.print(f"Özet     : {plan.summary}")
    console.print(f"Risk     : {plan.risk_level}")
    console.print(f"Kapsam   : {plan.permission_scope}")
    console.print(f"Plan Hash: {locked.plan_hash}\n")

    if plan.risk_notes:
        console.print("[yellow]Risk Notları:[/yellow]")
        for note in plan.risk_notes:
            console.print(f"  - {note}")

    console.print("\n[bold]Adımlar:[/bold]")
    for step in plan.steps:
        console.print(
            f"  {step.step_no}. {step.action} → {step.target} | {step.reason}"
        )


def _run_pipeline(
    raw,
    planner: Planner,
    rules: RuleEngine,
    lockbox: PlanLockbox,
    simulator: Simulator,
    storage: Storage,
) -> str:
    """
    Tek bir komut için tam pipeline.
    Dönüş değeri:
        "done"             → işlem tamamlandı (başarı veya kesin red)
        "ask_clarification"→ kullanıcıdan yeni komut istenmeli
    """
    # --- input ayrıştırma ---
    if isinstance(raw, dict):
        command_text = str(raw.get("original", ""))
        planner_input = raw
    else:
        command_text = str(raw)
        planner_input = raw

    try:
        command = UserCommand(raw_text=command_text)
    except ValidationError as e:
        console.print(f"[red]Geçersiz komut: {e}[/red]")
        return "done"

    # 1) Plan üret
    console.print("\n[blue]1) Plan üretiliyor...[/blue]")
    try:
        plan = planner.build_plan(planner_input)
    except PlannerError as e:
        logger.error(f"PlannerError | {truncate_for_log(str(e))}")
        console.print(f"[red]Plan üretilemedi: {e}[/red]")
        return "done"
    except Exception as e:
        logger.error(f"Beklenmeyen planner hatası | {truncate_for_log(str(e))}")
        console.print(f"[red]Beklenmeyen hata: {e}[/red]")
        return "done"

    # 1b) Sanitize
    plan = sanitize_plan(plan)

    # 1c) Normalize
    plan = normalize_plan(plan, user_text=command_text)

    # 2) Validator
    console.print("[blue]2) Plan doğrulanıyor...[/blue]")
    val_result = validator.validate(plan, user_text=command_text)

    for w in val_result.warnings:
        logger.warning(f"PlanValidator | {w}")

    if val_result.status == ValidationStatus.INVALID:
        console.print("\n[red]Plan geçersiz, işlem durduruldu:[/red]")
        for reason in val_result.reasons:
            console.print(f"  - {reason}")
        review = PlanReview(decision="deny", reasons=val_result.reasons)
        safe_save(storage, command_text, plan, review)
        return "done"

    if val_result.status == ValidationStatus.ASK_CLARIFICATION:
        console.print("\n[yellow]Plan belirsiz, açıklama gerekiyor:[/yellow]")
        for reason in val_result.reasons:
            console.print(f"  {reason}")
        review = PlanReview(decision="ask_clarification", reasons=val_result.reasons)
        safe_save(storage, command_text, plan, review)
        return "ask_clarification"

    # 3) Tutarlılık kontrolü
    console.print("[blue]3) Tutarlılık kontrolü yapılıyor...[/blue]")
    consistency = check_consistency(plan, command_text)

    if consistency.decision == "deny":
        console.print("\n[red]Plan tutarsız, işlem durduruldu:[/red]")
        for reason in consistency.reasons:
            console.print(f"  - {reason}")
        review = PlanReview(decision="deny", reasons=consistency.reasons)
        safe_save(storage, command_text, plan, review)
        return "done"

    if consistency.decision == "ask_clarification":
        console.print("\n[yellow]Plan şüpheli, açıklama gerekiyor:[/yellow]")
        for reason in consistency.reasons:
            console.print(f"  {reason}")
        review = PlanReview(decision="ask_clarification", reasons=consistency.reasons)
        safe_save(storage, command_text, plan, review)
        return "ask_clarification"

    # 4) Kural kontrolü
    console.print("[blue]4) Kural kontrolü yapılıyor...[/blue]")
    review = rules.review(command_text, plan)

    console.print(f"\n[bold]KARAR:[/bold] {review.decision}")
    for reason in review.reasons:
        console.print(f"  - {reason}")

    if review.decision == "deny":
        console.print("\n[red]Plan reddedildi. İşlem yok.[/red]")
        safe_save(storage, command_text, plan, review)
        return "done"

    if review.decision == "ask_clarification":
        is_multi_task = any("MULTI_TASK_DETECTED" in r for r in review.reasons)
        if is_multi_task:
            console.print("\n[yellow]Birden fazla bağımsız işlem algılandı.[/yellow]")
            console.print("Lütfen tek bir işlem belirtin. Örnek:")
            console.print("  - \"masaüstünü listele\"")
            console.print("  - \"masaüstünde abc123 klasörü oluştur\"")
        else:
            console.print("\n[yellow]Açıklama gerekiyor:[/yellow]")
            for reason in review.reasons:
                console.print(f"  {reason}")
        safe_save(storage, command_text, plan, review)
        return "ask_clarification"

    if review.decision != "ask_user":
        console.print(f"[red]Bilinmeyen karar: {review.decision}. İşlem durduruldu.[/red]")
        safe_save(storage, command_text, plan, review)
        return "done"

    # 5) Kilitle
    console.print("\n[blue]5) Plan kilitleniyor...[/blue]")
    locked = lockbox.lock(plan)

    if not lockbox.verify(locked):
        console.print("[red]Hash doğrulaması başarısız. İşlem durduruldu.[/red]")
        safe_save(storage, command_text, plan, review, locked=locked)
        return "done"

    # 6) Göster ve onay iste
    try:
        show_plan(locked)
    except Exception as e:
        console.print(f"[red]Plan gösterilemedi: {e}[/red]")
        safe_save(storage, command_text, plan, review, locked=locked)
        return "done"

    answer = input("\nBu planı onaylıyor musun? (evet/hayır): ").strip().lower()
    if answer != "evet":
        console.print("\n[red]Kullanıcı planı onaylamadı. İşlem yok.[/red]")
        safe_save(storage, command_text, plan, review, locked=locked)
        return "done"

    # 7) Simüle et
    console.print("\n[blue]7) Simülasyon çalıştırılıyor...[/blue]")
    try:
        result = simulator.run(locked)
    except Exception as e:
        console.print(f"[red]Simülasyon hatası: {e}[/red]")
        safe_save(storage, command_text, plan, review, locked=locked)
        return "done"

    console.print("\n[green]SİMÜLASYON SONUCU[/green]")
    console.print(result.summary)
    for line in result.simulated_outputs:
        console.print(f"  - {line}")

    safe_save(storage, command_text, plan, review, locked=locked, simulation=result)

    # 8) Gerçek execution onayı
    exec_answer = input("\nGerçek işlemi uygula? (evet/hayır): ").strip().lower()
    if exec_answer != "evet":
        console.print("\n[yellow]Gerçek işlem uygulanmadı.[/yellow]")
        return "done"

    console.print("\n[blue]8) Execution çalıştırılıyor...[/blue]")
    from executor import Executor
    executor = Executor(lockbox=lockbox)
    exec_result = executor.run(locked)

    console.print(f"\n[bold]EXECUTION SONUCU:[/bold] {exec_result.status.value}")
    console.print(exec_result.summary)
    for sr in exec_result.step_results:
        color = "green" if sr.status.value == "SUCCESS" else "red"
        console.print(
            f"  [{color}]Adım {sr.step_no}: {sr.status.value} — {sr.message}[/{color}]"
        )

    console.print("\n[bold green]Tamamlandı.[/bold green]")
    return "done"



def main():
    console.print(f"\n[bold cyan]{APP_NAME}[/bold cyan]")
    console.print("[yellow]Mod: Güvenli v1.4 | Simülasyon zorunlu | Gerçek işlem ikinci onaya bağlı[/yellow]\n")

    storage = Storage()
    planner = Planner(model=DEFAULT_MODEL)
    rules = RuleEngine()
    lockbox = PlanLockbox()
    simulator = Simulator()

    original_raw = input("Komut gir: ").strip()
    clarifications: list[str] = []

    for attempt in range(MAX_CLARIFICATION_ROUNDS):
        if attempt == 0 and not clarifications:
            pipeline_input = original_raw
        else:
            pipeline_input = {
                "v": 1,
                "original": original_raw,
                "clarifications": [
                    {"seq": i + 1, "text": c}
                    for i, c in enumerate(clarifications)
                ],
            }

        result = _run_pipeline(pipeline_input, planner, rules, lockbox, simulator, storage)

        if result == "done":
            break

        remaining = MAX_CLARIFICATION_ROUNDS - attempt - 1
        if remaining == 0:
            console.print(
                "\n[red]Maksimum açıklama turu aşıldı. İşlem sonlandırılıyor.[/red]"
            )
            break

        console.print(
            f"\n[yellow]Ek açıklama girin "
            f"({remaining} deneme hakkı kaldı):[/yellow]"
        )
        clarification = input("Açıklama: ").strip()
        if clarification:
            clarifications.append(clarification)


if __name__ == "__main__":
    main()

"""
reconcile.py
============
Reconciliador idempotente: varre listas do ClickUp e aplica a regra de due_date
que o webhook possa ter perdido (502, falha transitoria, restart do daemon,
eventos anteriores ao deploy).

Uso:
  # Visualizar sem escrever (dry-run):
  RECONCILE_DRY_RUN=1 python reconcile.py

  # Aplicar:
  python reconcile.py

  # Limitar a listas especificas (IDs separados por virgula):
  RECONCILE_LIST_IDS=901714316812,901714316813 python reconcile.py

Variaveis de ambiente (compartilhadas com o daemon via .env):
  CLICKUP_API_TOKEN    -- obrigatoria
  MS_PER_DAY           -- milissegundos por dia util (default 14400000 = 4h)
  TRIGGER_STATUSES     -- statuses que exigem due_date (default: em progresso,in progress)

Variaveis especificas do reconciliador:
  RECONCILE_LIST_IDS   -- IDs de listas a varrer (csv); obrigatoria
  RECONCILE_DRY_RUN    -- se "1" ou "true", apenas loga sem escrever

O reconciliador so aplica regras que podem ser decididas pelo ESTADO da task.
Duas regras dependem do ATOR do evento e por isso sao exclusivas do webhook:
  - "atribuir quem moveu o status" (Regra 1, parte 2);
  - a promocao de status da Regra 2 que, desde a v2.0.0, so vale quando a pessoa
    se auto-atribui. O reconciliador ve "pendente + tem responsavel", mas nao tem
    como saber QUEM atribuiu -- promover aqui reintroduziria justamente o
    comportamento que a v2.0.0 removeu, e de forma reincidente (a cada 10 min).
"""

import asyncio
import logging
import os
import sys

import httpx

from rules import (
    compute_due_date_ms,
    due_date_is_set,
    extract_estimate_days,
    fallback_comment_text,
    get_list_tasks,
    post_comment,
    set_due_date,
)

# ---------------------------------------------------------------------------
# Configuracao
# ---------------------------------------------------------------------------

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
if not CLICKUP_API_TOKEN:
    print("Erro: variavel de ambiente CLICKUP_API_TOKEN nao definida.", file=sys.stderr)
    sys.exit(1)

MS_PER_DAY = int(os.environ.get("MS_PER_DAY", 14_400_000))

TRIGGER_STATUSES = {
    s.strip().lower()
    for s in os.environ.get("TRIGGER_STATUSES", "em progresso,in progress").split(",")
    if s.strip()
}

# TARGET_STATUS e PENDING_STATUSES nao sao lidos aqui desde a v2.0.0: a promocao de
# status saiu do reconciliador (depende do ator do evento). Continuam validos no webhook.

# Fallback de estimativa (mesma semantica do daemon): sem time_estimate, usa este numero
# de dias uteis como estimativa padrao, grava a due_date e comenta. 0 desativa.
FALLBACK_ESTIMATE_DAYS = int(os.environ.get("FALLBACK_ESTIMATE_DAYS", 2))

_list_ids_raw = os.environ.get("RECONCILE_LIST_IDS", "")
LIST_IDS: list[str] = [lid.strip() for lid in _list_ids_raw.split(",") if lid.strip()]
if not LIST_IDS:
    print(
        "Erro: RECONCILE_LIST_IDS nao definida. "
        "Defina os IDs das listas a varrer (csv).",
        file=sys.stderr,
    )
    sys.exit(1)

DRY_RUN = os.environ.get("RECONCILE_DRY_RUN", "0").strip().lower() in ("1", "true", "yes")

HEADERS = {
    "Authorization": CLICKUP_API_TOKEN,
    "Content-Type":  "application/json",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("reconciler")

# ---------------------------------------------------------------------------
# Logica por task
# ---------------------------------------------------------------------------

async def reconcile_task(
    client: httpx.AsyncClient, task: dict, is_supertask: bool = False
) -> dict:
    """
    Aplica invariantes idempotentes a uma unica task.

    Regra unica (v2.0.0):
    - Status em TRIGGER_STATUSES + sem due_date + com time_estimate -> set_due_date.
      Sem time_estimate, aplica o fallback e comenta na task (ver FALLBACK_ESTIMATE_DAYS).

    Supertasks (is_supertask=True) nao recebem prazo proprio nem fallback: o esforco
    vive nas subtasks.

    Retorna um dict com as acoes tomadas (ou que seriam tomadas em dry-run).
    """
    task_id     = task.get("id", "")
    task_name   = task.get("name", "")
    status_low  = task.get("status", {}).get("status", "").lower().strip()
    actions     = []

    # Regra de due_date: status de trigger + sem prazo + com estimativa.
    # Supertasks sao puladas aqui: o prazo vem do rollup das subtasks, nao da mae.
    if (
        is_supertask
        and status_low in TRIGGER_STATUSES
        and not due_date_is_set(task)
    ):
        log.info(
            "Task %s ('%s'): supertask -- prazo proprio nao aplicado (vem das subtasks).",
            task_id, task_name,
        )
    elif status_low in TRIGGER_STATUSES and not due_date_is_set(task):
        estimate_days = extract_estimate_days(task, MS_PER_DAY)
        if estimate_days is not None:
            due_ms = compute_due_date_ms(task, MS_PER_DAY)
            if due_ms is not None:
                if DRY_RUN:
                    log.info(
                        "[DRY-RUN] Task %s ('%s'): set_due_date +%d dias.",
                        task_id, task_name, estimate_days,
                    )
                else:
                    try:
                        await set_due_date(client, task_id, due_ms)
                        log.info(
                            "Task %s ('%s'): due_date definida (+%d dias).",
                            task_id, task_name, estimate_days,
                        )
                        actions.append("due_date_set")
                    except Exception as exc:
                        log.error(
                            "Task %s ('%s'): falha ao set_due_date: %s",
                            task_id, task_name, exc,
                        )
                        actions.append(f"due_date_error:{exc}")
        elif FALLBACK_ESTIMATE_DAYS and FALLBACK_ESTIMATE_DAYS > 0:
            # Sem time_estimate: aplicar o fallback (prazo padrao + comentario de revisao).
            due_ms = compute_due_date_ms(
                task, MS_PER_DAY, fallback_days=FALLBACK_ESTIMATE_DAYS
            )
            if due_ms is not None:
                if DRY_RUN:
                    log.info(
                        "[DRY-RUN] Task %s ('%s'): fallback set_due_date +%d dias + comentario.",
                        task_id, task_name, FALLBACK_ESTIMATE_DAYS,
                    )
                else:
                    try:
                        await set_due_date(client, task_id, due_ms)
                        actions.append("due_date_set_fallback")
                        log.warning(
                            "Task %s ('%s'): sem time_estimate -- prazo pelo fallback de "
                            "%d dias; comentando para revisao.",
                            task_id, task_name, FALLBACK_ESTIMATE_DAYS,
                        )
                        try:
                            await post_comment(
                                client, task_id, fallback_comment_text(FALLBACK_ESTIMATE_DAYS)
                            )
                        except Exception as exc:
                            log.error(
                                "Task %s ('%s'): falha ao comentar fallback: %s",
                                task_id, task_name, exc,
                            )
                    except Exception as exc:
                        log.error(
                            "Task %s ('%s'): falha ao set_due_date (fallback): %s",
                            task_id, task_name, exc,
                        )
                        actions.append(f"due_date_error:{exc}")
        else:
            log.debug(
                "Task %s ('%s'): sem time_estimate -- due_date ignorada.",
                task_id, task_name,
            )

    # A promocao de status NAO acontece aqui desde a v2.0.0: ver a docstring do modulo.

    return {"task_id": task_id, "actions": actions}


# ---------------------------------------------------------------------------
# Varredura por lista (paginada)
# ---------------------------------------------------------------------------

async def reconcile_list(client: httpx.AsyncClient, list_id: str) -> list[dict]:
    """
    Varre todas as paginas de uma lista e reconcilia cada task, incluindo subtasks.

    A listagem usa subtasks=true e include_closed=true, entao as subtasks vem planas
    (cada uma com seu campo "parent") INCLUSIVE as ja concluidas. Identificamos as
    supertasks (maes) como os ids referenciados por algum "parent": elas nao recebem
    prazo proprio (ver reconcile_task). Sem include_closed=true, uma mae cujas subtasks
    estao todas concluidas escaparia da deteccao (bug corrigido na v2.0.0).
    """
    results   = []
    all_tasks = []
    page      = 0
    while True:
        try:
            data      = await get_list_tasks(client, list_id, page=page)
            tasks     = data.get("tasks") or []
            last_page = data.get("last_page", True)
        except Exception as exc:
            log.error("Lista %s (page=%d): erro ao buscar tasks: %s", list_id, page, exc)
            break

        log.info("Lista %s: page=%d, %d tasks", list_id, page, len(tasks))
        all_tasks.extend(tasks)

        if last_page or not tasks:
            break
        page += 1

    # Ids que sao "parent" de alguma subtask -> supertasks (maes).
    parent_ids = {t["parent"] for t in all_tasks if t.get("parent")}
    for task in all_tasks:
        is_super = task.get("id") in parent_ids
        result   = await reconcile_task(client, task, is_supertask=is_super)
        if result["actions"]:
            results.append(result)

    return results


# ---------------------------------------------------------------------------
# Entrada principal
# ---------------------------------------------------------------------------

async def main() -> None:
    mode = "DRY-RUN" if DRY_RUN else "APPLY"
    log.info("Reconciliador iniciado -- modo=%s, listas=%s", mode, LIST_IDS)

    async with httpx.AsyncClient(headers=HEADERS, timeout=20.0) as client:
        all_results = []
        for list_id in LIST_IDS:
            results = await reconcile_list(client, list_id)
            all_results.extend(results)

    total = len(all_results)
    log.info(
        "Reconciliador concluido -- %d task(s) com acoes%s.",
        total,
        " (simuladas)" if DRY_RUN else " aplicadas",
    )
    if DRY_RUN and total == 0:
        log.info("Nenhuma divergencia encontrada.")


if __name__ == "__main__":
    asyncio.run(main())

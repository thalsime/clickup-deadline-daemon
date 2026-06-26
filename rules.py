"""
rules.py
========
Nucleo compartilhado de regras, helpers de API e constantes do deadline daemon.

Importado por main.py (webhook) e reconcile.py (reconciliador), garantindo que
correccoes de logica (ex.: respeitar start_date) beneficiam ambos os fluxos
a partir de uma unica fonte.

Nao tem side effects no import -- ao contrario de main.py, nao le variaveis de
ambiente fora de funcoes. O chamador e responsavel por passar CLICKUP_API_TOKEN,
MS_PER_DAY, TRIGGER_STATUSES, TARGET_STATUS e PENDING_STATUSES.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("deadline-daemon.rules")

CLICKUP_API_BASE = "https://api.clickup.com/api/v2"


# ---------------------------------------------------------------------------
# Helpers -- API ClickUp
# ---------------------------------------------------------------------------

async def get_task(client: httpx.AsyncClient, task_id: str) -> dict:
    """Busca os detalhes completos de uma task."""
    resp = await client.get(
        f"{CLICKUP_API_BASE}/task/{task_id}",
        params={"include_subtasks": "true"},
    )
    if not resp.is_success:
        log.error(
            "ClickUp GET /task/%s -> %d: %s",
            task_id, resp.status_code, resp.text[:500],
        )
    resp.raise_for_status()
    return resp.json()


async def get_list_tasks(
    client: httpx.AsyncClient,
    list_id: str,
    page: int = 0,
) -> dict:
    """
    Busca tasks de uma lista do ClickUp (paginado).
    Retorna o payload completo: {"tasks": [...], "last_page": bool}.
    """
    resp = await client.get(
        f"{CLICKUP_API_BASE}/list/{list_id}/task",
        params={
            "include_closed": "false",
            "subtasks":       "true",
            "page":           str(page),
        },
    )
    if not resp.is_success:
        log.error(
            "ClickUp GET /list/%s/task (page=%d) -> %d: %s",
            list_id, page, resp.status_code, resp.text[:500],
        )
    resp.raise_for_status()
    return resp.json()


async def set_due_date(client: httpx.AsyncClient, task_id: str, due_date_ms: int) -> None:
    """Define o due_date da task (timestamp em ms)."""
    resp = await client.put(
        f"{CLICKUP_API_BASE}/task/{task_id}",
        json={"due_date": due_date_ms},
    )
    if not resp.is_success:
        log.error(
            "ClickUp PUT /task/%s (due_date=%d) -> %d: %s",
            task_id, due_date_ms, resp.status_code, resp.text[:500],
        )
    resp.raise_for_status()


async def add_assignee(client: httpx.AsyncClient, task_id: str, user_id: int) -> None:
    """Adiciona um responsavel a task sem remover os existentes."""
    resp = await client.put(
        f"{CLICKUP_API_BASE}/task/{task_id}",
        json={"assignees": {"add": [user_id], "rem": []}},
    )
    if not resp.is_success:
        log.error(
            "ClickUp PUT /task/%s (assignee add) -> %d: %s",
            task_id, resp.status_code, resp.text[:500],
        )
    resp.raise_for_status()


async def set_status(client: httpx.AsyncClient, task_id: str, status: str) -> None:
    """Altera o status da task (nome exato do Space, case-sensitive na API)."""
    resp = await client.put(
        f"{CLICKUP_API_BASE}/task/{task_id}",
        json={"status": status},
    )
    if not resp.is_success:
        log.error(
            "ClickUp PUT /task/%s (status=%r) -> %d: %s",
            task_id, status, resp.status_code, resp.text[:500],
        )
    resp.raise_for_status()


async def post_comment(client: httpx.AsyncClient, task_id: str, text: str) -> None:
    """Posta um comentario na task (POST /task/{id}/comment), sem notificar todos."""
    resp = await client.post(
        f"{CLICKUP_API_BASE}/task/{task_id}/comment",
        json={"comment_text": text, "notify_all": False},
    )
    if not resp.is_success:
        log.error(
            "ClickUp POST /task/%s/comment -> %d: %s",
            task_id, resp.status_code, resp.text[:500],
        )
    resp.raise_for_status()


def fallback_comment_text(days: int) -> str:
    """Texto do comentario postado quando o prazo e definido pelo fallback (sem time_estimate)."""
    return (
        f"Prazo definido automaticamente com estimativa padrão de {days} dias úteis "
        "porque esta tarefa estava sem estimativa de esforço (time_estimate). "
        "Este prazo pode não refletir o esforço real - recomenda-se revisar a "
        "estimativa de esforço e ajustar o prazo, se necessário."
    )


# ---------------------------------------------------------------------------
# Helpers -- calculo de datas
# ---------------------------------------------------------------------------

def extract_estimate_days(task: dict, ms_per_day: int) -> int | None:
    """
    Le o campo nativo time_estimate (esforco em ms) e converte em dias uteis
    pela base ms_per_day, arredondando para cima.
    Retorna None se o campo nao existir, for nulo ou nao-positivo.
    """
    value = task.get("time_estimate")
    if value is None:
        return None
    try:
        estimate_ms = int(value)
    except (ValueError, TypeError):
        log.warning("Task %s: time_estimate invalido: %r", task.get("id"), value)
        return None
    if estimate_ms <= 0:
        return None
    return math.ceil(estimate_ms / ms_per_day)


def compute_due_date_ms(
    task: dict, ms_per_day: int, fallback_days: int | None = None
) -> int | None:
    """
    Calcula o timestamp ms da due_date a partir de time_estimate.

    A base de calculo e max(agora, start_date), garantindo due_date >= start_date.
    O ClickUp rejeita com 400 um PUT de due_date anterior ao start_date existente
    (confirmado em producao: task 86e1qtczy retornou 400 por este motivo).

    Sem time_estimate: retorna None, a menos que fallback_days (> 0) seja informado --
    nesse caso usa fallback_days como estimativa padrao (ver FALLBACK_ESTIMATE_DAYS).
    """
    estimate_days = extract_estimate_days(task, ms_per_day)
    if estimate_days is None:
        if not fallback_days or fallback_days <= 0:
            return None
        estimate_days = fallback_days
    now_utc = datetime.now(timezone.utc)
    # Respeitar start_date: due_date nunca pode ser anterior ao inicio da task.
    raw_start = task.get("start_date")
    try:
        start_dt = (
            datetime.fromtimestamp(int(raw_start) / 1000, tz=timezone.utc)
            if raw_start
            else None
        )
    except (ValueError, TypeError):
        start_dt = None
    base   = max(now_utc, start_dt) if start_dt else now_utc
    due_dt = base + timedelta(days=estimate_days)
    return int(due_dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Predicados
# ---------------------------------------------------------------------------

def due_date_is_set(task: dict) -> bool:
    """
    Retorna True se a task ja tem due_date definida.
    Trata due_date=0 e due_date="0" como "nao definida" (falsy-zero do ClickUp).
    """
    raw = task.get("due_date")
    return raw is not None and raw != 0 and raw != "0"


def is_supertask(task: dict) -> bool:
    """
    Retorna True se a task possui subtasks (e mae/supertask).

    Requer que a task tenha sido buscada com include_subtasks=true (o campo "subtasks"
    so vem preenchido nesse caso). Supertasks nao recebem prazo proprio nem fallback:
    o esforco real vive nas subtasks, e dar prazo a mae mascararia o rollup.
    """
    return bool(task.get("subtasks"))

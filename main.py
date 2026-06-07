"""
clickup-deadline-daemon  v2.0
==============================
Webhook receiver que:
  1. Calcula due_date quando uma task muda para "em progresso" ou recebe um assignee.
  2. Ao mover para o status de trigger sem responsável, atribui quem fez a mudança.
  3. Ao atribuir responsável numa task "pendente", promove para "em progresso".
  4. Audita toda e qualquer mudança em qualquer task (SQLite, dedup por history_item_id).

Anti-loop: usa conta de serviço dedicada. O daemon resolve o ID do dono do token no
startup (GET /api/v2/user) e ignora eventos onde o ator é ele mesmo -- mas sempre
audita. Se o TOKEN_OWNER_ID não for resolvido, adota postura conservadora: só audita,
não executa ações de escrita.

Regras de due_date:
  - Age somente se a task tiver time_estimate (esforço em ms) preenchido.
  - Converte em dias úteis pela base 4h/dia (MS_PER_DAY), arredondando para cima.
  - Não sobrescreve due_date já definida manualmente.
"""

import hashlib
import hmac
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import audit as _audit

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

CLICKUP_API_TOKEN = os.environ["CLICKUP_API_TOKEN"]
WEBHOOK_SECRET = os.environ.get("CLICKUP_WEBHOOK_SECRET", "")  # recomendado

# Milissegundos por dia útil (base 4h/dia). Override via env se a base mudar.
# Ver docs/clickup/webhook-deadline-daemon/README.md seção 9 para referência de valores.
MS_PER_DAY = int(os.environ.get("MS_PER_DAY", 14_400_000))

# Status que disparam as regras de due_date e atribuição (lowercase, case-insensitive).
# IMPORTANTE: os nomes devem bater com os status configurados no Space do ClickUp.
# Verificar em Settings -> Statuses. Exemplo no Space "Espaço da equipe": "em progresso".
TRIGGER_STATUSES = {
    s.strip().lower()
    for s in os.environ.get("TRIGGER_STATUSES", "em progresso,in progress").split(",")
    if s.strip()
}

# Nome canônico do status-alvo para o PUT da regra 2 (promover tarefa pendente).
# Deve bater exatamente com o nome no Space (case-sensitive na API do ClickUp).
TARGET_STATUS = os.environ.get("TARGET_STATUS", "em progresso")

# Status considerados "pendente" (lowercase) -- condição da regra 2.
PENDING_STATUSES = {
    s.strip().lower()
    for s in os.environ.get("PENDING_STATUSES", "pendente").split(",")
    if s.strip()
}

# Backend de auditoria e caminho do arquivo SQLite.
AUDIT_BACKEND = os.environ.get("AUDIT_BACKEND", "sqlite")
AUDIT_PATH    = os.environ.get("AUDIT_PATH", "/opt/clickup-deadline-daemon/audit.db")

# ---------------------------------------------------------------------------
# Estado global -- resolvido no startup via GET /api/v2/user
# ---------------------------------------------------------------------------

TOKEN_OWNER_ID: int | None = None

# ---------------------------------------------------------------------------
# Infraestrutura HTTP
# ---------------------------------------------------------------------------

CLICKUP_API_BASE = "https://api.clickup.com/api/v2"
HEADERS = {
    "Authorization": CLICKUP_API_TOKEN,
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("deadline-daemon")

# ---------------------------------------------------------------------------
# Helpers -- API ClickUp
# ---------------------------------------------------------------------------

async def get_token_owner_id(client: httpx.AsyncClient) -> int | None:
    """Descobre o ID do dono do token via GET /api/v2/user."""
    try:
        resp = await client.get(f"{CLICKUP_API_BASE}/user")
        resp.raise_for_status()
        return int(resp.json()["user"]["id"])
    except Exception as exc:
        log.error("Erro ao resolver token owner: %s", exc)
        return None


async def get_task(client: httpx.AsyncClient, task_id: str) -> dict:
    """Busca os detalhes completos de uma task, incluindo time_estimate e assignees."""
    resp = await client.get(
        f"{CLICKUP_API_BASE}/task/{task_id}",
        params={"include_subtasks": "false"},
    )
    resp.raise_for_status()
    return resp.json()


async def set_due_date(client: httpx.AsyncClient, task_id: str, due_date_ms: int) -> None:
    """Define o due_date da task (timestamp em ms)."""
    resp = await client.put(
        f"{CLICKUP_API_BASE}/task/{task_id}",
        json={"due_date": due_date_ms},
    )
    resp.raise_for_status()


async def add_assignee(client: httpx.AsyncClient, task_id: str, user_id: int) -> None:
    """Adiciona um responsável à task sem remover os existentes."""
    resp = await client.put(
        f"{CLICKUP_API_BASE}/task/{task_id}",
        json={"assignees": {"add": [user_id], "rem": []}},
    )
    resp.raise_for_status()


async def set_status(client: httpx.AsyncClient, task_id: str, status: str) -> None:
    """Altera o status da task (nome exato do Space, case-sensitive na API)."""
    resp = await client.put(
        f"{CLICKUP_API_BASE}/task/{task_id}",
        json={"status": status},
    )
    resp.raise_for_status()

# ---------------------------------------------------------------------------
# Helpers -- extração e predicados
# ---------------------------------------------------------------------------

def extract_estimate_days(task: dict) -> int | None:
    """
    Lê o campo nativo time_estimate (esforço em ms) e converte em dias úteis
    pela base MS_PER_DAY (4h/dia), arredondando para cima.
    Retorna None se o campo não existir, for nulo ou não-positivo.
    """
    value = task.get("time_estimate")
    if value is None:
        return None
    try:
        estimate_ms = int(value)
    except (ValueError, TypeError):
        log.warning("Task %s: time_estimate inválido: %r", task.get("id"), value)
        return None
    if estimate_ms <= 0:
        return None
    return math.ceil(estimate_ms / MS_PER_DAY)


def compute_due_date_ms(task: dict) -> int | None:
    """
    Calcula o timestamp ms da due_date a partir de time_estimate.
    Retorna None se time_estimate não estiver definido ou for inválido.
    """
    estimate_days = extract_estimate_days(task)
    if estimate_days is None:
        return None
    now_utc = datetime.now(timezone.utc)
    due_dt  = now_utc + timedelta(days=estimate_days)
    return int(due_dt.timestamp() * 1000)


def get_actor_id(event: dict) -> int | None:
    """Extrai o ID do usuário que executou a ação (history_items[0].user.id)."""
    try:
        raw_id = event["history_items"][0]["user"]["id"]
        return int(raw_id)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def is_self_action(event: dict) -> bool:
    """
    Verifica se a ação foi executada pelo próprio daemon (token owner).
    Retorna False se TOKEN_OWNER_ID não foi resolvido (conservador -- não filtra nada).
    """
    if TOKEN_OWNER_ID is None:
        return False
    actor_id = get_actor_id(event)
    return actor_id is not None and actor_id == TOKEN_OWNER_ID


def should_trigger_on_status(event: dict) -> bool:
    """Verifica se o evento é uma mudança de status para um dos status de trigger."""
    if event.get("event") != "taskStatusUpdated":
        return False
    new_status = (
        event.get("history_items", [{}])[0]
        .get("after", {})
        .get("status", "")
        .lower()
        .strip()
    )
    return new_status in TRIGGER_STATUSES


def should_trigger_on_assignee(event: dict) -> bool:
    """Verifica se o evento é uma atribuição de responsável."""
    return event.get("event") == "taskAssigneeUpdated"

# ---------------------------------------------------------------------------
# Lógica de due_date
# ---------------------------------------------------------------------------

async def apply_due_date(
    client: httpx.AsyncClient, task_id: str, task: dict
) -> dict:
    """
    Aplica due_date calculado, respeitando a regra de não sobrescrever.

    Returns:
        dict com chave "action": "due_date_set" | "skipped" e detalhes.
    """
    task_name = task.get("name", "")

    if task.get("due_date"):
        log.info("Task %s ('%s'): já tem due_date -- ignorando.", task_id, task_name)
        return {"task_id": task_id, "action": "skipped", "reason": "due_date already set"}

    due_ms = compute_due_date_ms(task)
    if due_ms is None:
        log.info(
            "Task %s ('%s'): time_estimate não definido -- ignorando.", task_id, task_name
        )
        return {"task_id": task_id, "action": "skipped", "reason": "time_estimate not set"}

    await set_due_date(client, task_id, due_ms)
    due_dt        = datetime.fromtimestamp(due_ms / 1000, tz=timezone.utc)
    estimate_days = extract_estimate_days(task)
    log.info(
        "Task %s ('%s'): due_date definida para %s (+%d dias de esforço).",
        task_id,
        task_name,
        due_dt.strftime("%Y-%m-%d"),
        estimate_days or 0,
    )
    return {
        "task_id":    task_id,
        "action":     "due_date_set",
        "due_date":   due_dt.strftime("%Y-%m-%d"),
        "days_added": estimate_days,
    }

# ---------------------------------------------------------------------------
# Handlers por regra de negócio
# ---------------------------------------------------------------------------

async def handle_status_in_progress(
    client: httpx.AsyncClient, task_id: str, event: dict
) -> dict:
    """
    Handler para taskStatusUpdated -> status de trigger.

    Regra 1: calcula due_date (se não existir). Se a task não tiver nenhum
    responsável, atribui quem moveu o status.
    """
    task   = await get_task(client, task_id)
    result = await apply_due_date(client, task_id, task)

    actor_id  = get_actor_id(event)
    assignees = task.get("assignees") or []

    if actor_id is not None and not assignees:
        await add_assignee(client, task_id, actor_id)
        log.info(
            "Task %s: responsável atribuído automaticamente (actor_id=%d).",
            task_id,
            actor_id,
        )
        result["assignee_added"] = actor_id
    elif assignees:
        log.info(
            "Task %s: já tem %d responsável(is) -- skip atribuição automática.",
            task_id,
            len(assignees),
        )

    result["trigger"] = "status_changed"
    return result


async def handle_assignee_added(
    client: httpx.AsyncClient, task_id: str, event: dict
) -> dict:
    """
    Handler para taskAssigneeUpdated.

    Regra 2: calcula due_date (se não existir). Se a task estiver em um status
    "pendente", promove para TARGET_STATUS ("em progresso").
    A cascata gerada pelo set_status será bloqueada pelo anti-loop (actor = daemon).
    """
    task   = await get_task(client, task_id)
    result = await apply_due_date(client, task_id, task)

    current_status = task.get("status", {}).get("status", "").lower().strip()
    if current_status in PENDING_STATUSES:
        await set_status(client, task_id, TARGET_STATUS)
        log.info(
            "Task %s: status promovido de '%s' para '%s'.",
            task_id,
            current_status,
            TARGET_STATUS,
        )
        result["status_set"] = TARGET_STATUS
    else:
        log.info(
            "Task %s: status '%s' não é pendente -- skip promoção.",
            task_id,
            current_status,
        )

    result["trigger"] = "assignee_added"
    return result

# ---------------------------------------------------------------------------
# Lifespan (startup)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global TOKEN_OWNER_ID
    async with httpx.AsyncClient(headers=HEADERS, timeout=10.0) as client:
        TOKEN_OWNER_ID = await get_token_owner_id(client)
    if TOKEN_OWNER_ID is not None:
        log.info(
            "Token owner resolvido: id=%d -- ações do daemon serão ignoradas no roteamento.",
            TOKEN_OWNER_ID,
        )
    else:
        log.warning(
            "Não foi possível resolver o token owner -- postura conservadora ativa: "
            "só auditoria, sem ações de escrita até reiniciar."
        )
    _audit.init_audit(AUDIT_BACKEND, AUDIT_PATH)
    log.info("Auditoria ativa: backend=%s path=%s", AUDIT_BACKEND, AUDIT_PATH)
    yield

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="ClickUp Deadline Daemon", version="1.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Helpers -- assinatura
# ---------------------------------------------------------------------------

def verify_signature(body: bytes, signature: str) -> bool:
    """Verifica a assinatura HMAC-SHA256 do webhook (se WEBHOOK_SECRET definido)."""
    if not WEBHOOK_SECRET:
        return True
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "token_owner_id": TOKEN_OWNER_ID}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_signature: str = Header(default="", alias="X-Signature"),
):
    body = await request.body()

    # Verificar assinatura HMAC se WEBHOOK_SECRET definido
    if WEBHOOK_SECRET and not verify_signature(body, x_signature):
        log.warning("Webhook com assinatura inválida -- rejeitando.")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        event = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = event.get("event", "")
    task_id    = event.get("task_id")

    log.info("Webhook recebido: event=%s task_id=%s", event_type, task_id)

    if not task_id:
        return JSONResponse({"status": "ignored", "reason": "no task_id"})

    # 1. Auditar SEMPRE -- inclusive ações do próprio daemon (token owner)
    try:
        await _audit.audit_record(event, TOKEN_OWNER_ID)
    except Exception as exc:
        log.error("Erro ao auditar event=%s task=%s: %s", event_type, task_id, exc)

    # 2. Anti-loop: ignorar ações do próprio daemon no roteamento (auditoria já feita)
    if is_self_action(event):
        log.debug("self-action ignorada: event=%s task=%s", event_type, task_id)
        return JSONResponse({"status": "ignored", "reason": "self-action"})

    # 3. Postura conservadora: sem TOKEN_OWNER_ID resolvido, só auditou -- não age
    if TOKEN_OWNER_ID is None:
        log.warning(
            "TOKEN_OWNER_ID não resolvido -- auditado sem ação: event=%s task=%s",
            event_type,
            task_id,
        )
        return JSONResponse({
            "status": "audited",
            "reason": "token owner unresolved, no action taken",
        })

    # 4. Roteamento -- sempre retornar 200 (5xx faz o ClickUp desabilitar o webhook)
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=10.0) as client:
            if should_trigger_on_status(event):
                result = await handle_status_in_progress(client, task_id, event)
            elif should_trigger_on_assignee(event):
                result = await handle_assignee_added(client, task_id, event)
            else:
                return JSONResponse({
                    "status": "ignored",
                    "reason": f"event '{event_type}' not handled",
                })
    except Exception as exc:
        log.error("Erro ao processar event=%s task=%s: %s", event_type, task_id, exc)
        return JSONResponse({"status": "error", "reason": str(exc)})

    return JSONResponse(result)

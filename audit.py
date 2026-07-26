"""
audit.py
========
Auditoria de eventos ClickUp em SQLite.

Grava uma linha por history_item recebido. Usa INSERT OR IGNORE por
history_item_id para garantir idempotência em caso de reentrega do webhook.

Dependências: apenas stdlib (sqlite3, asyncio, json).
"""

import asyncio
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone

log = logging.getLogger("deadline-daemon.audit")

# Conexão global -- inicializada em init_audit() no startup do daemon.
_conn: sqlite3.Connection | None = None

# Lock de escrita: SQLite com check_same_thread=False permite acesso concorrente,
# mas executemany() sem barreira causa "database is locked" sob rajadas de eventos
# simultâneos. O Lock garante que apenas uma thread grava por vez.
_lock = threading.Lock()

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    history_item_id TEXT    UNIQUE,
    received_at     TEXT    NOT NULL,
    event_date      TEXT,
    event_type      TEXT,
    task_id         TEXT,
    team_id         TEXT,
    field           TEXT,
    actor_id        INTEGER,
    actor_name      TEXT,
    actor_email     TEXT,
    before_value    TEXT,
    after_value     TEXT,
    is_self_action  INTEGER DEFAULT 0,
    daemon_action   TEXT,
    raw_payload     TEXT
)
"""

_CREATE_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_audit_task_id      ON audit_log(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_actor_id     ON audit_log(actor_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_received_at  ON audit_log(received_at)",
]

# Um ? por coluna, na mesma ordem do _CREATE_TABLE (exceto id autoincrement).
_INSERT = """
INSERT OR IGNORE INTO audit_log
    (history_item_id, received_at, event_date, event_type, task_id, team_id,
     field, actor_id, actor_name, actor_email,
     before_value, after_value, is_self_action, daemon_action, raw_payload)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def init_audit(backend: str, path: str) -> None:
    """
    Inicializa o backend de auditoria: cria o arquivo SQLite, a tabela e os índices.

    Deve ser chamado uma vez no startup do daemon (lifespan). Pode ser chamado novamente
    para reinicializar (ex.: em testes com tmp_path).

    Args:
        backend: identificador do backend; atualmente apenas "sqlite" é suportado.
        path:    caminho para o arquivo .db (criado se não existir).
    """
    global _conn
    if backend != "sqlite":
        log.warning("Backend '%s' não suportado -- usando sqlite mesmo assim.", backend)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute(_CREATE_TABLE)
    for idx in _CREATE_INDICES:
        _conn.execute(idx)
    _conn.commit()
    log.info("Auditoria SQLite inicializada em %s", path)


def _write_rows(rows: list[tuple]) -> None:
    """
    Escrita síncrona -- chamada via asyncio.to_thread para não bloquear o event loop.
    O _lock serializa gravações concorrentes e previne "database is locked" sob rajadas.
    """
    if _conn is None:
        raise RuntimeError("init_audit() não foi chamado antes de _write_rows()")
    with _lock, _conn:
        _conn.executemany(_INSERT, rows)


async def audit_record(
    event: dict,
    token_owner_id: int | None,
    daemon_action: str | None = None,
) -> None:
    """
    Grava uma linha por history_item no audit_log.

    INSERT OR IGNORE por history_item_id garante idempotência: reentregas do webhook
    com o mesmo history_item_id não duplicam o registro.

    Eventos sem history_items (borda rara) geram uma linha com history_item_id = NULL;
    NULLs não conflitam no UNIQUE (SQL standard), então esses podem duplicar -- aceitável.

    Args:
        event:          payload completo do webhook recebido.
        token_owner_id: ID do dono do token do daemon; usado para marcar is_self_action.
        daemon_action:  descrição da ação executada pelo daemon neste evento (ex.:
                        "due_date_set", "assignee_added"), ou None se nenhuma ação.
    """
    if _conn is None:
        log.warning("audit_record chamado antes de init_audit -- ignorando.")
        return

    received_at = datetime.now(timezone.utc).isoformat()
    event_type  = str(event.get("event") or "")
    task_id     = str(event.get("task_id") or "")
    team_id     = str(event.get("team_id") or "")
    raw_payload = json.dumps(event, ensure_ascii=False)

    history_items: list[dict] = event.get("history_items") or []
    rows: list[tuple] = []

    for item in history_items:
        item_id_raw     = item.get("id")
        history_item_id = str(item_id_raw) if item_id_raw else None  # NULL se ausente
        event_date      = str(item.get("date") or "")
        field           = str(item.get("field") or "")

        user     = item.get("user") or {}
        raw_uid  = user.get("id")
        try:
            actor_id = int(raw_uid) if raw_uid is not None else None
        except (ValueError, TypeError):
            actor_id = None
        actor_name  = str(user.get("username") or "")
        actor_email = str(user.get("email")    or "")

        before_raw   = item.get("before")
        after_raw    = item.get("after")
        before_value = json.dumps(before_raw, ensure_ascii=False) if before_raw is not None else None
        after_value  = json.dumps(after_raw,  ensure_ascii=False) if after_raw  is not None else None

        is_self = int(
            token_owner_id is not None
            and actor_id is not None
            and actor_id == token_owner_id
        )

        rows.append((
            history_item_id,
            received_at,
            event_date,
            event_type,
            task_id,
            team_id,
            field,
            actor_id,
            actor_name,
            actor_email,
            before_value,
            after_value,
            is_self,
            daemon_action,
            raw_payload,
        ))

    if not rows:
        # Evento sem history_items: gravar registro mínimo para cobertura total de auditoria.
        rows.append((
            None,           # history_item_id -- NULL permite múltiplos
            received_at,
            "",             # event_date
            event_type,
            task_id,
            team_id,
            "",             # field
            None, None, None,   # actor_id, actor_name, actor_email
            None, None,         # before_value, after_value
            0,              # is_self_action
            daemon_action,
            raw_payload,
        ))

    await asyncio.to_thread(_write_rows, rows)


def _update_action(history_item_ids: list[str], action: str) -> None:
    """Escrita síncrona -- atualiza daemon_action nas linhas já inseridas."""
    if _conn is None:
        return
    with _lock, _conn:
        _conn.executemany(
            "UPDATE audit_log SET daemon_action = ? WHERE history_item_id = ?",
            [(action, hid) for hid in history_item_ids],
        )


async def audit_set_daemon_action(history_item_ids: list[str], action: str) -> None:
    """
    Atualiza a coluna daemon_action nas linhas já gravadas pelo audit_record.

    Deve ser chamado após o roteamento (quando a ação do daemon é conhecida).
    Usa o mesmo _lock de _write_rows para evitar colisão de transações.

    Args:
        history_item_ids: IDs dos history_items do evento roteado.
        action:           rótulo da ação executada (ex.: "due_date_set", "skipped").
    """
    if not history_item_ids:
        return
    await asyncio.to_thread(_update_action, history_item_ids, action)

"""
Testes unitários do deadline daemon.

Rodar com:
  export CLICKUP_API_TOKEN=pk_dummy_test_token
  pytest test_main.py -v
"""

import os
import sqlite3
import sys

import httpx
import pytest

# Adicionar o diretório do daemon ao sys.path para import local de 'audit'
_DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
if _DAEMON_DIR not in sys.path:
    sys.path.insert(0, _DAEMON_DIR)

# Definir variável antes de importar o módulo (o import lê CLICKUP_API_TOKEN no topo)
os.environ.setdefault("CLICKUP_API_TOKEN", "pk_dummy_test_token")

import audit as _audit
import main

# Re-exportar para continuidade dos testes existentes
from main import (
    extract_estimate_days,
    should_trigger_on_assignee,
    should_trigger_on_status,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture()
def fresh_audit_db(tmp_path, monkeypatch):
    """Inicializa um audit.db isolado para cada teste de auditoria."""
    db_path = str(tmp_path / "test_audit.db")
    _audit.init_audit("sqlite", db_path)
    yield db_path
    # Fechar conexão ao fim do teste para liberar o arquivo temporário
    if _audit._conn:
        _audit._conn.close()
        monkeypatch.setattr(_audit, "_conn", None)


# ===========================================================================
# Helpers de factory
# ===========================================================================

def make_task(time_estimate, assignees=None, due_date=None, status="pendente") -> dict:
    """Cria um dict de task com os campos relevantes."""
    return {
        "id":            "task-001",
        "name":          "Teste",
        "time_estimate": time_estimate,
        "assignees":     assignees if assignees is not None else [],
        "due_date":      due_date,
        "status":        {"status": status},
    }


def make_status_event(new_status: str, actor_id: int = 111111) -> dict:
    return {
        "event":   "taskStatusUpdated",
        "task_id": "task-001",
        "history_items": [{
            "id":    "item-001",
            "date":  "1748000000000",
            "field": "status",
            "user":  {
                "id":       actor_id,
                "username": "Alice",
                "email":    "alice@ex.com",
            },
            "before": {"status": "pendente"},
            "after":  {"status": new_status},
        }],
    }


def make_assignee_event(actor_id: int = 111111, assigned_id: int = 111111) -> dict:
    return {
        "event":   "taskAssigneeUpdated",
        "task_id": "task-001",
        "history_items": [{
            "id":    "item-002",
            "date":  "1748000000001",
            "field": "assignee_add",
            "user":  {
                "id":       actor_id,
                "username": "Alice",
                "email":    "alice@ex.com",
            },
            "after": {
                "id":       assigned_id,
                "username": "Bob",
                "email":    "bob@ex.com",
            },
        }],
    }


# ===========================================================================
# extract_estimate_days
# ===========================================================================

def test_1_dia_exato():
    assert extract_estimate_days(make_task(14_400_000)) == 1


def test_3_dias_exatos():
    assert extract_estimate_days(make_task(43_200_000)) == 3


def test_5_dias_exatos():
    assert extract_estimate_days(make_task(72_000_000)) == 5


def test_arredonda_pra_cima():
    # 14400001 ms > 1 dia -> deve retornar 2
    assert extract_estimate_days(make_task(14_400_001)) == 2


def test_retorna_none_sem_estimate():
    assert extract_estimate_days(make_task(None)) is None


def test_retorna_none_com_zero():
    assert extract_estimate_days(make_task(0)) is None


def test_retorna_none_com_negativo():
    assert extract_estimate_days(make_task(-1000)) is None


def test_retorna_none_com_valor_invalido():
    assert extract_estimate_days(make_task("abc")) is None


# ===========================================================================
# should_trigger_on_status
# ===========================================================================

def test_trigger_status_em_progresso():
    # "em progresso" é o status padrão de trigger
    assert should_trigger_on_status(make_status_event("em progresso")) is True


def test_trigger_status_in_progress():
    assert should_trigger_on_status(make_status_event("In Progress")) is True


def test_nao_trigger_status_pendente():
    assert should_trigger_on_status(make_status_event("pendente")) is False


def test_nao_trigger_evento_diferente():
    event = {"event": "taskCreated", "task_id": "task-001"}
    assert should_trigger_on_status(event) is False


# ===========================================================================
# should_trigger_on_assignee
# ===========================================================================

def test_trigger_assignee():
    event = {"event": "taskAssigneeUpdated", "task_id": "task-001"}
    assert should_trigger_on_assignee(event) is True


def test_nao_trigger_assignee_outro_evento():
    event = {"event": "taskStatusUpdated", "task_id": "task-001"}
    assert should_trigger_on_assignee(event) is False


# ===========================================================================
# get_actor_id
# ===========================================================================

def test_get_actor_id_ok():
    event = make_status_event("em progresso", actor_id=111111)
    assert main.get_actor_id(event) == 111111


def test_get_actor_id_sem_history_items():
    event = {"event": "taskStatusUpdated", "task_id": "task-001", "history_items": []}
    assert main.get_actor_id(event) is None


def test_get_actor_id_sem_user():
    event = {
        "event": "taskStatusUpdated",
        "history_items": [{"after": {"status": "em progresso"}}],
    }
    assert main.get_actor_id(event) is None


def test_get_actor_id_campo_ausente():
    assert main.get_actor_id({}) is None


# ===========================================================================
# is_self_action
# ===========================================================================

def test_is_self_action_match(monkeypatch):
    monkeypatch.setattr(main, "TOKEN_OWNER_ID", 222222)
    event = make_status_event("em progresso", actor_id=222222)
    assert main.is_self_action(event) is True


def test_is_self_action_no_match(monkeypatch):
    monkeypatch.setattr(main, "TOKEN_OWNER_ID", 222222)
    event = make_status_event("em progresso", actor_id=111111)
    assert main.is_self_action(event) is False


def test_is_self_action_owner_none(monkeypatch):
    monkeypatch.setattr(main, "TOKEN_OWNER_ID", None)
    event = make_status_event("em progresso", actor_id=222222)
    # owner desconhecido -> False (conservador, não filtra nada)
    assert main.is_self_action(event) is False


# ===========================================================================
# compute_due_date_ms
# ===========================================================================

def test_compute_due_date_ms_retorna_none_sem_estimate():
    assert main.compute_due_date_ms(make_task(None)) is None


def test_compute_due_date_ms_retorna_inteiro_positivo():
    result = main.compute_due_date_ms(make_task(14_400_000))
    assert isinstance(result, int)
    assert result > 0


def test_compute_due_date_ms_cresce_com_estimate():
    ms_1d = main.compute_due_date_ms(make_task(14_400_000))
    ms_3d = main.compute_due_date_ms(make_task(43_200_000))
    assert ms_1d is not None and ms_3d is not None
    assert ms_3d > ms_1d


# ===========================================================================
# Predicado "pendente" (case-insensitive via PENDING_STATUSES)
# ===========================================================================

def test_pending_status_lower():
    assert "pendente" in main.PENDING_STATUSES


def test_pending_status_case_insensitive():
    # A comparação no handler normaliza .lower().strip()
    current = "PENDENTE"
    assert current.lower().strip() in main.PENDING_STATUSES


# ===========================================================================
# Regra 1: handle_status_in_progress
# ===========================================================================

@pytest.mark.asyncio
async def test_regra1_atribui_se_sem_responsavel(monkeypatch):
    """Regra 1: task sem responsável -> atribui quem moveu o status."""
    task = make_task(28_800_000, assignees=[], due_date=None)
    event = make_status_event("em progresso", actor_id=111111)

    calls: dict = {"set_due_date": [], "add_assignee": []}

    async def mock_get_task(client, task_id):
        return task

    async def mock_set_due_date(client, task_id, due_date_ms):
        calls["set_due_date"].append((task_id, due_date_ms))

    async def mock_add_assignee(client, task_id, user_id):
        calls["add_assignee"].append((task_id, user_id))

    monkeypatch.setattr(main, "get_task",     mock_get_task)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "add_assignee", mock_add_assignee)
    monkeypatch.setattr(main, "TOKEN_OWNER_ID", 222222)

    async with httpx.AsyncClient() as client:
        result = await main.handle_status_in_progress(client, "task-001", event)

    assert result.get("assignee_added") == 111111
    assert ("task-001", 111111) in calls["add_assignee"]
    assert len(calls["set_due_date"]) == 1  # due_date foi calculada e aplicada


@pytest.mark.asyncio
async def test_regra1_skip_se_com_responsavel(monkeypatch):
    """Regra 1: task JÁ com responsável -> não sobrescreve assignee."""
    task = make_task(28_800_000, assignees=[{"id": 999}], due_date=None)
    event = make_status_event("em progresso", actor_id=111111)

    calls: dict = {"add_assignee": []}

    async def mock_get_task(client, task_id):
        return task

    async def mock_set_due_date(client, task_id, due_date_ms):
        pass

    async def mock_add_assignee(client, task_id, user_id):
        calls["add_assignee"].append(user_id)

    monkeypatch.setattr(main, "get_task",     mock_get_task)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "add_assignee", mock_add_assignee)
    monkeypatch.setattr(main, "TOKEN_OWNER_ID", 222222)

    async with httpx.AsyncClient() as client:
        result = await main.handle_status_in_progress(client, "task-001", event)

    assert "assignee_added" not in result
    assert calls["add_assignee"] == []


# ===========================================================================
# Regra 2: handle_assignee_added
# ===========================================================================

@pytest.mark.asyncio
async def test_regra2_promove_se_pendente(monkeypatch):
    """Regra 2: task 'pendente' recebe assignee -> promove para TARGET_STATUS."""
    task  = make_task(28_800_000, assignees=[], due_date=None, status="pendente")
    event = make_assignee_event(actor_id=111111, assigned_id=111111)

    calls: dict = {"set_status": []}

    async def mock_get_task(client, task_id):
        return task

    async def mock_set_due_date(client, task_id, due_date_ms):
        pass

    async def mock_set_status(client, task_id, status):
        calls["set_status"].append((task_id, status))

    monkeypatch.setattr(main, "get_task",     mock_get_task)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "set_status",   mock_set_status)
    monkeypatch.setattr(main, "TOKEN_OWNER_ID", 222222)

    async with httpx.AsyncClient() as client:
        result = await main.handle_assignee_added(client, "task-001", event)

    assert result.get("status_set") == main.TARGET_STATUS
    assert ("task-001", main.TARGET_STATUS) in calls["set_status"]


@pytest.mark.asyncio
async def test_regra2_skip_se_nao_pendente(monkeypatch):
    """Regra 2: task em status diferente de 'pendente' -> não muda status."""
    task  = make_task(28_800_000, assignees=[], due_date=None, status="em progresso")
    event = make_assignee_event(actor_id=111111)

    calls: dict = {"set_status": []}

    async def mock_get_task(client, task_id):
        return task

    async def mock_set_due_date(client, task_id, due_date_ms):
        pass

    async def mock_set_status(client, task_id, status):
        calls["set_status"].append(status)

    monkeypatch.setattr(main, "get_task",     mock_get_task)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "set_status",   mock_set_status)

    async with httpx.AsyncClient() as client:
        result = await main.handle_assignee_added(client, "task-001", event)

    assert "status_set" not in result
    assert calls["set_status"] == []


# ===========================================================================
# Auditoria -- dedup por history_item_id (SQLite)
# ===========================================================================

@pytest.mark.asyncio
async def test_audit_dedup_mesmo_history_item_id(fresh_audit_db):
    """INSERT OR IGNORE garante que reentregas do mesmo history_item_id não duplicam."""
    event = {
        "event":   "taskStatusUpdated",
        "task_id": "task-dedup-abc",
        "team_id": "WORKSPACE_ID",
        "history_items": [{
            "id":    "item-dedup-001",
            "date":  "1748000000000",
            "field": "status",
            "user":  {"id": 100, "username": "Alice", "email": "alice@ex.com"},
            "before": {"status": "pendente"},
            "after":  {"status": "em progresso"},
        }],
    }

    # Simular reentrega: mesmo evento enviado duas vezes
    await _audit.audit_record(event, token_owner_id=None)
    await _audit.audit_record(event, token_owner_id=None)

    conn   = sqlite3.connect(fresh_audit_db)
    count  = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE history_item_id = 'item-dedup-001'"
    ).fetchone()[0]
    conn.close()

    assert count == 1, f"INSERT OR IGNORE falhou: esperado 1 linha, encontrado {count}"


@pytest.mark.asyncio
async def test_audit_marca_is_self_action(fresh_audit_db):
    """is_self_action = 1 quando o ator é o token owner."""
    event = {
        "event":   "taskStatusUpdated",
        "task_id": "task-self",
        "history_items": [{
            "id":    "item-self-001",
            "date":  "1748000000002",
            "field": "status",
            "user":  {"id": 222222, "username": "Conta de Serviço", "email": "svc@ex.com"},
            "after": {"status": "em progresso"},
        }],
    }

    await _audit.audit_record(event, token_owner_id=222222)

    conn = sqlite3.connect(fresh_audit_db)
    row  = conn.execute(
        "SELECT is_self_action FROM audit_log WHERE history_item_id = 'item-self-001'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 1, "is_self_action deveria ser 1 para evento do próprio daemon"


@pytest.mark.asyncio
async def test_audit_nao_marca_is_self_action_para_humano(fresh_audit_db):
    """is_self_action = 0 quando o ator é diferente do token owner."""
    event = {
        "event":   "taskStatusUpdated",
        "task_id": "task-human",
        "history_items": [{
            "id":    "item-human-001",
            "date":  "1748000000003",
            "field": "status",
            "user":  {"id": 111111, "username": "UsuarioTeste", "email": "t@ex.com"},
            "after": {"status": "em progresso"},
        }],
    }

    await _audit.audit_record(event, token_owner_id=222222)

    conn = sqlite3.connect(fresh_audit_db)
    row  = conn.execute(
        "SELECT is_self_action FROM audit_log WHERE history_item_id = 'item-human-001'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 0, "is_self_action deveria ser 0 para ação humana"

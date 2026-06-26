"""
Testes unitários do deadline daemon.

Rodar com:
  export CLICKUP_API_TOKEN=pk_dummy_test_token
  pytest test_main.py -v
"""

import asyncio
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
os.environ.setdefault("RECONCILE_LIST_IDS", "test-list-id")

import audit as _audit  # noqa: E402
import main  # noqa: E402
import reconcile  # noqa: E402

# Re-exportar para continuidade dos testes existentes
from main import (  # noqa: E402
    extract_estimate_days,
    should_trigger_on_assignee,
    should_trigger_on_status,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture()
def fresh_audit_db(tmp_path, monkeypatch):
    """
    Inicializa um audit.db isolado para cada teste de auditoria.

    Guarda a conexão e o lock originais antes de init_audit e os restaura no
    teardown -- evita que a conexão fechada com o tmp_path vaze para outros testes.
    """
    prev_conn = _audit._conn
    prev_lock = _audit._lock

    db_path = str(tmp_path / "test_audit.db")
    _audit.init_audit("sqlite", db_path)
    yield db_path

    # Fechar a conexão do teste e restaurar o estado anterior.
    if _audit._conn and _audit._conn is not prev_conn:
        _audit._conn.close()
    monkeypatch.setattr(_audit, "_conn", prev_conn)
    monkeypatch.setattr(_audit, "_lock", prev_lock)


# ===========================================================================
# Helpers de factory
# ===========================================================================

def make_task(time_estimate, assignees=None, due_date=None, status="pendente", subtasks=None) -> dict:
    """Cria um dict de task com os campos relevantes."""
    task = {
        "id":            "task-001",
        "name":          "Teste",
        "time_estimate": time_estimate,
        "assignees":     assignees if assignees is not None else [],
        "due_date":      due_date,
        "status":        {"status": status},
    }
    if subtasks is not None:
        task["subtasks"] = subtasks
    return task


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

def make_assignee_remove_event(actor_id: int = 111111, removed_id: int = 111111) -> dict:
    return {
        "event":   "taskAssigneeUpdated",
        "task_id": "task-001",
        "history_items": [{
            "id":    "item-003",
            "date":  "1748000000002",
            "field": "assignee_rem",
            "user":  {
                "id":       actor_id,
                "username": "Alice",
                "email":    "alice@ex.com",
            },
            "before": {
                "id":       removed_id,
                "username": "Bob",
                "email":    "bob@ex.com",
            },
            "after": None,
        }],
    }


def test_trigger_assignee_adicao_com_field():
    assert should_trigger_on_assignee(make_assignee_event()) is True


def test_trigger_assignee_adicao_sem_field_mas_com_after():
    event = {
        "event":   "taskAssigneeUpdated",
        "task_id": "task-001",
        "history_items": [{"after": {"id": 111111, "username": "Bob"}}],
    }
    assert should_trigger_on_assignee(event) is True


def test_nao_trigger_assignee_remocao_com_field():
    assert should_trigger_on_assignee(make_assignee_remove_event()) is False


def test_nao_trigger_assignee_remocao_sem_field_after_nulo():
    event = {
        "event":   "taskAssigneeUpdated",
        "task_id": "task-001",
        "history_items": [{"before": {"id": 111111, "username": "Bob"}, "after": None}],
    }
    assert should_trigger_on_assignee(event) is False


def test_nao_trigger_assignee_remocao_sem_field_after_dict_vazio():
    # Caso real: ClickUp pode enviar after={} em remoções (sem 'id')
    event = {
        "event":   "taskAssigneeUpdated",
        "task_id": "task-001",
        "history_items": [{"before": {"id": 111111, "username": "Bob"}, "after": {}}],
    }
    assert should_trigger_on_assignee(event) is False


def test_nao_trigger_assignee_sem_history_items():
    event = {"event": "taskAssigneeUpdated", "task_id": "task-001"}
    assert should_trigger_on_assignee(event) is False


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
# apply_due_date: guarda de due_date="0" (D2)
# ===========================================================================

@pytest.mark.asyncio
async def test_apply_due_date_zero_string_nao_e_prazo_definido(monkeypatch):
    """due_date='0' deve ser tratado como ausente; prazo deve ser calculado e aplicado."""
    task = make_task(14_400_000, assignees=[], due_date="0")
    chamadas: list = []

    async def mock_set_due_date(client, task_id, due_date_ms):
        chamadas.append(due_date_ms)

    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)

    async with httpx.AsyncClient() as client:
        result = await main.apply_due_date(client, "task-001", task)

    assert result.get("action") == "due_date_set", (
        "due_date='0' foi incorretamente interpretado como prazo ja definido"
    )
    assert len(chamadas) == 1


@pytest.mark.asyncio
async def test_apply_due_date_zero_int_nao_e_prazo_definido(monkeypatch):
    """due_date=0 (inteiro) deve ser tratado como ausente; prazo deve ser aplicado."""
    task = make_task(14_400_000, assignees=[], due_date=0)
    chamadas: list = []

    async def mock_set_due_date(client, task_id, due_date_ms):
        chamadas.append(due_date_ms)

    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)

    async with httpx.AsyncClient() as client:
        result = await main.apply_due_date(client, "task-001", task)

    assert result.get("action") == "due_date_set"
    assert len(chamadas) == 1


# ===========================================================================
# apply_due_date: fallback de estimativa (v1.2.0)
# ===========================================================================

@pytest.mark.asyncio
async def test_apply_due_date_fallback_sem_estimate(monkeypatch):
    """Sem time_estimate + fallback habilitado: grava prazo e comenta (due_date_set_fallback)."""
    task = make_task(None, assignees=[], due_date=None)
    dues: list = []
    comments: list = []

    async def mock_set_due_date(client, task_id, due_date_ms):
        dues.append(due_date_ms)

    async def mock_post_comment(client, task_id, text):
        comments.append(text)

    monkeypatch.setattr(main, "FALLBACK_ESTIMATE_DAYS", 2)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "post_comment", mock_post_comment)

    async with httpx.AsyncClient() as client:
        result = await main.apply_due_date(client, "task-001", task)

    assert result.get("action") == "due_date_set_fallback"
    assert result.get("days_added") == 2
    assert len(dues) == 1
    assert len(comments) == 1 and "2 dias" in comments[0]


@pytest.mark.asyncio
async def test_apply_due_date_fallback_desativado(monkeypatch):
    """Sem time_estimate e FALLBACK_ESTIMATE_DAYS=0: volta ao skip, sem prazo nem comentario."""
    task = make_task(None, assignees=[], due_date=None)
    dues: list = []
    comments: list = []

    async def mock_set_due_date(client, task_id, due_date_ms):
        dues.append(due_date_ms)

    async def mock_post_comment(client, task_id, text):
        comments.append(text)

    monkeypatch.setattr(main, "FALLBACK_ESTIMATE_DAYS", 0)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "post_comment", mock_post_comment)

    async with httpx.AsyncClient() as client:
        result = await main.apply_due_date(client, "task-001", task)

    assert result.get("action") == "skipped"
    assert result.get("reason") == "time_estimate not set"
    assert dues == [] and comments == []


@pytest.mark.asyncio
async def test_apply_due_date_com_estimate_nao_comenta(monkeypatch):
    """Com time_estimate: caminho normal (due_date_set), sem comentario de fallback."""
    task = make_task(14_400_000, assignees=[], due_date=None)
    comments: list = []

    async def mock_set_due_date(client, task_id, due_date_ms):
        pass

    async def mock_post_comment(client, task_id, text):
        comments.append(text)

    monkeypatch.setattr(main, "FALLBACK_ESTIMATE_DAYS", 2)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "post_comment", mock_post_comment)

    async with httpx.AsyncClient() as client:
        result = await main.apply_due_date(client, "task-001", task)

    assert result.get("action") == "due_date_set"
    assert comments == []


@pytest.mark.asyncio
async def test_apply_due_date_ja_definido_nao_comenta(monkeypatch):
    """due_date ja definido: skip, sem set_due_date nem comentario (mesmo sem time_estimate)."""
    task = make_task(None, assignees=[], due_date=1782223260000)
    dues: list = []
    comments: list = []

    async def mock_set_due_date(client, task_id, due_date_ms):
        dues.append(due_date_ms)

    async def mock_post_comment(client, task_id, text):
        comments.append(text)

    monkeypatch.setattr(main, "FALLBACK_ESTIMATE_DAYS", 2)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "post_comment", mock_post_comment)

    async with httpx.AsyncClient() as client:
        result = await main.apply_due_date(client, "task-001", task)

    assert result.get("action") == "skipped"
    assert result.get("reason") == "due_date already set"
    assert dues == [] and comments == []


def test_compute_due_date_ms_fallback_quando_sem_estimate():
    """compute_due_date_ms com fallback_days retorna prazo mesmo sem time_estimate."""
    assert main.compute_due_date_ms(make_task(None)) is None
    via_fallback = main.compute_due_date_ms(make_task(None), fallback_days=2)
    assert isinstance(via_fallback, int) and via_fallback > 0


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


# ===========================================================================
# A1: compute_due_date_ms respeita start_date
# ===========================================================================

def test_compute_due_date_ms_sem_start_date():
    """Sem start_date, a base é agora (comportamento original)."""
    task = make_task(14_400_000)   # 1 dia
    result = main.compute_due_date_ms(task)
    assert result is not None and result > 0


def test_compute_due_date_ms_start_date_futuro_e_base():
    """
    Com start_date no futuro, due_date deve ser >= start_date + estimativa,
    não agora + estimativa. Garante que o PUT não retorna 400 por due < start.
    """
    import time
    agora_ms  = int(time.time() * 1000)
    # start_date daqui a 10 dias
    start_ms  = agora_ms + 10 * 86_400_000
    task      = {
        "id":            "task-start",
        "name":          "Teste start_date",
        "time_estimate": 14_400_000,   # 1 dia
        "assignees":     [],
        "due_date":      None,
        "status":        {"status": "pendente"},
        "start_date":    str(start_ms),
    }
    due_ms = main.compute_due_date_ms(task)
    assert due_ms is not None
    # due_date deve ser pelo menos start_date (base >= start_date)
    assert due_ms >= start_ms, (
        f"due_date ({due_ms}) deve ser >= start_date ({start_ms})"
    )


def test_compute_due_date_ms_start_date_passado_nao_recua():
    """Com start_date no passado, a base continua sendo agora (max)."""
    import time
    agora_ms  = int(time.time() * 1000)
    start_ms  = agora_ms - 5 * 86_400_000   # 5 dias atrás
    task      = {
        "id":            "task-start-past",
        "name":          "Teste start passado",
        "time_estimate": 14_400_000,
        "assignees":     [],
        "due_date":      None,
        "status":        {"status": "pendente"},
        "start_date":    str(start_ms),
    }
    due_ms = main.compute_due_date_ms(task)
    assert due_ms is not None
    assert due_ms >= agora_ms, "Due no passado não faz sentido"


def test_compute_due_date_ms_start_date_invalido_nao_quebra():
    """start_date inválido (string não-numérica) não deve lançar exceção."""
    task = {
        "id":            "task-bad-start",
        "name":          "Teste",
        "time_estimate": 14_400_000,
        "assignees":     [],
        "due_date":      None,
        "status":        {"status": "pendente"},
        "start_date":    "nao-e-um-timestamp",
    }
    # Deve funcionar como se start_date não existisse
    result = main.compute_due_date_ms(task)
    assert result is not None and result > 0


# ===========================================================================
# A2: isolamento de falha em handlers (due_date falha -> assignee/status ainda ocorre)
# ===========================================================================

@pytest.mark.asyncio
async def test_regra1_assignee_ocorre_mesmo_com_falha_no_due_date(monkeypatch):
    """
    Regra 1: se set_due_date lançar (ex.: 400 do ClickUp por start_date),
    add_assignee ainda deve ser executado -- as ações são independentes.
    """
    task  = make_task(28_800_000, assignees=[], due_date=None)
    event = make_status_event("em progresso", actor_id=111111)

    calls: dict = {"add_assignee": []}

    async def mock_get_task(client, task_id):
        return task

    async def mock_set_due_date(client, task_id, due_date_ms):
        raise Exception("400 Bad Request -- due_date < start_date (simulado)")

    async def mock_add_assignee(client, task_id, user_id):
        calls["add_assignee"].append(user_id)

    monkeypatch.setattr(main, "get_task",     mock_get_task)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "add_assignee", mock_add_assignee)
    monkeypatch.setattr(main, "TOKEN_OWNER_ID", 222222)

    async with httpx.AsyncClient() as client:
        result = await main.handle_status_in_progress(client, "task-001", event)

    # Apesar do erro no due_date, o assignee deve ter sido atribuído.
    assert calls["add_assignee"] == [111111], "add_assignee deve ser chamado mesmo com falha em due_date"
    assert "due_date_error" in result, "Erro de due_date deve ser registrado no resultado"
    assert result.get("assignee_added") == 111111


@pytest.mark.asyncio
async def test_regra2_status_promovido_mesmo_com_falha_no_due_date(monkeypatch):
    """
    Regra 2: se set_due_date lançar, set_status ainda deve ser executado.
    """
    task  = make_task(28_800_000, assignees=[], due_date=None, status="pendente")
    event = make_assignee_event(actor_id=111111, assigned_id=111111)

    calls: dict = {"set_status": []}

    async def mock_get_task(client, task_id):
        return task

    async def mock_set_due_date(client, task_id, due_date_ms):
        raise Exception("400 Bad Request -- simulado")

    async def mock_set_status(client, task_id, status):
        calls["set_status"].append(status)

    monkeypatch.setattr(main, "get_task",     mock_get_task)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "set_status",   mock_set_status)
    monkeypatch.setattr(main, "TOKEN_OWNER_ID", 222222)

    async with httpx.AsyncClient() as client:
        result = await main.handle_assignee_added(client, "task-001", event)

    assert calls["set_status"] == [main.TARGET_STATUS], "set_status deve ser chamado mesmo com falha em due_date"
    assert "due_date_error" in result
    assert result.get("status_set") == main.TARGET_STATUS


# ===========================================================================
# C1: lock do SQLite sob gravações concorrentes
# ===========================================================================

@pytest.mark.asyncio
async def test_audit_lock_sob_concorrencia(fresh_audit_db):
    """
    Sob rajada de N gravações simultâneas, todas as linhas devem ser persistidas.
    Antes do _lock, ~50% das linhas se perdiam (confirmado em producao).
    """
    N = 20
    events = [
        {
            "event":   "taskStatusUpdated",
            "task_id": f"task-lock-{i}",
            "history_items": [{
                "id":    f"item-lock-{i:04d}",
                "date":  "1748000000000",
                "field": "status",
                "user":  {"id": 111111, "username": "Alice", "email": "a@ex.com"},
                "after": {"status": "em progresso"},
            }],
        }
        for i in range(N)
    ]

    # Disparar todas as gravações concorrentemente
    await asyncio.gather(*[_audit.audit_record(e, token_owner_id=None) for e in events])

    conn  = sqlite3.connect(fresh_audit_db)
    count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.close()

    assert count == N, (
        f"Esperado {N} linhas sob concorrência; gravado {count}. "
        f"Possível falha no _lock de escrita."
    )


# ===========================================================================
# C2: audit_set_daemon_action popula a coluna daemon_action
# ===========================================================================

@pytest.mark.asyncio
async def test_audit_set_daemon_action(fresh_audit_db):
    """daemon_action deve ser atualizado após o roteamento."""
    event = {
        "event":   "taskStatusUpdated",
        "task_id": "task-daction",
        "history_items": [{
            "id":    "item-daction-001",
            "date":  "1748000000010",
            "field": "status",
            "user":  {"id": 111111, "username": "Alice", "email": "a@ex.com"},
            "after": {"status": "em progresso"},
        }],
    }
    await _audit.audit_record(event, token_owner_id=None)
    await _audit.audit_set_daemon_action(["item-daction-001"], "due_date_set")

    conn = sqlite3.connect(fresh_audit_db)
    row  = conn.execute(
        "SELECT daemon_action FROM audit_log WHERE history_item_id = 'item-daction-001'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "due_date_set", f"daemon_action esperado 'due_date_set', obtido {row[0]!r}"


# ===========================================================================
# Hierarquia: supertasks nao recebem prazo proprio; subtasks sim (v1.3.0)
# ===========================================================================

def test_is_supertask_true_com_subtasks():
    assert main.is_supertask(make_task(None, subtasks=[{"id": "sub-1"}])) is True


def test_is_supertask_false_sem_subtasks():
    assert main.is_supertask(make_task(14_400_000)) is False
    assert main.is_supertask(make_task(14_400_000, subtasks=[])) is False


@pytest.mark.asyncio
async def test_apply_due_date_pula_supertask(monkeypatch):
    """Supertask (com subtasks) nao recebe due_date nem fallback."""
    task = make_task(None, due_date=None, subtasks=[{"id": "sub-1"}])
    dues, comments = [], []

    async def mock_set_due_date(client, task_id, ms):
        dues.append(ms)

    async def mock_post_comment(client, task_id, text):
        comments.append(text)

    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "post_comment", mock_post_comment)

    async with httpx.AsyncClient() as client:
        result = await main.apply_due_date(client, "task-001", task)

    assert result["action"] == "skipped"
    assert "supertask" in result["reason"]
    assert dues == []
    assert comments == []


@pytest.mark.asyncio
async def test_apply_due_date_task_plana_sem_estimate_usa_fallback(monkeypatch):
    """Task sem subtasks e sem time_estimate mantem o fallback (comportamento v1.2.0)."""
    task = make_task(None, due_date=None)  # sem subtasks
    dues, comments = [], []

    async def mock_set_due_date(client, task_id, ms):
        dues.append(ms)

    async def mock_post_comment(client, task_id, text):
        comments.append(text)

    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "post_comment", mock_post_comment)
    monkeypatch.setattr(main, "FALLBACK_ESTIMATE_DAYS", 2)

    async with httpx.AsyncClient() as client:
        result = await main.apply_due_date(client, "task-001", task)

    assert result["action"] == "due_date_set_fallback"
    assert len(dues) == 1
    assert len(comments) == 1


@pytest.mark.asyncio
async def test_handle_status_supertask_nao_seta_due_mas_atribui(monkeypatch):
    """Supertask em progresso: nao seta due_date, mas ainda atribui o ator (assignee)."""
    task  = make_task(None, assignees=[], due_date=None, subtasks=[{"id": "sub-1"}])
    event = make_status_event("em progresso", actor_id=111111)
    dues, assignees_added = [], []

    async def mock_get_task(client, task_id):
        return task

    async def mock_set_due_date(client, task_id, ms):
        dues.append(ms)

    async def mock_add_assignee(client, task_id, user_id):
        assignees_added.append(user_id)

    monkeypatch.setattr(main, "get_task",     mock_get_task)
    monkeypatch.setattr(main, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(main, "add_assignee", mock_add_assignee)
    monkeypatch.setattr(main, "TOKEN_OWNER_ID", 222222)

    async with httpx.AsyncClient() as client:
        await main.handle_status_in_progress(client, "task-001", event)

    assert dues == []                   # supertask nao recebe prazo proprio
    assert assignees_added == [111111]  # mas o ator e atribuido normalmente


@pytest.mark.asyncio
async def test_reconcile_task_pula_supertask(monkeypatch):
    """No reconciliador, supertask (is_supertask=True) nao recebe due_date nem fallback."""
    task = make_task(None, due_date=None, status="em progresso")
    dues = []

    async def mock_set_due_date(client, task_id, ms):
        dues.append(ms)

    monkeypatch.setattr(reconcile, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(reconcile, "DRY_RUN", False)

    async with httpx.AsyncClient() as client:
        result = await reconcile.reconcile_task(client, task, is_supertask=True)

    assert dues == []
    assert "due_date_set" not in result["actions"]
    assert "due_date_set_fallback" not in result["actions"]


@pytest.mark.asyncio
async def test_reconcile_task_subtask_recebe_due_date(monkeypatch):
    """Subtask (folha, is_supertask=False) em progresso com estimate recebe due_date."""
    task = make_task(14_400_000, due_date=None, status="em progresso")
    dues = []

    async def mock_set_due_date(client, task_id, ms):
        dues.append(ms)

    monkeypatch.setattr(reconcile, "set_due_date", mock_set_due_date)
    monkeypatch.setattr(reconcile, "DRY_RUN", False)

    async with httpx.AsyncClient() as client:
        result = await reconcile.reconcile_task(client, task, is_supertask=False)

    assert len(dues) == 1
    assert "due_date_set" in result["actions"]

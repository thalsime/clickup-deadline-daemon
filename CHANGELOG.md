# Changelog

Todas as mudanças relevantes deste projeto são documentadas aqui.

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/).
Versionamento segue [Semantic Versioning](https://semver.org/lang/pt-BR/).

---

## [Não lançado]

## [1.1.3] - 2026-06-08

### Corrigido

- `should_trigger_on_assignee`: o fallback para eventos sem o campo `field` agora
  exige `after.id` preenchido. O ClickUp pode enviar `after: {}` (dict vazio) em
  eventos de remoção de responsável; o check anterior (`after is not None`) deixava
  esse caso passar como adição, disparando a Regra 2 indevidamente.
- `handle_status_in_progress`: quando `actor_id` está ausente no evento e a task
  não possui responsável, o daemon agora emite `WARNING` explicitando que nenhum
  responsável foi atribuído (era falha silenciosa sem nenhum log).

### Adicionado

- `log.debug` do campo `field` e do valor `after` em todo evento
  `taskAssigneeUpdated`, facilitando diagnóstico de casos ambíguos.
- Teste `test_nao_trigger_assignee_remocao_sem_field_after_dict_vazio`: garante
  que `after: {}` é tratado como remoção (retorna `False`).

---

## [1.1.2] - 2026-06-08

### Corrigido

- `should_trigger_on_assignee`: eventos `taskAssigneeUpdated` de _remoção_ de
  responsável são agora ignorados corretamente. O ClickUp dispara esse evento tanto
  para adição quanto para remoção; a direção é determinada por
  `history_items[0].field` (`"assignee_add"` / `"assignee_rem"`). Quando o campo
  `field` está ausente, o fallback verifica se `after is not None`.

---

## [1.1.1] - 2026-06-08

### Corrigido

- `due_date_is_set` (extraído para `rules.py`): `due_date=0` e `due_date="0"` são
  agora tratados como "não definido". O ClickUp retorna `0` para tasks sem prazo;
  o check anterior (`if task.get("due_date")`) avaliava `0` como falsy e funcionava,
  mas a guarda explícita evita regressões futuras.
- `extract_estimate_days`: `time_estimate` ausente ou não-positivo agora emite
  `WARNING` em vez de falhar silenciosamente, ajudando a identificar tasks sem
  estimativa configurada.

---

## [1.1.0] - 2026-06-08

### Adicionado

- `rules.py`: módulo compartilhado com lógica de negócio e helpers de API
  (`compute_due_date_ms`, `extract_estimate_days`, `due_date_is_set`,
  `get_task`, `set_due_date`, `add_assignee`, `set_status`, `get_list_tasks`).
  Importado por `main.py` e `reconcile.py`, garantindo que correções de lógica
  beneficiam ambos os fluxos a partir de uma única fonte.
- `reconcile.py`: reconciliador idempotente que varre listas configuradas via
  `RECONCILE_LIST_IDS` e aplica invariantes sem depender de webhook:
  - task em status de trigger (padrão: `em progresso`) sem `due_date` e com
    `time_estimate` -> define o prazo;
  - task em status pendente com responsável -> promove para o status-alvo.
  Suporta `RECONCILE_DRY_RUN=1` para pré-visualização sem escrita.
- `deploy/reconcile.service` e `deploy/reconcile.timer`: unidades systemd oneshot
  + timer (`OnUnitActiveSec=10min`, `Persistent=true`) que executam o reconciliador
  automaticamente a cada 10 minutos.

### Corrigido

- `compute_due_date_ms`: o cálculo do prazo agora usa `max(agora, start_date)` como
  base, garantindo que `due_date >= start_date`. O ClickUp rejeita com HTTP 400
  qualquer `due_date` anterior ao `start_date` existente na task; este bug era a
  causa-raiz das falhas em tasks com data de início futura (ex.: tasks do
  sindivarejo-mobile).

---

## [1.0.0] - 2026-06-07

### Adicionado

- Receptor de webhooks FastAPI para o ClickUp (porta configurável, HTTPS via proxy
  reverso).
- **Regra 1** (`taskStatusUpdated` -> status de trigger): adiciona o ator do evento
  como responsável e define o prazo a partir de `time_estimate`.
- **Regra 2** (`taskAssigneeUpdated` -> adição de responsável): promove o status
  para o alvo e define o prazo a partir de `time_estimate`.
- **Regra 3**: auditoria de todos os eventos em banco SQLite (`audit.db`).
- Anti-loop: eventos cujo ator é a conta de serviço (`TOKEN_OWNER_ID`) são
  ignorados, evitando cascata infinita.
- Verificação de assinatura do webhook ClickUp (`WEBHOOK_SECRET`).
- `register_webhook.py`: script auxiliar para registrar o endpoint no workspace.
- Unidades systemd (`clickup-deadline-daemon.service`) para execução como serviço.

[Não lançado]: https://github.com/thalsime/clickup-deadline-daemon/compare/v1.1.3...HEAD
[1.1.3]: https://github.com/thalsime/clickup-deadline-daemon/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/thalsime/clickup-deadline-daemon/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/thalsime/clickup-deadline-daemon/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/thalsime/clickup-deadline-daemon/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/thalsime/clickup-deadline-daemon/releases/tag/v1.0.0

# clickup-deadline-daemon

> **v1.1.3** -- [CHANGELOG](CHANGELOG.md)

Daemon de automação do ClickUp que executa três regras quando tasks mudam de estado:

**Regra 1 -- Status "em progresso" sem responsável:** quando uma task muda para "em
progresso" (ou outro status em `TRIGGER_STATUSES`) e não tem nenhum assignee, o daemon
atribui automaticamente quem executou a mudança de status. Em seguida calcula e define
o `due_date` a partir do `time_estimate`. Se o ator do evento não estiver disponível no
payload (ex.: automações nativas do ClickUp), o daemon loga um WARNING e calcula o
`due_date` sem atribuir responsável.

**Regra 2 -- Assignee em task pendente:** quando alguém é atribuído a uma task cujo
status está em `PENDING_STATUSES` (ex.: "pendente"), o daemon promove o status para
"em progresso" e calcula o `due_date`.

**Regra 3 -- Auditoria total:** toda mudança em qualquer task (de qualquer tipo) é
registrada no banco SQLite local, incluindo as próprias ações do daemon. O registro
contém: quem executou, quando, qual campo mudou, valor antes e depois.

Regras comuns a todas:

- Com `time_estimate` preenchido: converte o esforço em dias úteis pela base 4h/dia
  (`MS_PER_DAY = 14400000 ms`), arredondando para cima. Exemplo: `time_estimate = 43200000`
  ms (3 dias) -> prazo = hoje + 3.
- Sem `time_estimate`: aplica um **fallback** de `FALLBACK_ESTIMATE_DAYS` dias úteis
  (default 2), grava a `due_date` e **comenta na task** avisando que a estimativa estava
  ausente, que o prazo pode não ser real e que se recomenda revisão. `FALLBACK_ESTIMATE_DAYS=0`
  desativa o fallback (volta a apenas pular quando falta a estimativa).
- O prazo base é `max(agora, start_date) + dias_de_estimativa`. Isso evita o HTTP 400
  que o ClickUp retorna quando `due_date` calculado ficaria antes do `start_date` da task.
- Não sobrescreve `due_date` já definida manualmente (logo, o comentário do fallback é
  postado uma única vez: na passagem seguinte a `due_date` já existe e a task é pulada).
- Compatível com o plano **Free** do ClickUp (usa webhooks nativos, sem custom fields).
- O banco de auditoria (`audit.db`) contém PII -- **nunca versionar**.

O campo `time_estimate` é o campo nativo que o ClickUp registra nas tasks via API; não há
campo customizado. Ele é visível em `GET /api/v2/task/{task_id}` e editável na interface
do ClickUp em "Time Estimate" (na lista de tasks ou no detalhe da task).

---

## Pré-requisitos

- VPS com Debian/Ubuntu
- Python 3.11+
- nginx (proxy reverso)
- Domínio público acessível pelo ClickUp
- **Conta de serviço no ClickUp** com acesso admin ao Space (ver seção 1)

---

## 1. Conta de serviço e anti-loop

O daemon deve usar o **token de API de uma conta de serviço dedicada**, não a conta
pessoal do tech lead. Isso é obrigatório para o mecanismo anti-loop funcionar corretamente.

**Por que é necessário:** quando o daemon executa uma ação no ClickUp (ex.: atribuir
responsável, mudar status), o ClickUp reenvia um evento de webhook para o daemon. Sem
uma identidade distinta, o daemon não consegue diferenciar eventos de origem humana de
eventos gerados por ele mesmo, causando uma cascata de ações em loop.

**Como funciona:** ao iniciar, o daemon chama `GET /api/v2/user` com o token configurado
para descobrir o ID numérico da conta de serviço (variável interna `TOKEN_OWNER_ID`).
Eventos onde o ator é a própria conta de serviço são auditados normalmente mas não
disparam novas ações de escrita. Se a resolução do ID falhar no startup (ex.: token
inválido), o daemon entra em modo conservador: audita tudo mas não executa nenhuma
escrita até o próximo restart.

**Como criar a conta de serviço:**

1. Crie um usuário ClickUp separado (ex.: com um email de serviço ou alias como
   `seuemail+clickup-daemon@dominio.com`).
2. Adicione esse usuário ao Space do ClickUp como **administrador** (necessário para
   que o token possa atribuir responsáveis e mudar status nas tasks).
3. Faça login com esse usuário e acesse `https://app.clickup.com/settings/apps` para
   gerar o token da API.
4. Cole o token em `CLICKUP_API_TOKEN` no arquivo `.env`.

---

## 2. Instalar no VPS

```bash
# Copiar o projeto para o VPS (SCP para /tmp primeiro; depois mover como root)
scp -r . usuario@servidor:/tmp/daemon-update/
ssh usuario@servidor "sudo cp -r /tmp/daemon-update/. /opt/clickup-deadline-daemon/"

# Ajustar proprietário (o serviço roda como www-data)
sudo chown -R www-data:www-data /opt/clickup-deadline-daemon/

cd /opt/clickup-deadline-daemon

# Criar virtualenv e instalar dependências
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Configurar variáveis de ambiente
sudo cp .env.example /opt/clickup-deadline-daemon/.env
sudo chown www-data:www-data /opt/clickup-deadline-daemon/.env
sudo chmod 640 /opt/clickup-deadline-daemon/.env
sudo nano /opt/clickup-deadline-daemon/.env
```

O arquivo `.env` deve ter permissão `640` e dono `www-data:www-data`.

---

## 3. Configurar o systemd

```bash
sudo cp clickup-deadline-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable clickup-deadline-daemon
sudo systemctl start clickup-deadline-daemon

# Verificar status e TOKEN_OWNER_ID resolvido
sudo systemctl status clickup-deadline-daemon

# Ver logs em tempo real (confirmar TOKEN_OWNER_ID resolvido no startup)
sudo journalctl -u clickup-deadline-daemon -f
```

No startup, o log deve mostrar a linha `Token owner resolvido: id=<id>`. Se aparecer
`Não foi possível resolver o token owner`, o daemon funciona em modo conservador (audita, não escreve).

---

## 3.1. Configurar o reconciliador (systemd timer)

O reconciliador varre periodicamente as listas configuradas e aplica as regras de
`due_date` e promoção de status em tasks que o webhook possa ter perdido (reinício
do daemon, 502 transitório, evento anterior ao deploy).

```bash
sudo cp deploy/reconcile.service /etc/systemd/system/
sudo cp deploy/reconcile.timer   /etc/systemd/system/

# Definir RECONCILE_LIST_IDS no unit (IDs das listas separados por virgula)
sudo nano /etc/systemd/system/reconcile.service

sudo systemctl daemon-reload
sudo systemctl enable --now reconcile.timer

# Verificar proxima execucao
systemctl list-timers reconcile.timer

# Dry-run manual para validar antes de ativar
RECONCILE_DRY_RUN=1 sudo systemctl start reconcile.service
sudo journalctl -u clickup-reconciler -f
```

O timer executa a cada **10 minutos** com `Persistent=true` -- garante execução ao
reiniciar o VPS caso o horário programado tenha passado durante o downtime.

**Nota:** o reconciliador não repete a lógica de "atribuir quem moveu o status"
(Regra 1 parte 2), pois essa depende do ator do evento original, disponível apenas
no payload do webhook. As demais invariantes (calcular `due_date` e promover status)
são aplicadas idempotentemente.

---

## 4. Configurar o nginx

```bash
sudo cp nginx.conf.example /etc/nginx/sites-available/clickup-deadline-daemon

# Substituir SEU_DOMINIO pelo domínio real
sudo nano /etc/nginx/sites-available/clickup-deadline-daemon

sudo ln -s /etc/nginx/sites-available/clickup-deadline-daemon \
           /etc/nginx/sites-enabled/clickup-deadline-daemon

sudo nginx -t && sudo systemctl reload nginx
```

### HTTPS

**Opção A -- Plugin nginx (certbot-nginx, mais simples):**

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d SEU_DOMINIO
```

**Opção B -- DNS-01 via Cloudflare (sem plugin nginx):**

```bash
sudo apt install certbot python3-certbot-dns-cloudflare
# Configurar credencial Cloudflare em /root/.secrets/cloudflare.ini (chmod 600)
sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /root/.secrets/cloudflare.ini \
  -d SEU_DOMINIO
```

Neste caso, adicionar o bloco SSL diretamente no vhost (sem `include options-ssl-nginx.conf`
-- esse arquivo só existe quando o plugin nginx está instalado).

---

## 5. Registrar o webhook no ClickUp

```bash
cd /opt/clickup-deadline-daemon

# Registrar (substituir URL e workspace ID)
venv/bin/python register_webhook.py register \
  --url https://SEU_DOMINIO/webhook \
  --workspace <WORKSPACE_ID>

# O ClickUp gera o secret automaticamente -- capturar na saída:
# {"secret": "<secret_gerado>", "webhook_id": "<id>", ...}
# Copiar o secret para CLICKUP_WEBHOOK_SECRET no .env e reiniciar o daemon.

# Listar webhooks existentes
venv/bin/python register_webhook.py list --workspace <WORKSPACE_ID>

# Atualizar lista de eventos sem recriar o webhook (preserva o secret)
venv/bin/python register_webhook.py update \
  --webhook-id <id> \
  --events "taskStatusUpdated,taskAssigneeUpdated,taskCreated,taskUpdated,taskDeleted"

# Remover webhook
venv/bin/python register_webhook.py delete --webhook-id <id>
```

**AVISO:** o ClickUp **ignora** o parâmetro `--secret` passado na criação e gera um
secret próprio. O secret real está na **resposta** do comando `register` -- copiar
imediatamente para o `.env`. O wildcard `--events "*"` é aceito pelo ClickUp e é o
padrão do script: cobre todos os eventos do workspace, incluindo tipos adicionados
futuramente. Para limitar a eventos específicos, passar a lista em `--events`.

---

## 6. Testar localmente (sem VPS)

```bash
# Instalar dependências
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Definir variável mínima
export CLICKUP_API_TOKEN=pk_dummy_test_token

# Rodar o servidor
uvicorn main:app --reload --port 8765

# Verificar health (retorna token_owner_id resolvido)
curl http://localhost:8765/health

# Simular webhook de mudança de status
curl -X POST http://localhost:8765/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "event": "taskStatusUpdated",
    "task_id": "86e1qtcwn",
    "history_items": [{
      "id": "hist-001",
      "field": "status",
      "user": {"id": 12345},
      "after": {"status": "em progresso"}
    }]
  }'
```

---

## 7. Rodar os testes

```bash
# Instalar dependências de produção + desenvolvimento
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Na pasta webhook-deadline-daemon/
export CLICKUP_API_TOKEN=pk_dummy_test_token
pytest test_main.py -v
```

Os testes cobrem: predicados puros (`get_actor_id`, `is_self_action`, `compute_due_date_ms`,
`due_date_is_set`, `is_assignee_add`), as três regras de automação, o anti-loop, o dedup de
auditoria e a discriminação add/rem no `taskAssigneeUpdated`. Total: **48 testes**.
Requer `pytest>=8.3` e `pytest-asyncio>=0.24` (instalados via `requirements-dev.txt`).

---

## Variáveis de ambiente

| Variável | Obrigatória | Default | Descrição |
|---|---|---|---|
| `CLICKUP_API_TOKEN` | Sim | -- | Token da **conta de serviço** da API do ClickUp (ver seção 1). |
| `CLICKUP_WEBHOOK_SECRET` | Não | -- | Secret HMAC para validar assinatura dos webhooks. Gerado pelo ClickUp na criação -- copiar da resposta do `register_webhook.py register`. |
| `TRIGGER_STATUSES` | Não | `em progresso,in progress` | Status que disparam o cálculo e a atribuição automática, separados por vírgula, case-insensitive. Deve bater com o nome exato do status no Space ClickUp (verificar em Settings -> Statuses). |
| `TARGET_STATUS` | Não | `em progresso` | Nome exato do status para o qual a Regra 2 promove tasks "pendentes". Deve bater com o status configurado no Space. |
| `PENDING_STATUSES` | Não | `pendente` | Status considerados "pendente" para a Regra 2, separados por vírgula, case-insensitive. |
| `MS_PER_DAY` | Não | `14400000` | Milissegundos por dia útil (default: 4h/dia). |
| `FALLBACK_ESTIMATE_DAYS` | Não | `2` | Dias úteis usados como estimativa padrão quando a task entra no gatilho sem `time_estimate`: grava a `due_date` e comenta pedindo revisão. `0` desativa o fallback. |
| `AUDIT_BACKEND` | Não | `sqlite` | Backend de auditoria (somente `sqlite` suportado atualmente). |
| `AUDIT_PATH` | Não | `/opt/clickup-deadline-daemon/audit.db` | Caminho do SQLite de auditoria. Deve ser gravável pelo usuário que roda o serviço. PII -- **nunca versionar**. |
| `RECONCILE_LIST_IDS` | Sim (reconcile.py) | -- | IDs das listas do ClickUp a varrer, separados por vírgula. Obrigatório para o reconciliador. Exemplo: `<LIST_ID_1>,<LIST_ID_2>`. |
| `RECONCILE_DRY_RUN` | Não | `0` | Se `1` ou `true`, o reconciliador loga sem escrever nenhuma alteração. Útil para validar antes de ativar em produção. |

---

## 8. Auditoria SQLite

O daemon registra **toda** mudança em qualquer task no arquivo SQLite `audit.db`.

### Schema

```sql
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    history_item_id TEXT UNIQUE,       -- dedup contra reentregas do ClickUp
    received_at     TEXT,              -- ISO UTC (quando o webhook chegou)
    event_date      TEXT,              -- epoch ms do item como string (quando a mudança ocorreu)
    event_type      TEXT,              -- ex.: taskStatusUpdated
    task_id         TEXT,
    team_id         TEXT,
    field           TEXT,              -- ex.: status, assignee_add
    actor_id        INTEGER,           -- quem executou
    actor_name      TEXT,
    actor_email     TEXT,              -- PII -- nao versionar este arquivo
    before_value    TEXT,              -- JSON do estado anterior
    after_value     TEXT,              -- JSON do estado posterior
    is_self_action  INTEGER DEFAULT 0, -- 1 se o ator e a conta de servico
    daemon_action   TEXT,              -- acao que o daemon tomou (se aplicavel)
    raw_payload     TEXT               -- payload JSON completo (forense)
)
```

### Consultas úteis

```bash
# Ver as últimas 20 ações disparadas por humanos (excluindo o próprio daemon)
sqlite3 /opt/clickup-deadline-daemon/audit.db \
  "SELECT received_at, task_id, field, actor_name, daemon_action
   FROM audit_log
   WHERE is_self_action = 0
   ORDER BY id DESC LIMIT 20;"

# Verificar ausência de loops (ações do daemon nao devem gerar novas ações)
sqlite3 /opt/clickup-deadline-daemon/audit.db \
  "SELECT COUNT(*) FROM audit_log WHERE is_self_action = 1 AND daemon_action IS NOT NULL;"

# Contar eventos por tipo
sqlite3 /opt/clickup-deadline-daemon/audit.db \
  "SELECT event_type, COUNT(*) n FROM audit_log GROUP BY event_type ORDER BY n DESC;"
```

**Aviso de PII:** `actor_email` e `actor_name` contêm dados pessoais dos usuários do
ClickUp. O arquivo `audit.db` e seus arquivos auxiliares (`audit.db-wal`, `audit.db-shm`)
nunca devem ser versionados, copiados para repositórios ou enviados a serviços externos.

---

## 9. Referência: estrutura dos payloads recebidos

Payloads confirmados em 2026-06-07 via log de debug no ambiente de produção.

### taskStatusUpdated

```json
{
  "event": "taskStatusUpdated",
  "task_id": "<id>",
  "team_id": "<workspace_id>",
  "webhook_id": "<webhook_id>",
  "history_items": [{
    "id": "<history_item_id>",
    "type": 1,
    "date": "<epoch_ms>",
    "field": "status",
    "parent_id": "<list_id>",
    "data": {"status_type": "custom"},
    "source": null,
    "user": {
      "id": "<user-id>",
      "username": "<nome-do-usuario>",
      "email": "<email-do-usuario>",
      "color": "#595d66",
      "initials": "<iniciais>",
      "profilePicture": null,
      "role": 1,
      "role_subtype": 0
    },
    "before": {"status": "pendente",    "color": "#87909e", "orderindex": 0, "type": "open"},
    "after":  {"status": "em progresso","color": "#5f55ee", "orderindex": 1, "type": "custom"}
  }]
}
```

O campo `history_items[0].user` identifica **quem executou a mudança de status**. O daemon
usa esse campo na Regra 1 para a atribuição automática quando não há assignee.

### taskAssigneeUpdated

```json
{
  "event": "taskAssigneeUpdated",
  "task_id": "<id>",
  "team_id": "<workspace_id>",
  "webhook_id": "<webhook_id>",
  "history_items": [{
    "id": "<history_item_id>",
    "type": 1,
    "date": "<epoch_ms>",
    "field": "assignee_add",
    "parent_id": "<list_id>",
    "data": {},
    "source": null,
    "user": {
      "id": "<user-id>",
      "username": "<nome-do-usuario>",
      "email": "<email-do-usuario>",
      "color": "#595d66",
      "initials": "<iniciais>",
      "profilePicture": null,
      "role": 1,
      "role_subtype": 0
    },
    "after": {
      "id": "<user-id-atribuido>",
      "username": "<nome-do-atribuido>",
      "email": "<email-do-atribuido>",
      "color": "#595d66",
      "initials": "<iniciais>",
      "profilePicture": null
    }
  }]
}
```

Dois campos de usuário distintos:
- `history_items[0].user` -- quem **executou** a atribuição
- `history_items[0].after` -- quem **foi atribuído** (mesmos campos, sem `role`)

Quando alguém se auto-atribui, os dois objetos referenciam a mesma pessoa.

**AVISO: evento bidirecional.** O ClickUp emite `taskAssigneeUpdated` tanto para
**adição** quanto para **remoção** de responsável. A direção é indicada pelo campo
`field` do `history_item`:

- `field: "assignee_add"` -- responsável foi **adicionado** (daemon deve reagir)
- `field: "assignee_rem"` -- responsável foi **removido** (daemon ignora)

Fallback quando `field` estiver ausente: o daemon verifica se `after` é um dict com
`id` preenchido (`after.get("id")` truthy). Se `after: {}` (dict vazio) ou
`after: null`, trata como remoção e ignora. A lógica está em `is_assignee_add()`
em `main.py`.

---

## 10. Nota: `time_estimate` na API ClickUp usa milissegundos

O campo `time_estimate` das tasks é armazenado e retornado pela API em **milissegundos**,
independente de como é exibido na interface do ClickUp.

Referência rápida:

| Exibição no ClickUp | Milissegundos |
|---|---|
| 1h | 3,600,000 |
| 4h (1 dia -- base daemon 4h/dia) | 14,400,000 |
| 8h | 28,800,000 |
| 10h | 36,000,000 |
| 2d (base daemon) | 28,800,000 |
| 5d (base daemon) | 72,000,000 |

Ao criar ou atualizar tasks via API ou scripts, sempre passar o valor em milissegundos.
A ferramenta MCP `update_task` descreve o campo como "minutos", mas isso está incorreto --
passar o valor em ms diretamente (confirmado em 2026-06-07).

---

## Estrutura do projeto

```
clickup-deadline-daemon/
-- main.py                           # FastAPI app -- logica principal (v1.1.3)
-- rules.py                          # Predicados e calculo de due_date (compartilhado)
-- audit.py                          # Modulo de auditoria SQLite
-- reconcile.py                      # Reconciliador idempotente (varre listas a cada 10 min)
-- test_main.py                      # Testes unitarios (48 testes)
-- register_webhook.py               # Script para registrar/listar/atualizar/remover webhooks
-- requirements.txt                  # Dependencias de producao
-- requirements-dev.txt              # Dependencias de desenvolvimento (pytest, pytest-asyncio)
-- .env.example
-- CHANGELOG.md                      # Historico de versoes (v1.0.0 a v1.1.3)
-- clickup-deadline-daemon.service   # systemd unit -- daemon principal (FastAPI, porta 8765)
-- nginx.conf.example
-- deploy/
    -- reconcile.service             # systemd unit -- reconciliador (oneshot)
    -- reconcile.timer               # systemd timer -- dispara a cada 10 minutos
-- README.md
```

---

## Fluxo de contribuição

Branches:

- `main` -- branch de release protegida. Nunca recebe commit ou push direto. Só avança
  via squash-merge de Pull Requests vindos exclusivamente da `dev`, mergeados na
  interface do GitHub.
- `dev` -- branch de integração (default). Recebe código via Pull Request a partir de
  branches de trabalho (`feature/...`, `fix/...`). Protegida contra push direto, force-push
  e deleção; exige CI verde.

Fluxo de trabalho:

1. Crie uma branch a partir da `dev`:
   `git checkout dev && git pull && git checkout -b feature/minha-mudanca`
2. Faça commits (assinados) e abra um PR para a `dev`. O CI precisa passar.
3. Quando a `dev` estiver pronta para release, abra um PR `dev` -> `main` no GitHub.
   O check `validate-source` garante que só a `dev` pode ser origem, e o CI precisa
   passar. Faça o squash-merge pelo botão do GitHub.

### Setup local obrigatório (uma vez por clone)

Ativa os hooks que bloqueiam commit/push direto na `main`:

```bash
git config core.hooksPath .githooks
```

Os hooks (`.githooks/pre-commit` e `.githooks/pre-push`) recusam commits e pushes diretos
na `main` localmente. A proteção real está nos rulesets do GitHub; os hooks são uma rede
de segurança local.

### Commits assinados

A `main` exige commits verificados (`required_signatures`). Configure a assinatura SSH no
clone, reaproveitando sua chave de autenticação:

```bash
git config gpg.format ssh
git config user.signingkey ~/.ssh/sua_chave.pub
git config commit.gpgsign true
```

Registre a mesma chave pública como **Signing Key** em GitHub Settings -> SSH and GPG keys
(é um tipo distinto da Authentication Key; a mesma chave pode ser registrada nos dois tipos).

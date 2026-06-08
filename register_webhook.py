"""
register_webhook.py
===================
Registra (ou lista / remove / atualiza) o webhook no ClickUp.

Uso:
  python register_webhook.py register --url https://SEU_DOMINIO/webhook --workspace <WORKSPACE_ID>
  python register_webhook.py list     --workspace <WORKSPACE_ID>
  python register_webhook.py delete   --webhook-id <id>
  python register_webhook.py update   --webhook-id <id> [--url https://...] [--events taskStatusUpdated,taskAssigneeUpdated]

O subcomando 'update' usa PUT e preserva o secret existente.
O subcomando 'register' usa POST; o ClickUp gera um novo secret na resposta -- copiar
para CLICKUP_WEBHOOK_SECRET no .env e reiniciar o daemon.
"""

import argparse
import json
import os
import sys

import httpx

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_API_BASE  = "https://api.clickup.com/api/v2"
HEADERS = {
    "Authorization": CLICKUP_API_TOKEN,
    "Content-Type": "application/json",
}

# Escutar todos os eventos -- garante auditoria total e compatibilidade com novas features.
# Use --events para restringir (ex.: taskStatusUpdated,taskAssigneeUpdated) se necessário.
WATCHED_EVENTS = ["*"]


def _parse_events(events_arg: str) -> list[str]:
    """Converte argumento --events (csv ou '*') para lista."""
    parts = [e.strip() for e in events_arg.split(",") if e.strip()]
    return parts if parts else ["*"]


def register(workspace_id: str, endpoint_url: str, events: list[str], secret: str | None):
    payload: dict = {
        "endpoint": endpoint_url,
        "events":   events,
    }
    if secret:
        payload["secret"] = secret

    resp = httpx.post(
        f"{CLICKUP_API_BASE}/team/{workspace_id}/webhook",
        headers=HEADERS,
        json=payload,
        timeout=10.0,
    )
    resp.raise_for_status()
    data    = resp.json()
    webhook = data.get("webhook", data)
    wh_id   = webhook.get("id") or data.get("id")
    wh_sec  = webhook.get("secret") or data.get("secret")

    print(json.dumps(data, indent=2))
    print()
    print("Webhook registrado com sucesso!")
    print(f"  ID:      {wh_id}")
    print(f"  URL:     {endpoint_url}")
    print(f"  Events:  {events}")
    if wh_sec:
        print(f"  Secret:  {wh_sec}")
        print()
        print("IMPORTANTE: copie o 'secret' acima para CLICKUP_WEBHOOK_SECRET no .env")
        print("e reinicie o daemon. O ClickUp não exibe o secret novamente.")


def list_webhooks(workspace_id: str):
    resp = httpx.get(
        f"{CLICKUP_API_BASE}/team/{workspace_id}/webhook",
        headers=HEADERS,
        timeout=10.0,
    )
    resp.raise_for_status()
    data     = resp.json()
    webhooks = data.get("webhooks", [])
    if not webhooks:
        print("Nenhum webhook registrado.")
        return
    for wh in webhooks:
        print(
            f"ID: {wh['id']}  "
            f"URL: {wh['endpoint']}  "
            f"Status: {wh.get('status')}  "
            f"Events: {wh.get('events')}"
        )


def delete_webhook(webhook_id: str):
    resp = httpx.delete(
        f"{CLICKUP_API_BASE}/webhook/{webhook_id}",
        headers=HEADERS,
        timeout=10.0,
    )
    resp.raise_for_status()
    print(f"Webhook {webhook_id} removido.")


def update_webhook(webhook_id: str, endpoint_url: str | None, events: list[str]):
    """
    Atualiza eventos (e opcionalmente a URL) do webhook via PUT.
    O secret existente é preservado -- não é necessário atualizar o .env.
    """
    payload: dict = {"events": events, "status": "active"}
    if endpoint_url:
        payload["endpoint"] = endpoint_url

    resp = httpx.put(
        f"{CLICKUP_API_BASE}/webhook/{webhook_id}",
        headers=HEADERS,
        json=payload,
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    print(json.dumps(data, indent=2))
    print()
    print(f"Webhook {webhook_id} atualizado.")
    print(f"  Events: {events}")
    if endpoint_url:
        print(f"  URL:    {endpoint_url}")
    print("Secret preservado -- não é necessário atualizar o .env.")


def main():
    parser = argparse.ArgumentParser(description="Gerencia webhooks do ClickUp")
    subparsers = parser.add_subparsers(dest="command")

    # register
    reg = subparsers.add_parser("register", help="Registra um novo webhook (POST -- gera novo secret)")
    reg.add_argument("--url",       required=True, help="URL publica do endpoint (ex.: https://meu-vps.com/webhook)")
    reg.add_argument("--workspace", required=True, help="Workspace ID do ClickUp")
    reg.add_argument("--events",    default="*",   help="Eventos separados por vírgula, ou '*' para todos (default: *)")
    reg.add_argument("--secret",    default=None,  help="Secret HMAC sugerido (o ClickUp pode ignorar e gerar o próprio)")

    # list
    lst = subparsers.add_parser("list", help="Lista webhooks registrados")
    lst.add_argument("--workspace", required=True, help="Workspace ID do ClickUp")

    # delete
    dlt = subparsers.add_parser("delete", help="Remove um webhook")
    dlt.add_argument("--webhook-id", required=True, help="ID do webhook a remover")

    # update
    upd = subparsers.add_parser("update", help="Atualiza eventos/URL do webhook (PUT -- preserva secret)")
    upd.add_argument("--webhook-id", required=True, help="ID do webhook a atualizar")
    upd.add_argument("--events",     default="*",   help="Novos eventos separados por virgula, ou '*' (default: *)")
    upd.add_argument("--url",        default=None,  help="Nova URL (opcional -- omitir para manter a atual)")

    args = parser.parse_args()

    if not CLICKUP_API_TOKEN:
        print("Erro: variável de ambiente CLICKUP_API_TOKEN não definida.")
        sys.exit(1)

    if args.command == "register":
        register(args.workspace, args.url, _parse_events(args.events), args.secret)
    elif args.command == "list":
        list_webhooks(args.workspace)
    elif args.command == "delete":
        delete_webhook(args.webhook_id)
    elif args.command == "update":
        update_webhook(args.webhook_id, args.url, _parse_events(args.events))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

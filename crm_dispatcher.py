"""
crm_dispatcher.py

CRM sync dispatcher — sends lead/reply events to integrated CRMs.

Supports:
  - HubSpot:  creates/updates contacts, optionally creates deals
  - Pipedrive: creates persons + deals in a configured pipeline
  - Generic webhook: posts the full event payload to any URL

Called from reply_classifier.py when a reply is classified as 'interested',
or from imap_processor.py when any reply is detected.
"""

import json
import logging
import os
from typing import Any

import httpx

from supabase import Client, create_client

logger = logging.getLogger(__name__)

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")


def _get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _log_sync(sb: Client, client_id: str, integration_id: str, lead_id: str, event_type: str, status: str, response: str) -> None:
    """Record a sync attempt for audit."""
    try:
        sb.table("crm_sync_log").insert({
            "client_id": client_id,
            "integration_id": integration_id,
            "lead_id": lead_id,
            "event_type": event_type,
            "status": status,
            "response": response[:1000],
        }).execute()
    except Exception as exc:
        logger.warning("Failed to log CRM sync: %s", exc)


def sync_to_hubspot(integration: dict, lead: dict, event_type: str) -> tuple[str, str]:
    """
    Sync a lead to HubSpot. Returns (status, response).
    """
    api_key = integration.get("api_key", "")
    if not api_key:
        return "failed", "No HubSpot API key configured"

    payload: dict[str, Any] = {
        "properties": {
            "email": lead.get("email", ""),
            "firstname": lead.get("first_name", ""),
            "lastname": lead.get("last_name", ""),
            "company": lead.get("company", ""),
            "phone": lead.get("phone", ""),
            "warmr_event": event_type,
        }
    }

    try:
        resp = httpx.post(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return "success", f"Contact created: {resp.json().get('id')}"
        if resp.status_code == 409:
            # Contact exists — try update by email
            email = lead.get("email", "")
            update_resp = httpx.patch(
                f"https://api.hubapi.com/crm/v3/objects/contacts/{email}?idProperty=email",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=15,
            )
            if update_resp.status_code in (200, 201):
                return "success", f"Contact updated: {email}"
            return "failed", f"HubSpot update {update_resp.status_code}: {update_resp.text[:200]}"
        return "failed", f"HubSpot {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return "failed", f"HubSpot exception: {exc}"


def sync_to_pipedrive(integration: dict, lead: dict, event_type: str) -> tuple[str, str]:
    """Sync a lead to Pipedrive as a person + optionally a deal."""
    api_key = integration.get("api_key", "")
    config = integration.get("config", {}) or {}
    if not api_key:
        return "failed", "No Pipedrive API key configured"

    company_domain = config.get("company_domain", "api")
    base = f"https://{company_domain}.pipedrive.com/api/v1"

    person_payload = {
        "name": (lead.get("first_name", "") + " " + lead.get("last_name", "")).strip() or lead.get("email", ""),
        "email": [lead.get("email", "")],
        "phone": [lead.get("phone", "")] if lead.get("phone") else [],
        "org_id": None,
    }

    try:
        resp = httpx.post(
            f"{base}/persons?api_token={api_key}",
            json=person_payload,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            person_id = resp.json().get("data", {}).get("id")
            return "success", f"Person created: {person_id}"
        return "failed", f"Pipedrive {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return "failed", f"Pipedrive exception: {exc}"


def sync_to_webhook(integration: dict, lead: dict, event_type: str) -> tuple[str, str]:
    """Generic webhook POST."""
    url = integration.get("webhook_url", "")
    if not url:
        return "failed", "No webhook URL configured"

    payload = {
        "event": event_type,
        "lead": lead,
        "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }

    try:
        resp = httpx.post(url, json=payload, timeout=15)
        if 200 <= resp.status_code < 300:
            return "success", f"Webhook delivered: HTTP {resp.status_code}"
        return "failed", f"Webhook {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return "failed", f"Webhook exception: {exc}"


def dispatch_event(client_id: str, lead: dict, event_type: str, sb: Client | None = None) -> list[dict]:
    """
    Dispatch a lead event to all active CRM integrations for this client.

    event_type: 'reply' | 'interested' | 'meeting' | 'bounce'
    Returns a list of {provider, status, response} dicts.
    """
    sb = sb or _get_supabase()
    resp = sb.table("crm_integrations").select("*").eq("client_id", client_id).eq("active", True).execute()
    integrations = resp.data or []
    if not integrations:
        return []

    # Decrypt stored api_keys once per dispatch so sync_to_* receives plaintext.
    # decrypt() is a no-op for plaintext rows (backward-compat for unmigrated data).
    try:
        from utils.secrets_vault import decrypt as _vault_decrypt
        for integration in integrations:
            if integration.get("api_key"):
                try:
                    integration["api_key"] = _vault_decrypt(integration["api_key"])
                except Exception as exc:
                    logger.error("Failed to decrypt api_key for integration %s: %s", integration.get("id"), exc)
                    integration["api_key"] = ""  # Force sync to fail cleanly
    except ImportError:
        pass

    results = []
    for integration in integrations:
        # Check if this event type should sync
        flag_key = f"sync_on_{event_type}"
        if not integration.get(flag_key, False):
            continue

        provider = integration.get("provider", "")
        if provider == "hubspot":
            status, response = sync_to_hubspot(integration, lead, event_type)
        elif provider == "pipedrive":
            status, response = sync_to_pipedrive(integration, lead, event_type)
        elif provider == "webhook":
            status, response = sync_to_webhook(integration, lead, event_type)
        else:
            status, response = "failed", f"Unknown provider: {provider}"

        _log_sync(sb, client_id, integration["id"], lead.get("id", ""), event_type, status, response)
        results.append({"provider": provider, "status": status, "response": response})

    return results

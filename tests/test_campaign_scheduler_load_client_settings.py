"""
tests/test_campaign_scheduler_load_client_settings.py — regression-guard
voor UnboundLocalError op load_client_settings in process_lead.

Bug: een lokale `from warmup_engine import load_client_settings` binnen
process_lead() shadowt de module-level load_client_settings (regel 98),
waardoor Python de naam als lokaal markeert en de eerdere call op
regel 854 (`client_settings = load_client_settings(...)`) een
UnboundLocalError gooit.

Fix: de lokale import importeert nu alleen `resolve_sender_name` +
`append_signature` uit warmup_engine. `load_client_settings` blijft de
module-level functie (regel 98).

Static checks — geen SMTP/DB-mock nodig.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import campaign_scheduler


def test_no_shadowing_import_of_load_client_settings_in_process_lead():
    """
    process_lead() mag GEEN lokale `from warmup_engine import
    load_client_settings` bevatten — anders krijg je UnboundLocalError op
    de eerdere call.
    """
    src = inspect.getsource(campaign_scheduler.process_lead)
    assert "from warmup_engine import load_client_settings" not in src, (
        "Regression: shadowing import of load_client_settings detected in "
        "process_lead(). Causes UnboundLocalError."
    )


def test_process_lead_calls_module_level_load_client_settings():
    """
    process_lead() roept de module-level load_client_settings aan
    (regel 98 in campaign_scheduler.py). Bewijst dat de fix werkt:
    de call gaat naar de juiste functie, niet naar een lokale shadow.
    """
    src = inspect.getsource(campaign_scheduler.process_lead)
    assert "load_client_settings(supabase, client_id)" in src, (
        "process_lead must call load_client_settings — fix removed the "
        "wrong reference."
    )


def test_module_level_load_client_settings_is_callable():
    """Sanity: load_client_settings bestaat op module-niveau en is een functie."""
    assert hasattr(campaign_scheduler, "load_client_settings")
    assert callable(campaign_scheduler.load_client_settings)


def test_warmup_engine_still_exports_resolve_sender_name_and_signature():
    """
    De lokale import die wél blijft staan (`from warmup_engine import
    resolve_sender_name, append_signature`) heeft beide functies nodig.
    Als warmup_engine ooit één van die twee removed, krijgt process_lead
    een ImportError binnen de try-except — silent fallback maar zonder
    sender_name + signature in de uitgaande mail.
    """
    import warmup_engine
    assert hasattr(warmup_engine, "resolve_sender_name")
    assert hasattr(warmup_engine, "append_signature")
    assert callable(warmup_engine.resolve_sender_name)
    assert callable(warmup_engine.append_signature)

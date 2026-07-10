"""
warmup_templates.py — zero-LLM warmup email content via spintax.

Warmup traffic is internal (client inbox <-> warmup network) and never seen
by a prospect, so paying for a live Claude Haiku generation on EVERY warmup
send (warmup_engine.generate_email_content, warmup_engine.py:382) is pure
burn. This module renders natural-looking warmup emails from a per-language
spintax bank, reusing the deterministic renderer in spintax_engine so the
output still varies from send to send.

Public API:
    render_warmup_email(language, sender_name, recipient_name, seed=None)
        -> (subject, body)

generate_email_content() uses these templates by default and only samples a
small percentage of sends through the live model (see WARMUP_LLM_SAMPLE_PCT).
"""

from __future__ import annotations

import uuid

from spintax_engine import process_spintax

# ---------------------------------------------------------------------------
# Template banks. {a|b|c} = spintax choice. {sender}/{recipient} = names.
# Each subject/body is a spintax string so a single template yields many
# surface forms. Keep them short, human, and marketing-free (per CLAUDE.md).
# ---------------------------------------------------------------------------

_SUBJECTS: dict[str, list[str]] = {
    "nl": [
        "{Snelle|Korte} vraag",
        "{Even|Kort} {bijpraten|afstemmen}",
        "{Update|Statusje} {project|planning}",
        "{Terugkoppeling|Follow-up} van {gisteren|deze week}",
        "{Voorstel|Idee} voor {volgende week|binnenkort}",
    ],
    "en": [
        "{Quick|Short} question",
        "{Quick|Brief} {check-in|update}",
        "{Following up|Circling back} on {yesterday|our chat}",
        "{Update|Status} on the {project|planning}",
        "{Idea|Thought} for {next week|soon}",
    ],
    "fr": [
        "{Petite|Courte} question",
        "{Point|Suivi} {rapide|rapide sur le projet}",
        "{Retour|Suivi} de {hier|cette semaine}",
        "{Mise a jour|Statut} sur le {projet|planning}",
        "{Idee|Proposition} pour {la semaine prochaine|bientot}",
    ],
}

_BODIES: dict[str, list[str]] = {
    "nl": [
        "Hoi {recipient},\n\n{Bedankt voor je bericht van gisteren.|Fijn dat we elkaar spraken.} "
        "{Ik wilde even laten weten dat|Even een korte update:} het {op schema ligt|goed loopt}. "
        "{Laat het gerust weten als je nog vragen hebt.|Ik hoor het graag als er iets is.}\n\n"
        "{Groeten|Met vriendelijke groet},\n{sender}",

        "Hoi {recipient},\n\n{Kleine|Korte} vraag: {kun je me later deze week de laatste stand doorsturen?|"
        "heb je de cijfers al kunnen bekijken?} {Geen haast|Het heeft geen spoed}, {wanneer het uitkomt is prima.|"
        "als het maandag lukt is dat prima.}\n\n{Alvast bedankt|Dank je wel},\n{sender}",

        "Beste {recipient},\n\n{Ik pak het onderwerp van ons gesprek even op.|Ter opvolging van eerder.} "
        "{Volgens mij kunnen we het simpel houden|Ik denk dat de aanpak werkt}. "
        "{Zullen we er kort over bellen?|Laat maar weten wat jij denkt.}\n\n"
        "{Groet|Hartelijke groet},\n{sender}",
    ],
    "en": [
        "Hi {recipient},\n\n{Thanks for your note yesterday.|Good speaking earlier.} "
        "{Just a quick update:|Wanted to let you know} things are {on track|moving along nicely}. "
        "{Let me know if anything comes up.|Happy to help if you need anything.}\n\n"
        "{Best|Cheers},\n{sender}",

        "Hi {recipient},\n\n{Quick|Small} question: {could you send the latest version later this week?|"
        "did you get a chance to look at the numbers?} {No rush|No hurry at all}, "
        "{whenever works is fine.|Monday is totally fine.}\n\n{Thanks|Appreciate it},\n{sender}",

        "Hello {recipient},\n\n{Following up on our chat.|Picking up where we left off.} "
        "{I think we can keep this simple|The approach looks solid to me}. "
        "{Want to hop on a quick call?|Let me know your thoughts.}\n\n"
        "{Best regards|Best},\n{sender}",
    ],
    "fr": [
        "Bonjour {recipient},\n\n{Merci pour ton message d'hier.|Content d'avoir echange.} "
        "{Petit point rapide :|Je voulais te dire que} tout {avance bien|est sur les rails}. "
        "{N'hesite pas si besoin.|Dis-moi si tu as des questions.}\n\n"
        "{Bien a toi|Cordialement},\n{sender}",

        "Bonjour {recipient},\n\n{Petite|Courte} question : {peux-tu m'envoyer la derniere version cette semaine ?|"
        "as-tu pu regarder les chiffres ?} {Rien d'urgent|Pas de precipitation}, "
        "{quand tu peux.|lundi c'est parfait.}\n\n{Merci d'avance|Merci},\n{sender}",

        "Bonjour {recipient},\n\n{Je reviens vers toi suite a notre echange.|Pour faire suite.} "
        "{Je pense qu'on peut rester simples|L'approche me parait bonne}. "
        "{On s'appelle rapidement ?|Dis-moi ce que tu en penses.}\n\n"
        "{Cordialement|Bien a toi},\n{sender}",
    ],
}


def render_warmup_email(
    language: str,
    sender_name: str,
    recipient_name: str,
    seed: str | None = None,
) -> tuple[str, str]:
    """
    Render a (subject, body) warmup email in `language` with zero LLM calls.

    Args:
        language:       'nl' | 'en' | 'fr' (falls back to 'en').
        sender_name:    Name to sign off with.
        recipient_name: Greeting name.
        seed:           Optional string to seed spintax selection. A random
                        uuid is used when omitted, so each send varies.

    Returns:
        (subject, body) with all spintax resolved and names substituted.
    """
    lang = language if language in _SUBJECTS else "en"
    seed = seed or uuid.uuid4().hex

    subjects = _SUBJECTS[lang]
    bodies = _BODIES[lang]

    idx_seed_sub = int(seed[:8], 16) if _is_hex(seed[:8]) else abs(hash(seed))
    idx_seed_bod = int(seed[8:16], 16) if _is_hex(seed[8:16]) else abs(hash(seed + "b"))

    subject_tpl = subjects[idx_seed_sub % len(subjects)]
    body_tpl = bodies[idx_seed_bod % len(bodies)]

    # Substitute names FIRST: {sender}/{recipient} are single-option brace
    # blocks and would otherwise be consumed by process_spintax. Plain string
    # replace here is spintax-agnostic; the remaining {a|b} blocks then spin.
    subject_tpl = subject_tpl.replace("{sender}", sender_name).replace("{recipient}", recipient_name)
    body_tpl = body_tpl.replace("{sender}", sender_name).replace("{recipient}", recipient_name)

    subject = process_spintax(subject_tpl, lead_id=seed, step_number=1)
    body = process_spintax(body_tpl, lead_id=seed, step_number=2)

    return subject.strip(), body.strip()


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except (ValueError, TypeError):
        return False

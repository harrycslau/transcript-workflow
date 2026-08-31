"""Tag synchronization between YAML configuration and the database.

Synchronization is an upsert by normalized ``name_key``:

- new configured tags create ``Tag`` rows;
- description changes update rows;
- tags removed from YAML are RETIRED (``is_configured=False``), never
  deleted — historical suggestions and assignments keep their FK;
- a removed tag re-added to YAML reactivates the same row (display
  name and history intact).

``sync_tags`` mutates the database and therefore runs ONLY inside
locked mutating commands (``brain summarize``, ``brain tags --sync``);
``brain tags`` without ``--sync`` is genuinely read-only.
"""

from __future__ import annotations

from django.db import transaction

from brainlib.config import AppConfig, tag_name_key
from workflow.models import Tag


def sync_tags(config: AppConfig) -> dict[str, int]:
    """Synchronize ``Tag`` rows with ``config.tags.allowed``. Idempotent.

    Returns counts: ``created``, ``updated`` (description changes),
    ``retired`` (removed from config), ``reactivated`` (re-added).
    """
    configured: dict[str, str] = {}
    for spec in config.tags.allowed:
        configured[tag_name_key(spec.name)] = spec.description

    with transaction.atomic():
        existing = {tag.name_key: tag for tag in Tag.objects.select_for_update()}
        counts = {"created": 0, "updated": 0, "retired": 0, "reactivated": 0}
        for key, description in configured.items():
            tag = existing.get(key)
            if tag is None:
                display = next(
                    (spec.name for spec in config.tags.allowed if tag_name_key(spec.name) == key), key
                )
                Tag.objects.create(name=display, name_key=key, description=description, is_configured=True)
                counts["created"] += 1
                continue
            changed = False
            if not tag.is_configured:
                tag.is_configured = True
                counts["reactivated"] += 1
                changed = True
            if tag.description != description:
                tag.description = description
                counts["updated"] += 1
                changed = True
            # Display name is preserved from first synchronization;
            # config re-spellings never rename history.
            if changed:
                tag.save()
        for key, tag in existing.items():
            if key not in configured and tag.is_configured:
                tag.is_configured = False
                tag.save()
                counts["retired"] += 1
    return counts


def configured_tags() -> dict[str, Tag]:
    """Configured (non-retired) tags keyed by ``name_key``."""
    return {tag.name_key: tag for tag in Tag.objects.filter(is_configured=True)}

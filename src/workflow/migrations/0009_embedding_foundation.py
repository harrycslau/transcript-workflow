"""Step 5B.2: Embedding storage foundation — generation + document models.

Creates the two embedding-store tables only; SCHEMA-ONLY by design:

- ``workflow_embedding_generation`` (:class:`EmbeddingGeneration`): one
  row per embedding-model generation (``model``, ``dimensions``,
  ``embedding_version``, ``source_index_version``), with a DB-enforced
  lifecycle (at most one ``active`` via a partial unique index; a
  state/shape CHECK that doubles as the explicit state allowlist;
  chronology CHECKs ``completed_at <= activated_at`` and
  ``activated_at <= superseded_at``; dimensions bounds 1..16384; identity
  fields non-empty). Duplicate identity tuples are intentionally allowed
  so a same-contract rebuild can coexist with the old active generation.
  ``embedding_version`` binds the FULL embedding implementation contract
  (deterministic text preparation + vector mapping, versioned by Step
  5B.3) on a distinct axis from ``source_index_version`` (the
  ``SearchDocument`` index contract).
- ``workflow_embedding_document`` (:class:`EmbeddingDocument`): one row
  per embedded vector. ``document_key`` is COPIED from
  ``SearchDocument.document_key`` (no FK / pk dependency, so embedding
  rows survive search-index rebuilds and never block source deletion);
  ``source_content_hash`` binds the source content; ``vector_blob`` is
  the portable raw little-endian IEEE-754 float32 encoding (see
  ``workflow.services.vector_codec``). Unique per
  (generation, document_key); key/hash/vector must be non-empty.

There is NO ``RunPython``, no backfill and no network/embedding-client
use: applying it never reads or writes source data and never contacts
any endpoint. Migration 0009 is fully reversible — the reverse drops
only the two embedding tables and preserves every pre-existing table
(``SearchDocument`` and all canonical source data untouched).
"""

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workflow", "0008_search_index"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmbeddingGeneration",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "model",
                    models.TextField(
                        help_text="Exact configured/canonical embedding model string"
                    ),
                ),
                (
                    "dimensions",
                    models.PositiveIntegerField(
                        help_text="Per-vector float32 count, 1..16384"
                    ),
                ),
                (
                    "embedding_version",
                    models.CharField(
                        help_text=(
                            "Full embedding implementation contract version (text "
                            "preparation + mapping; defined in Step 5B.3)"
                        ),
                        max_length=16,
                    ),
                ),
                (
                    "source_index_version",
                    models.CharField(
                        help_text="SearchDocument index contract version", max_length=16
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("building", "Building"),
                            ("active", "Active"),
                            ("superseded", "Superseded"),
                            ("failed", "Failed"),
                        ],
                        default="building",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("superseded_at", models.DateTimeField(blank=True, null=True)),
                ("failed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "workflow_embedding_generation",
                "ordering": ["-created_at"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("state", "active")),
                        fields=("state",),
                        name="uniq_active_embedding_generation",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("dimensions__gte", 1), ("dimensions__lte", 16384)
                        ),
                        name="chk_embedding_generation_dimensions_bounds",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("model", ""), _negated=True),
                            models.Q(("embedding_version", ""), _negated=True),
                            models.Q(("source_index_version", ""), _negated=True),
                        ),
                        name="chk_embedding_generation_identity_nonempty",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("activated_at__isnull", True),
                                ("completed_at__isnull", True),
                                ("failed_at__isnull", True),
                                ("state", "building"),
                                ("superseded_at__isnull", True),
                            ),
                            models.Q(
                                ("activated_at__isnull", False),
                                ("completed_at__isnull", False),
                                ("failed_at__isnull", True),
                                ("state", "active"),
                                ("superseded_at__isnull", True),
                            ),
                            models.Q(
                                ("activated_at__isnull", False),
                                ("completed_at__isnull", False),
                                ("failed_at__isnull", True),
                                ("state", "superseded"),
                                ("superseded_at__isnull", False),
                            ),
                            models.Q(
                                ("activated_at__isnull", True),
                                ("completed_at__isnull", True),
                                ("failed_at__isnull", False),
                                ("state", "failed"),
                                ("superseded_at__isnull", True),
                            ),
                            _connector="OR",
                        ),
                        name="chk_embedding_generation_lifecycle_shape",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("activated_at__isnull", False), _negated=True),
                            ("completed_at__isnull", True),
                            ("completed_at__lte", models.F("activated_at")),
                            _connector="OR",
                        ),
                        name="chk_embedding_generation_completed_before_activated",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("superseded_at__isnull", False), _negated=True),
                            ("activated_at__lte", models.F("superseded_at")),
                            _connector="OR",
                        ),
                        name="chk_embedding_generation_activated_before_superseded",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="EmbeddingDocument",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "document_key",
                    models.TextField(
                        help_text=(
                            "Copied SearchDocument.document_key at embedding time "
                            "(no FK)"
                        )
                    ),
                ),
                (
                    "source_content_hash",
                    models.CharField(
                        help_text="sha256 of the source SearchDocument content",
                        max_length=64,
                    ),
                ),
                (
                    "vector_blob",
                    models.BinaryField(
                        help_text="Raw little-endian IEEE-754 float32 vector"
                    ),
                ),
                (
                    "embedded_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "generation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="documents",
                        to="workflow.embeddinggeneration",
                    ),
                ),
            ],
            options={
                "db_table": "workflow_embedding_document",
                "ordering": ["generation", "document_key"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("generation", "document_key"),
                        name="uniq_embedding_document_per_generation",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("document_key", ""), _negated=True),
                            models.Q(("source_content_hash", ""), _negated=True),
                            models.Q(("vector_blob", b""), _negated=True),
                        ),
                        name="chk_embedding_document_fields_nonempty",
                    ),
                ],
            },
        ),
    ]

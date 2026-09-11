"""Tag definition provenance: config/custom origin for Tag rows.

Adds ``Tag.definition_origin`` (choices ``config``/``custom``, default
``config``) so user-created custom tags are distinguishable from YAML
config-owned definitions, plus a DB CHECK allowlist constraint. Existing
rows become ``config`` (their only writer was YAML sync). Custom tags
are created with origin ``custom`` and ``is_configured=True``;
``tags --sync`` promotes a custom tag to config-owned when a configured
name normalizes to its ``name_key`` (the same row keeps assignments and
history) and retires only absent config-owned tags, never custom tags.

Fully reversible: reverse drops the field and the constraint only; no
data migration and no ``RunPython``.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workflow', '0009_embedding_foundation'),
    ]

    operations = [
        migrations.AddField(
            model_name='tag',
            name='definition_origin',
            field=models.CharField(choices=[('config', 'Config'), ('custom', 'Custom')], default='config', help_text='Provenance of the definition: config (YAML tags.allowed) or custom (user-created)', max_length=16),
        ),
        migrations.AddConstraint(
            model_name='tag',
            constraint=models.CheckConstraint(condition=models.Q(('definition_origin__in', ['config', 'custom'])), name='chk_tag_definition_origin_allowlist'),
        ),
    ]

"""Backfill Recording.summary_status for pre-Step-3 rows.

No summaries exist yet (Step 2 database state), so the mapping is:
``missing`` where an active transcript exists, else ``not_ready``.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Recording = apps.get_model("workflow", "Recording")
    Recording.objects.filter(transcripts__is_active=True).update(summary_status="missing")
    Recording.objects.exclude(transcripts__is_active=True).update(summary_status="not_ready")


def backwards(apps, schema_editor):
    Recording = apps.get_model("workflow", "Recording")
    Recording.objects.all().update(summary_status="not_ready")


class Migration(migrations.Migration):

    dependencies = [
        ("workflow", "0003_tag_recording_resummarization_failed_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

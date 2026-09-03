from django.apps import AppConfig


class WorkflowConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "workflow"
    verbose_name = "Brain Workflow"

    def ready(self):
        # Register the Unicode-aware SQLite collation on every connection
        # (app, test, CLI and server) before any query runs.
        from workflow import sqlite_unicode

        sqlite_unicode.connect()

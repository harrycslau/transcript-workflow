"""Django forms for the Step 4 web interface."""

from __future__ import annotations

from django import forms


class TagAddForm(forms.Form):
    """Manual tag assignment.

    The selector offers configured tags; retired tags appear only when
    the explicit "include retired tags" opt-in is set (a deliberate
    restore of an existing historical assignment).
    """

    tag = forms.ChoiceField(label="Tag")
    include_retired = forms.BooleanField(
        required=False,
        label="Include retired tags (deliberate restore)",
    )

    def __init__(self, *, configured, retired, **kwargs):
        super().__init__(**kwargs)
        include = False
        if self.is_bound:
            include = str(self.data.get("include_retired", "")).lower() in ("on", "true", "1")
        elif self.initial.get("include_retired"):
            include = True
        pool = list(configured) + (list(retired) if include else [])
        self.fields["tag"].choices = [("", "Choose a tag…")] + [(tag.pk, tag.name) for tag in pool]
        self._tag_map = {str(tag.pk): tag for tag in pool}

    def clean(self):
        cleaned = super().clean()
        raw = cleaned.get("tag")
        tag = self._tag_map.get(str(raw))
        if tag is None:
            raise forms.ValidationError("Choose a valid tag.")
        cleaned["tag_obj"] = tag
        return cleaned


class RouteForm(forms.Form):
    """Manual routing profile selection (choices come from validated config)."""

    profile = forms.ChoiceField(label="Routing profile")

    def __init__(self, *, config, **kwargs):
        super().__init__(**kwargs)
        choices = [("", "Choose a profile…")]
        for profile in sorted(config.macwhisper.routing.profiles.values(), key=lambda p: p.name):
            language = profile.language if profile.language is not None else "auto"
            suffix = " (manual-only)" if profile.manual_only else ""
            choices.append((profile.name, f"{profile.name}{suffix} — model {profile.model}, language {language}"))
        self.fields["profile"].choices = choices


class ActionConfirmForm(forms.Form):
    """Hidden state echoed by every action form (fingerprint + extras)."""

    fingerprint = forms.CharField(required=False, widget=forms.HiddenInput)
    confirmed = forms.CharField(required=False, widget=forms.HiddenInput)
    mode = forms.CharField(required=False, widget=forms.HiddenInput)
    profile = forms.CharField(required=False, widget=forms.HiddenInput)

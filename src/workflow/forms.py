"""Django forms for the Step 4 web interface."""

from __future__ import annotations

import unicodedata

from django import forms

from brainlib.config import tag_name_key

_TAG_NAME_MAX = 64


class TagAddForm(forms.Form):
    """Manual tag assignment.

    The selector offers available (non-retired) definitions — config-owned
    and custom; retired tags appear only when the explicit "include
    retired tags" opt-in is set (a deliberate restore of an existing
    historical assignment).
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


class CustomTagForm(forms.Form):
    """Create a global reusable custom tag and assign it to the recording.

    The service (:func:`workflow.services.tags.create_custom_tag_and_assign`)
    is the authoritative validator; this form provides early, friendly
    client-side validation with the same rules (blank, control
    characters/newlines, ``Tag.name`` max length, normalized-key length).
    """

    name = forms.CharField(
        label="New tag name",
        max_length=_TAG_NAME_MAX,
        strip=True,
        error_messages={"required": "Enter a tag name."},
        widget=forms.TextInput(
            attrs={
                "placeholder": "New tag name",
                "maxlength": str(_TAG_NAME_MAX),
                "autocomplete": "off",
            }
        ),
    )

    def clean_name(self):
        name = self.cleaned_data["name"]
        if not name:
            raise forms.ValidationError("Enter a tag name.")
        if any(unicodedata.category(ch).startswith("C") for ch in name):
            raise forms.ValidationError("Tag name must not contain control characters or newlines.")
        if len(tag_name_key(name)) > _TAG_NAME_MAX:
            raise forms.ValidationError("Tag name is too long after normalization.")
        return name


class TagSelectionForm(forms.Form):
    """Bulk atomic tag selection for the + Add tag modal Done button.

    Repeated checkbox fields carry the COMPLETE desired active set
    (``selected_tags`` = available definitions, ``selected_retired_tags``
    = the explicit retired opt-in) plus an optional new custom tag name.
    The service (:func:`workflow.services.tags.apply_tag_selection`) is
    the authoritative validator under races; this form bounds the choice
    sets to the current tag pool and validates early with friendly,
    value-free errors.
    """

    selected_tags = forms.MultipleChoiceField(
        required=False,
        label="Tags",
        widget=forms.CheckboxSelectMultiple,
    )
    selected_retired_tags = forms.MultipleChoiceField(
        required=False,
        label="Retired tags",
        widget=forms.CheckboxSelectMultiple,
    )
    new_tag_name = forms.CharField(
        required=False,
        label="New tag name",
        max_length=_TAG_NAME_MAX,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "placeholder": "New tag name",
                "maxlength": str(_TAG_NAME_MAX),
                "autocomplete": "off",
            }
        ),
    )

    def __init__(self, *, available, retired, **kwargs):
        super().__init__(**kwargs)
        self.fields["selected_tags"].choices = [
            (tag.pk, tag.name) for tag in available
        ]
        self.fields["selected_retired_tags"].choices = [
            (tag.pk, tag.name) for tag in retired
        ]

    def clean_new_tag_name(self):
        name = self.cleaned_data["new_tag_name"]
        if not name:
            return ""
        if any(unicodedata.category(ch).startswith("C") for ch in name):
            raise forms.ValidationError("Tag name must not contain control characters or newlines.")
        if len(tag_name_key(name)) > _TAG_NAME_MAX:
            raise forms.ValidationError("Tag name is too long after normalization.")
        return name


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

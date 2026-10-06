from pathlib import Path

from django import forms
from django.utils.translation import gettext_lazy as _

from sms_sender.input_loader import SLUG_RE, segment_from_path

from ..text import persian_text
from . import files
from .models import Segment

UPLOAD_ERRORS = {
    "excel": _("This is an Excel file. In Excel, save it as “CSV UTF-8” and upload that."),
    "binary": _("This doesn't look like a text file. Upload a CSV or TXT file."),
    "too_big": _("The file is larger than 50 MB."),
    "empty": _("The file has no rows."),
}
MAPPING_ERRORS = {
    "unknown_column": _("Choose the columns again; the page was out of date."),
    "column_twice": _("A column can have only one use."),
    "unnamed_column": _("A chosen column has no name in the first row. Name it in the file, or untick “the first row holds the column names”."),
    "duplicate_name": _("Two chosen columns have the same name, or one is named “phone”. Rename it in the file."),
    "no_rows": _("The file has column names but no rows."),
}
# A slug that would collide with the page addresses under /segments/.
RESERVED_SLUGS = {"upload"}


def clean_slug(slug: str) -> str:
    if not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
        raise forms.ValidationError(
            _("Use lowercase English letters, digits and dashes, e.g. vip-users.")
        )
    return slug


class ReplaceForm(forms.Form):
    """Just the file: what an upload checks."""
    file = forms.FileField()

    def clean(self):
        data = super().clean()
        upload = data.get("file")
        if upload is None:
            return data
        if Path(upload.name).suffix.lower() not in (".csv", ".txt"):
            self.add_error("file", _("Upload a CSV or TXT file. In Excel, use “Save as” → “CSV UTF-8”."))
            return data
        if upload.size > files.MAX_BYTES:
            self.add_error("file", UPLOAD_ERRORS["too_big"])
            return data
        raw = upload.read()
        try:
            data["table"] = files.parse(raw)
        except files.UploadError as e:
            self.add_error("file", UPLOAD_ERRORS[e.code])
            return data
        data["raw"] = raw
        return data


class UploadForm(forms.Form):
    name = forms.CharField(max_length=200, required=False)
    slug = forms.CharField(max_length=64, required=False)
    file = forms.FileField()

    def clean_slug(self) -> str:
        """A short name typed here is checked on its own, so it's checked as
        you type too (live.py); an empty one comes from the file's name."""
        typed = (self.cleaned_data.get("slug") or "").strip()
        if not typed:
            return ""
        slug = clean_slug(typed)
        if Segment.objects.filter(slug=slug).exists():
            raise forms.ValidationError(_("A segment with this short name already exists. Choose another."))
        return slug

    def clean(self):
        data = super().clean()
        upload = data.get("file")
        if upload is None or self.has_error("slug"):
            return data
        if Path(upload.name).suffix.lower() not in (".csv", ".txt"):
            self.add_error("file", _("Upload a CSV or TXT file. In Excel, use “Save as” → “CSV UTF-8”."))
            return data
        if upload.size > files.MAX_BYTES:
            self.add_error("file", UPLOAD_ERRORS["too_big"])
            return data
        raw = upload.read()
        try:
            data["table"] = files.parse(raw)
        except files.UploadError as e:
            self.add_error("file", UPLOAD_ERRORS[e.code])
            return data
        data["raw"] = raw
        slug = (data.get("slug") or "").strip() or segment_from_path(upload.name)
        try:
            data["slug"] = clean_slug(slug)
        except forms.ValidationError as e:
            self.add_error("slug", e)
            return data
        if Segment.objects.filter(slug=slug).exists():
            self.add_error("slug", _("A segment with this short name already exists. Choose another."))
        data["name"] = persian_text((data.get("name") or "").strip()) or slug
        return data


class MappingForm(forms.Form):
    has_header = forms.BooleanField(required=False)
    phone = forms.TypedChoiceField(coerce=int)
    user_id = forms.TypedChoiceField(coerce=int, required=False, empty_value=None)
    tokens = forms.TypedMultipleChoiceField(coerce=int, required=False)

    def __init__(self, *args, width: int, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(i, str(i)) for i in range(width)]
        self.fields["phone"].choices = choices
        self.fields["phone"].error_messages["required"] = _("Choose the column with the mobile numbers.")
        self.fields["user_id"].choices = [("", "")] + choices
        self.fields["tokens"].choices = choices

    def mapping(self) -> files.Mapping:
        d = self.cleaned_data
        return files.Mapping(
            has_header=d["has_header"], phone=d["phone"], user_id=d["user_id"],
            tokens=tuple(d["tokens"]),
        )

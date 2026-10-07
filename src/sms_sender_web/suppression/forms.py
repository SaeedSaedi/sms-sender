import re

from django import forms
from django.utils.translation import gettext_lazy as _

from sms_sender.phone import InvalidPhoneError, normalize

from ..segments import files
from ..segments.forms import UPLOAD_ERRORS

# One number per line; commas and semicolons (Latin or Persian) separate too.
_SEPARATORS = re.compile(r"[\r\n,;،؛]+")


def _phone(text: str) -> str | None:
    try:
        return normalize(text)
    except InvalidPhoneError:
        return None


class AddForm(forms.Form):
    numbers = forms.CharField(widget=forms.Textarea, required=False)
    file = forms.FileField(required=False)
    note = forms.CharField(max_length=200, required=False)
    # Empty: every campaign. A campaign's short name: only that one (the
    # CLI's --opt-out for one campaign, numbers or a file).
    scope = forms.CharField(max_length=64, required=False)

    def clean_scope(self):
        from ..jobs.models import Campaign

        slug = (self.cleaned_data.get("scope") or "").strip()
        if not slug:
            return None
        campaign = Campaign.objects.filter(slug=slug).first()
        if campaign is None:
            raise forms.ValidationError(_("Choose a campaign from the list."))
        return campaign

    def clean(self):
        data = super().clean()
        phones: set[str] = set()
        invalid = 0
        for part in _SEPARATORS.split(data.get("numbers") or ""):
            if part.strip():
                phone = _phone(part)
                if phone:
                    phones.add(phone)
                else:
                    invalid += 1
        upload = data.get("file")
        if upload is not None:
            try:
                # The limit and a byte: enough to refuse a file too big without reading it all.
                table = files.parse(upload.read(files.MAX_BYTES + 1))
            except files.UploadError as e:
                self.add_error("file", UPLOAD_ERRORS[e.code])
                return data
            # Each row's first cell that is a mobile number; a first row
            # without one is a header.
            for n, row in enumerate(table.rows):
                phone = next((p for p in map(_phone, row) if p), None)
                if phone:
                    phones.add(phone)
                elif n:
                    invalid += 1
        if not phones and not invalid and "file" not in self.errors:
            raise forms.ValidationError(_("Enter at least one number, or choose a file."))
        data["phones"], data["invalid"] = phones, invalid
        return data

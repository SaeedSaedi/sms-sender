import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext_lazy as _

from .roles import ROLES
from .terms import ROLE_LABELS

# A Persian keyboard types ۰–۹ (and some apps paste ٠–٩); the code is ASCII.
_TO_ASCII = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class CodeForm(forms.Form):
    code = forms.CharField(max_length=16)

    def clean_code(self) -> str:
        code = re.sub(r"\s", "", self.cleaned_data["code"]).translate(_TO_ASCII)
        if not re.fullmatch(r"\d{6}", code):
            raise forms.ValidationError(_("Enter the six-digit code from your authenticator app."))
        return code


class TestPhoneForm(forms.Form):
    test_phone = forms.CharField(max_length=32, required=False)

    def clean_test_phone(self) -> str:
        from sms_sender.phone import InvalidPhoneError, normalize

        raw = self.cleaned_data["test_phone"].strip()
        if not raw:
            return ""
        try:
            return normalize(raw)
        except InvalidPhoneError as e:
            raise forms.ValidationError(_("That isn't a valid mobile number.")) from e


class NewUserForm(forms.Form):
    username = forms.CharField(max_length=150)
    password = forms.CharField(widget=forms.PasswordInput)
    role = forms.ChoiceField(choices=[(r, ROLE_LABELS[r]) for r in ROLES])

    def clean_username(self) -> str:
        username = self.cleaned_data["username"].strip()
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(_("This username is already taken."))
        return username

    def clean(self):
        data = super().clean()
        if data.get("password"):
            # Django's rules from settings (length, common, numeric, too like
            # the username); the catalog words their messages in Persian.
            try:
                validate_password(data["password"], get_user_model()(username=data.get("username", "")))
            except forms.ValidationError as error:
                self.add_error("password", error)
        return data

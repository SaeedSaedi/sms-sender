from django.apps import AppConfig


class CampaignsConfig(AppConfig):
    """The campaign pages. The models (Campaign, Job) are in `jobs`."""

    name = "sms_sender_web.campaigns"
    label = "campaigns"

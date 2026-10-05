from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/campaigns/", views.campaigns, name="api_campaigns"),
    path("api/v1/campaigns/<slug:slug>/attribution/", views.attribution, name="api_attribution"),
    path("api-tokens/", views.token_list, name="api_tokens"),
    path("api-tokens/<int:pk>/revoke/", views.token_revoke, name="api_token_revoke"),
]

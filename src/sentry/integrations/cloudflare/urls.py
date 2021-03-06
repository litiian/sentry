from __future__ import absolute_import, print_function

from django.conf.urls import patterns, url

from .metadata import CloudflareMetadataEndpoint
from .webhook import CloudflareWebhookEndpoint


urlpatterns = patterns(
    '',
    url(r'^metadata/$', CloudflareMetadataEndpoint.as_view()),
    url(r'^webhook/$', CloudflareWebhookEndpoint.as_view()),
)

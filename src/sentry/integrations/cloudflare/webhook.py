from __future__ import absolute_import

import hmac
import logging
import six

from django.utils.crypto import constant_time_compare
from functools import wraps
from hashlib import sha256
from rest_framework.response import Response

from sentry import options
from sentry.api.authentication import TokenAuthentication
from sentry.api.base import Endpoint
from sentry.models import Organization, Project, ProjectKey, Team
from sentry.utils import json

logger = logging.getLogger('sentry.integrations.cloudflare')


def requires_auth(func):
    @wraps(func)
    def wrapped(self, request, *args, **kwargs):
        if not request.user.is_authenticated():
            return Response({
                'proceed': False,
            }, 401)
        return func(self, request, *args, **kwargs)
    return wrapped


class CloudflareWebhookEndpoint(Endpoint):
    permission_classes = ()

    def verify(self, payload, key, signature):
        return constant_time_compare(
            signature,
            hmac.new(
                key=key.encode('utf-8'),
                msg=payload.encode('utf-8'),
                digestmod=sha256,
            ).hexdigest()
        )

    def authenticate_from_json_token(self, data):
        try:
            token = data['authentications']['account']['token']['token']
        except KeyError:
            return

        auth = TokenAuthentication()
        return auth.authenticate_credentials(token)[1]

    def organization_from_json(self, request, data, scope='project:write'):
        try:
            organization_id = data['install']['options']['organization']
        except KeyError:
            return None

        organizations = Organization.objects.get_for_user(request.user, scope=scope)
        for org in organizations:
            if six.text_type(org.id) == organization_id:
                return org
        return None

    def project_from_json(self, request, data, scope='project:write'):
        try:
            project_id = data['install']['options']['project']
        except KeyError:
            return None

        org = self.organization_from_json(request, data)

        projects = Project.objects.filter(
            organization=org,
            team__in=Team.objects.get_for_user(org, request.user, scope='project:write'),
        )
        for project in projects:
            if six.text_type(project.id) == project_id:
                return project
        return None

    def on_preview(self, request, data, is_test):
        if not request.user.is_authenticated():
            return Response({
                'install': data['install'],
                'proceed': True
            })

        return self.on_account_change(request, data, is_test)

    @requires_auth
    def on_account_change(self, request, data, is_test):
        organizations = Organization.objects.get_for_user(request.user, scope='project:write')

        data['install']['schema']['properties']['organization'] = {
            'type': 'string',
            'title': 'Sentry Organization',
            'order': 1,
            'enum': [six.text_type(o.id) for o in organizations],
            'enumNames': {
                six.text_type(o.id): o.slug for o in organizations
            },
            'required': True,
        }
        if organizations:
            data['install']['options']['organization'] = data['install']['schema']['properties']['organization']['enum'][0]
            return self.on_organization_change(request, data, is_test)

        return Response({
            'install': data['install'],
            'proceed': True
        })

    @requires_auth
    def on_organization_change(self, request, data, is_test):
        org = self.organization_from_json(request, data)

        projects = list(Project.objects.filter(
            organization=org,
            team__in=Team.objects.get_for_user(org, request.user, scope='project:write'),
        ))

        data['install']['schema']['properties']['project'] = {
            'type': 'string',
            'title': 'Sentry Project',
            'order': 2,
            'enum': [six.text_type(o.id) for o in projects],
            'enumNames': {
                six.text_type(o.id): o.slug for o in projects
            },
            'required': True,
        }
        if projects:
            data['install']['options']['project'] = data['install']['schema']['properties']['project']['enum'][0]
            return self.on_project_change(request, data, is_test)

        return Response({
            'install': data['install'],
            'proceed': True
        })

    @requires_auth
    def on_project_change(self, request, data, is_test):
        project = self.project_from_json(request, data)

        keys = list(ProjectKey.objects.filter(
            project=project,
        ))

        data['install']['schema']['properties']['dsn'] = {
            'type': 'string',
            'title': 'DSN',
            'description': 'Your automatically configured DSN for communicating with Sentry.',
            'placeholder': 'https://public_key@sentry.io/1',
            'order': 3,
            'enum': [o.get_dsn(public=True) for o in keys],
            'required': True,
        }
        if keys:
            data['install']['options']['dsn'] = data['install']['schema']['properties']['dsn']['enum'][0]

        return Response({
            'install': data['install'],
            'proceed': True
        })

    def post(self, request):
        signature = request.META.get('HTTP_X_SIGNATURE_HMAC_SHA256_HEX')
        variant = request.META.get('HTTP_X_SIGNATURE_KEY_VARIANT')
        logging_data = {
            'user_id': request.user.id if request.user.is_authenticated() else None,
            'signature': signature,
            'variant': variant,
        }

        payload = request.body
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            logger.error('cloudflare.webhook.invalid-json', extra=logging_data)
            return Response(status=400)

        # check for secondary auth
        if not request.user.is_authenticated():
            token = self.authenticate_from_json_token(data)
            if token is not None:
                request.user = token.user
                request.auth = token
                logging_data['user_id'] = token.user.id

        event = data.get('event')
        logger.info('cloudflare.webhook.{}'.format(event), extra=logging_data)
        if not signature:
            logger.error('cloudflare.webhook.invalid-signature', extra=logging_data)
            return Response(status=400)
        if not variant:
            logger.error('cloudflare.webhook.invalid-variant', extra=logging_data)
            return Response(status=400)

        if variant == 'test':
            key = 'test-key'
        elif variant == '1':
            key = options.get('cloudflare.secret-key')
        else:
            logger.error('cloudflare.webhook.invalid-variant', extra=logging_data)
            return Response(status=400)

        app_id = data.get('app', {}).get('id')
        if app_id not in ('local', '') and variant == 'test':
            logger.error('cloudflare.webhook.invalid-variant', extra=logging_data)
            return Response(status=400)

        if not self.verify(payload, key, signature):
            logger.error('cloudflare.webhook.invalid-signature'.format(event), extra=logging_data)
            return Response(status=400)

        if event == 'option-change:account':
            return self.on_account_change(request, data, is_test=variant == 'test')
        if event == 'option-change:organization':
            return self.on_organization_change(request, data, is_test=variant == 'test')
        if event == 'option-change:project':
            return self.on_project_change(request, data, is_test=variant == 'test')
        elif event == 'preview':
            return self.on_preview(request, data, is_test=variant == 'test')
        return Response({
            'install': data['install'],
            'proceed': True
        })

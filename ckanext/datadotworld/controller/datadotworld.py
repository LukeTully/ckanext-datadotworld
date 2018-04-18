# Copyright 2017 data.world, inc
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import ckan.lib.base as base
import ckan.model as model
import ckan.logic as logic
import ckan.plugins.toolkit as tk
from ckanext.datadotworld.model.credentials import Credentials
from ckanext.datadotworld.model.extras import Extras
from ckan.common import _, request, c
import ckan.lib.helpers as h
from ckanext.datadotworld.api import API
from ckanext.datadotworld.api import compat_enqueue
from ckanext.datadotworld.api import syncronize
from pylons import config
import os
from ckan.lib.celery_app import celery
from sqlalchemy import func
import ckanext.datadotworld.helpers as dh

logger = logging.getLogger(__name__)


def syncronize_org(id):
    ckan_ini_filepath = os.path.abspath(config['__file__'])
    packages = model.Session.query(model.Package).filter_by(
        owner_org=id
    )
    for pkg in packages:
        compat_enqueue(
            'datadotworld.syncronize',
            syncronize,
            args=[pkg.id, ckan_ini_filepath])


class DataDotWorldController(base.BaseController):
    def list_sync(self, state, org_id=None):
        orgs = dh.admin_in_orgs(c.user)
        org = model.Group.get(org_id)
        if not orgs or (org and org not in orgs):
            base.abort(401, _('User %r not authorized to see this page') % (
                c.user))
        extra = {
            'displayed_state': state
        }
        ids = [o.id for o in orgs]
        query = model.Session.query(
            model.Package.name,
            model.Package.title,
            Extras.message
        ).join(
            model.Group, model.Package.owner_org == model.Group.id
        ).join(
            Extras
        ).filter(
            Extras.state == state
        )
        if org:
            query = query.filter(
                model.Group.id == org.id
            )
        else:
            query = query.filter(
                model.Group.id.in_(ids)
            )
        extra['datasets'] = query.all()
        for pkg in extra['datasets']:
            try:
                pkg.message = json.loads(pkg.message)
            except Exception:
                pkg.message = {
                    'RAW message': pkg.message
                }
        return base.render('datadotworld/list_sync.html', extra_vars=extra)

    def edit(self, id):
        def validate(data):
            error_dict = {}
            has_owner = data.get('owner')
            has_key = data.get('key')

            if tk.asbool(data.get('integration', 'False')):
                if not has_owner:
                    error_dict['owner'] = ['Required']
                if not has_key:
                    error_dict['key'] = ['Required']
            if tk.asbool(data.get('show_links', 'False')):
                if not has_owner or not has_key:
                    error_dict['show_links'] = [
                        'This option available only '
                        'if credentials are provided']
            if not error_dict:
                api = API(has_owner, has_key)
                check = api.check_credentials()
                if not check:
                    error_dict['key'] = ['Incorrect key']
            if error_dict:
                raise logic.ValidationError(error_dict)


        context = {
            'model': model,
            'session': model.Session,
            'user': c.user or c.author,
            'auth_user_obj': c.userobj}
        data_dict = {
            'id': id
        }
        stats = {}
        extra = {
            'errors': {},
            'error_summary': None,
            'stats': stats
        }
        try:
            if not h.check_access('organization_update', data_dict):
                raise logic.NotAuthorized
            c.group_dict = logic.get_action('organization_show')(context, data_dict)
            c.group = context['group']
            c.credentials = c.group.datadotworld_credentials
            if c.credentials is None:
                c.credentials = Credentials(
                    organization=c.group
                )
                model.Session.add(c.credentials)
        except logic.NotFound:
            base.abort(404, _('Organization not found'))
        except logic.NotAuthorized:
            base.abort(401, _('User %r not authorized to edit %s') % (
                c.user, id))
        if request.method == 'POST':
            data = dict(request.POST)
            c.credentials.update(data)
            try:
                validate(data)
            except logic.ValidationError as e:
                extra['errors'] = e.error_dict
                extra['error_summary'] = e.error_summary
            else:

                query = model.Session.query(Extras).join(
                    model.Package
                ).join(
                    model.Group, model.Package.owner_org == model.Group.id
                ).filter(model.Group.id == c.group.id)
                for item in query:
                    item.state = 'pending'

                model.Session.commit()
                h.flash_success('Saved')
                if tk.asbool(c.credentials.integration):
                    syncronize_org(c.group.id)
                return base.redirect_to('organization_dataworld', id=id)

        query = model.Session.query(
            func.count(model.Package.id).label('total'),
            Extras.state
        ).join(model.Group, model.Package.owner_org == model.Group.id).join(
            Extras
        ).group_by(Extras.state).filter(model.Package.owner_org == c.group.id)

        for amount, state in query:
            stats[state] = amount
        return base.render(
            'organization/edit_credentials.html', extra_vars=extra)

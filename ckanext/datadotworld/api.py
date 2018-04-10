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
import os.path
import logging
import time

import requests
from bleach import clean
from markdown import markdown
from webhelpers.text import truncate

import ckan.model as model
from ckan.logic import get_action
from ckan.lib.munge import munge_name

from ckanext.datadotworld.model import States
from ckanext.datadotworld.model.extras import Extras
from ckanext.datadotworld import __version__
from pylons import config
import re
from ckan.lib.helpers import url_for
from ckan.lib.helpers import date_str_to_datetime
from ckan.lib.helpers import render_datetime

log = logging.getLogger(__name__)
licenses = {
    'cc-by': 'CC-BY',
    'other-pd': 'Public Domain',
    'odc-pddl': 'PDDL',
    'cc-zero': 'CC-0',
    'odc-by': 'ODC-BY',
    'cc-by-sa': 'CC-BY-SA',
    'odc-odbl': 'ODC-ODbL',
    'cc-nc': 'CC BY-NC',
    # 'CC BY-NC-SA',
}

def compat_enqueue(name, fn, args=None):
    u'''
    Enqueue a background job using Celery or RQ.
    '''
    try:
        # Try to use RQ
        from ckan.lib.jobs import enqueue
        enqueue(fn, args=args)
    except ImportError:
        # Fallback to Celery
        from ckan.lib.celery_app import celery
        celery.send_task(name, args=args)

def load_config(ckan_ini_filepath):
    import os
    import paste.deploy
    config_abs_path = os.path.abspath(ckan_ini_filepath)
    conf = paste.deploy.appconfig('config:' + config_abs_path)
    import ckan
    ckan.config.environment.load_environment(conf.global_conf,
                                             conf.local_conf)


def register_translator():
    # https://github.com/ckan/ckanext-archiver/blob/master/ckanext/archiver/bin/common.py
    # If not set (in cli access), patch the a translator with a mock, so the
    # _() functions in logic layer don't cause failure.
    from paste.registry import Registry
    from pylons import translator
    from ckan.lib.cli import MockTranslator
    if 'registery' not in globals():
        global registry
        registry = Registry()
        registry.prepare()

    if 'translator_obj' not in globals():
        global translator_obj
        translator_obj = MockTranslator()
        registry.register(translator, translator_obj)
        
def syncronize(id, ckan_ini_filepath, attempt=0):
    load_config(ckan_ini_filepath)
    register_translator()
    notify(id, attempt)


def get_context():
    return {'ignore_auth': True}


def dataworld_name(title):
    cleaned_title = ' '.join(title.split()).replace('_', '-').replace(' ', '-')
    return munge_name(
        '-'.join(filter(None, cleaned_title.split('-')))
    )


def datadotworld_tags_name_normalize(tags_list):
    tags_list = [tag['name'].lower().replace('-', ' ').replace('_', ' ')
                 for tag in tags_list if (len(tag['name']) > 1 and
                                          len(tag['name']) <= 25)]
    tagname_match = re.compile('^[a-z0-9]+( [a-z0-9]+)*$')
    tags_list = [tag for tag in tags_list if tagname_match.match(tag)]
    tags_list = list(set(tags_list))
    return tags_list


def _get_creds_if_must_sync(pkg_dict):
    owner_org = pkg_dict.get('owner_org')
    org = model.Group.get(owner_org)
    if org is None:
        return
    credentials = org.datadotworld_credentials
    if credentials is None or not credentials.integration:
        return
    return credentials


def notify(pkg_id, attempt=0):
    pkg_dict = get_action('package_show')(get_context(), {'id': pkg_id})
    if pkg_dict.get('type', 'dataset') != 'dataset':
        return False
    credentials = _get_creds_if_must_sync(pkg_dict)
    if not credentials:
        return False
    if pkg_dict.get('state') == 'draft':
        return False
    api = API(credentials.owner, credentials.key)
    api.sync(pkg_dict, attempt)
    return True


def _prepare_resource_url(res):
    """Convert list of resources to files_list for data.world.
    """
    link = res['url'] or ''
    name = res['name'] or ''
    link_name, link_ext = os.path.splitext(os.path.basename(link))
    file_name, file_ext = os.path.splitext(os.path.basename(name))

    existing_format = res.get('format')
    if existing_format:
        ext = '.' + existing_format.lower()
    elif file_ext:
        ext = file_ext
    else:
        ext = link_ext.split('#').pop(0).split('?').pop(0)

    prepared_data = dict(
        name=(file_name or link_name) + ext,
        source=dict(
            url=link,
            expandArchive=True
        )
    )
    description = res.get('description', '')

    if description:

        prepared_data['description'] = truncate(
            description, 120, whole_word=True)

    return prepared_data

def _delay_request():
    request_delay = config.get(
        'ckan.datadotworld.request_delay', 1)
    try:
        request_delay = float(request_delay)
    except Exception as e:
        return False
    if (request_delay > 0):
        time.sleep(request_delay)

    return True


def _repeat_request(pkg_id, attempt):
    attempt += 1
    max_attempt = config.get(
        'ckan.datadotworld.max_request_attempt', 10)
    try:
        max_attempt = int(max_attempt) - 1
    except Exception as e:
        log.info('Wrong variable format for max_request_attempt.')
        return
    if attempt > max_attempt:
        log.info('Max request attempt ({0}) achieved for {1}.'.format(max_attempt, pkg_id))
        return
    ckan_ini_filepath = os.path.abspath(config['__file__'])
    compat_enqueue(
        'datadotworld.syncronize',
        syncronize,
        args=[pkg_id, ckan_ini_filepath, attempt])

def dataset_footnote(pkg_dict):
    dataset_url = url_for(controller='package', action='read', id=pkg_dict.get('id'), qualified=True)
    source_str = 'Source: {0}'.format(dataset_url)
    dataset_date = date_str_to_datetime(pkg_dict.get('metadata_modified'))
    date_str = 'Last updated at {0} : {1}'.format(
        url_for(controller='home', action='index', qualified=True), 
        render_datetime(dataset_date, '%Y-%m-%d'))
    return '\n\n{0}  \r\n{1}'.format(source_str, date_str)



def dataset_footnote(pkg_dict):
    dataset_url = url_for(controller='package', action='read', id=pkg_dict.get('id'), qualified=True)
    source_str = 'Source: {0}'.format(dataset_url)
    dataset_date = date_str_to_datetime(pkg_dict.get('metadata_modified'))
    date_str = 'Last updated at {0} : {1}'.format(
        url_for(controller='home', action='index', qualified=True), 
        render_datetime(dataset_date, '%Y-%m-%d'))
    return '\n\n{0}  \r\n{1}'.format(source_str, date_str)


class API:
    root = 'https://data.world'
    api_root = 'https://api.data.world/v0'
    api_create = api_root + '/datasets/{owner}'
    api_create_put = api_create + '/{id}'
    api_update = api_create + '/{name}'
    api_delete = api_create + '/{id}'
    api_res_create = api_update + '/files'
    api_res_sync = api_update + '/sync'
    api_res_update = api_res_create + '/{file}'
    api_res_delete = api_res_create + '/{file}'

    auth = 'Bearer {key}'
    user_agent_header = 'ckanext-datadotworld/' + __version__

    @classmethod
    def generate_link(cls, owner, package=None):
        """Create link to data.world dataset.
        """
        parts = [cls.root, owner]
        if package:
            parts.append(package)
        return '/'.join(parts)

    @staticmethod
    def creds_from_id(org_id):
        """Find data.world credentials by org id.
        """
        org = model.Group.get(org_id)
        if not org:
            return
        return org.datadotworld_credentials

    def __init__(self, owner, key):
        """Initialize client with credentials.
        """
        self.owner = owner
        self.key = key

    def _default_headers(self):
        return {
            'Authorization': self.auth.format(key=self.key),
            'Content-type': 'application/json',
            'User-Agent': self.user_agent_header
        }

    def _get(self, url):
        """Simple wrapper around GET request.
        """
        headers = self._default_headers()
        return requests.get(url=url, headers=headers)

    def _post(self, url, data):
        """Simple wrapper around POST request.
        """
        headers = self._default_headers()
        return requests.post(url=url, data=json.dumps(data), headers=headers)

    def _put(self, url, data):
        """Simple wrapper around PUT request.
        """
        headers = self._default_headers()
        return requests.put(url=url, data=json.dumps(data), headers=headers)

    def _delete(self, url, data):
        """Simple wrapper around DELETE request.
        """
        headers = self._default_headers()
        return requests.delete(url=url, data=json.dumps(data), headers=headers)

    def _format_data(self, pkg_dict):
        notes = pkg_dict.get('notes') or ''
        footnote = dataset_footnote(pkg_dict)
        notes += footnote
        tags = datadotworld_tags_name_normalize(pkg_dict.get('tags', []))
        data = dict(
            title=pkg_dict['name'],
            description=pkg_dict['title'],
            summary=notes,
            tags=list(set(tags)),
            license=licenses.get(pkg_dict.get('license_id'), 'Other'),
            visibility='PRIVATE' if pkg_dict.get('private') else 'OPEN',
            files=[
                _prepare_resource_url(res)
                for res in pkg_dict['resources']
            ]
        )

        return data

    def _is_dict_changed(self, new_data, old_data):
        for key, value in new_data.items():
            if old_data.get(key) != value:
                return True
        return False

    def _create_request(self, data, id):
        url = self.api_create_put.format(owner=self.owner, id=id)
        res = self._put(url, data)
        if res.status_code == 200:
            log.info('[{0}] Successfuly created'.format(id))
        else:
            log.warn(
                '[{0}] Create package: {1}'.format(id, res.content))

        _delay_request()

        return res

    def _update_request(self, data, id):
        url = self.api_update.format(owner=self.owner, name=id)
        res = self._put(url, data)
        if res.status_code == 200:
            log.info('[{0}] Successfuly updated'.format(id))
        else:
            log.warn(
                '[{0}] Update package: {1}'.format(id, res.content))
        
        _delay_request()
        
        return res

    def _delete_request(self, data, id):
        url = self.api_delete.format(owner=self.owner, id=id)
        res = self._delete(url, data)
        if res.status_code == 200:
            log.info('[{0}] Successfuly deleted'.format(id))
        else:
            log.warn(
                '[{0}] Delete package: {1}'.format(id, res.content))

        _delay_request()

        return res

    def _is_update_required(self, data, id):
        url = self.api_update.format(owner=self.owner, name=id)
        remote_res = self._get(url)
        if remote_res.status_code != 200:
            log.warn(
                '[{0}] Unable to get package for dirty check:{1}'.format(
                    id, remote_res.content))
        else:
            remote_data = remote_res.json()
            if not self._is_dict_changed(data, remote_data):
                return False
        return True

    def _create(self, data, extras, attempt=0):
        res = self._create_request(data, extras.id)
        extras.message = res.content
        if res.status_code == 200:
            resp_json = res.json()
            if 'uri' in resp_json:
                new_id = os.path.basename(resp_json['uri'])
                extras.id = new_id

            extras.state = States.uptodate
        elif res.status_code == 429:
            log.error('[{0}] Create package error (too many connections)'.format(
                extras.id))
            _repeat_request(extras.id, attempt)
        else:
            extras.state = States.failed
            log.error('[{0}] Create package failed: {1}'.format(
                extras.id, res.content))

        return data

    def _update(self, data, extras, attempt=0):
        if not self._is_update_required(data, extras.id):
            return data

        res = self._update_request(data, extras.id)
        extras.message = res.content

        if res.status_code == 200:
            extras.state = States.uptodate
        elif res.status_code == 404:
            log.warn('[{0}] Package not exists. Creating...'.format(
                extras.id))
            res = self._create(data, extras)
        elif res.status_code == 429:
            log.error('[{0}] Update package error (too many connections)'.format(
                extras.id))
            _repeat_request(extras.id, attempt)
        else:
            extras.state = States.failed
            log.error('[{0}] Update package error:{1}'.format(
                extras.id, res.content))
        return data

    def _delete_dataset(self, data, extras, attempt=0):
        res = self._delete_request(data, extras.id)
        extras.message = res.content
        if res.status_code in (200, 404):
            query = model.Session.query(Extras).filter(Extras.id == extras.id)
            query.delete()
            log.info('[{0}] deleted from datadotworld_extras table'.format(
                extras.id))
        elif res.status_code == 429:
            log.error('[{0}] Delete package error (too many connections)'.format(
                extras.id))
            _repeat_request(extras.id, attempt)
        else:
            extras.state = States.failed
            log.error('[{0}] Delete package error:{1}'.format(
                extras.id, res.content))
        return data

    def sync(self, pkg_dict, attempt=0):
        entity = model.Package.get(pkg_dict['id'])
        pkg_dict = get_action('package_show')(get_context(), {'id': entity.id})
        data_dict = self._format_data(pkg_dict)

        extras = entity.datadotworld_extras
        pkg_state = pkg_dict.get('state')
        if pkg_state == 'deleted':
            action = self._delete_dataset
        else:
            action = self._update if extras and extras.id else self._create
        if not extras:
            extras = Extras(
                package=entity, owner=self.owner,
                id=data_dict['title'])
            model.Session.add(extras)
            extras.state = States.pending

        try:
            model.Session.commit()
        except Exception as e:
            model.Session.rollback()
            log.error('[sync problem] {0}'.format(e))

        action(data_dict, extras)
        model.Session.commit()

    def sync_resources(self, id):
        url = self.api_res_sync.format(
            owner=self.owner,
            name=id
        )
        resp = self._get(url)
        msg = '{0} - {1:20} - {2}'.format(
            resp.status_code, id, resp.content
        )
        log.info(msg)

    def check_credentials(self):
        url = self.api_update.format(
            owner=self.owner,
            name='definitely-fake-dataset-name'
        )
        resp = self._get(url)

        if resp.status_code == 401:
            return False
        return True

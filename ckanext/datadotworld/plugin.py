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

import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
from ckanext.datadotworld.model.credentials import Credentials
import ckan.model as model
import logging
import ckanext.datadotworld.tasks as tasks
import ckanext.datadotworld.api as api
import ckanext.datadotworld.helpers as dh
import os
from pylons import config


log = logging.getLogger(__name__)


class DatadotworldPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IRoutes, inherit=True)
    plugins.implements(plugins.IPackageController, inherit=True)
    plugins.implements(plugins.ITemplateHelpers)

    # ITemplateHelpers

    def get_helpers(self):
        return {
            'datadotworld_link': api.API.generate_link,
            'datadotworld_creds': api.API.creds_from_id,
            'datadotworld_admin_in_orgs': dh.admin_in_orgs
        }

    # IConfigurer

    def update_config(self, config_):
        toolkit.add_template_directory(config_, 'templates')
        toolkit.add_public_directory(config_, 'public')
        toolkit.add_resource('fanstatic', 'datadotworld')

    # IRoutes

    def before_map(self, map):
        map.connect(
            'organization_dataworld',
            '/organization/edit/{id}/data.world',
            controller='ckanext.datadotworld.controller:DataDotWorldController',
            action='edit')
        map.connect(
            'list_dataworld_sync',
            '/data.world/{state:failed|pending|up-to-date|deleted}',
            controller='ckanext.datadotworld.controller:DataDotWorldController',
            action='list_sync')
        map.connect(
            'list_dataworld_sync_for_org',
            '/data.world/{org_id}/{state:failed|pending|up-to-date|deleted}',
            controller='ckanext.datadotworld.controller:DataDotWorldController',
            action='list_sync')


        return map

    # IPackageController

    def after_create(self, context, data_dict):
        ckan_ini_filepath = os.path.abspath(config['__file__'])
        api.compat_enqueue(
            'datadotworld.syncronize',
            tasks.syncronize,
            args=[data_dict['id'], ckan_ini_filepath])
        return data_dict

    def after_update(self, context, data_dict):
        ckan_ini_filepath = os.path.abspath(config['__file__'])
        api.compat_enqueue(
            'datadotworld.syncronize',
            tasks.syncronize,
            args=[data_dict['id'], ckan_ini_filepath])
        return data_dict

    def after_delete(self, context, data_dict):
        ckan_ini_filepath = os.path.abspath(config['__file__'])
        api.compat_enqueue(
            'datadotworld.syncronize',
            tasks.syncronize,
            args=[data_dict['id'], ckan_ini_filepath])
        return data_dict

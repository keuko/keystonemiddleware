# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import ast
import collections
import re

from pycadf import cadftaxonomy as taxonomy
from pycadf import endpoint
from pycadf import resource
import six
from six.moves import configparser
from six.moves.urllib import parse as urlparse

from keystonemiddleware.i18n import _LW


# NOTE(blk-u): Compatibility for Python 2. SafeConfigParser and
# SafeConfigParser.readfp are deprecated in Python 3. Remove this when we drop
# support for Python 2.
if six.PY2:
    class _ConfigParser(configparser.SafeConfigParser):
        read_file = configparser.SafeConfigParser.readfp
else:
    _ConfigParser = configparser.ConfigParser


Service = collections.namedtuple('Service',
                                 ['id', 'name', 'type', 'admin_endp',
                                  'public_endp', 'private_endp'])


AuditMap = collections.namedtuple('AuditMap',
                                  ['path_kw',
                                   'custom_actions',
                                   'service_endpoints',
                                   'default_target_endpoint_type'])


class PycadfAuditApiConfigError(Exception):
    """Error raised when pyCADF fails to configure correctly."""

    pass


class OpenStackAuditApi(object):

    def __init__(self, cfg_file, log):
        """Configure to recognize and map known api paths."""
        path_kw = {}
        custom_actions = {}
        endpoints = {}
        default_target_endpoint_type = None

        if cfg_file:
            try:
                map_conf = _ConfigParser()
                map_conf.read_file(open(cfg_file))

                try:
                    default_target_endpoint_type = map_conf.get(
                        'DEFAULT', 'target_endpoint_type')
                except configparser.NoOptionError:  # nosec
                    # Ignore the undefined config option,
                    # default_target_endpoint_type remains None which is valid.
                    pass

                try:
                    custom_actions = dict(map_conf.items('custom_actions'))
                except configparser.Error:  # nosec
                    # custom_actions remains {} which is valid.
                    pass

                try:
                    path_kw = dict(map_conf.items('path_keywords'))
                except configparser.Error:  # nosec
                    # path_kw remains {} which is valid.
                    pass

                try:
                    endpoints = dict(map_conf.items('service_endpoints'))
                except configparser.Error:  # nosec
                    # endpoints remains {} which is valid.
                    pass
            except configparser.ParsingError as err:
                raise PycadfAuditApiConfigError(
                    'Error parsing audit map file: %s' % err)

        self._log = log
        self._MAP = AuditMap(
            path_kw=path_kw, custom_actions=custom_actions,
            service_endpoints=endpoints,
            default_target_endpoint_type=default_target_endpoint_type)

    @staticmethod
    def _clean_path(value):
        """Clean path if path has json suffix."""
        return value[:-5] if value.endswith('.json') else value

    def get_action(self, req):
        """Take a given Request, parse url path to calculate action type.

        Depending on req.method:

        if POST:

        - path ends with 'action', read the body and use as action;
        - path ends with known custom_action, take action from config;
        - request ends with known path, assume is create action;
        - request ends with unknown path, assume is update action.

        if GET:

        - request ends with known path, assume is list action;
        - request ends with unknown path, assume is read action.

        if PUT, assume update action.
        if DELETE, assume delete action.
        if HEAD, assume read action.

        """
        path = req.path[:-1] if req.path.endswith('/') else req.path
        url_ending = self._clean_path(path[path.rfind('/') + 1:])
        method = req.method

        if url_ending + '/' + method.lower() in self._MAP.custom_actions:
            action = self._MAP.custom_actions[url_ending + '/' +
                                              method.lower()]
        elif url_ending in self._MAP.custom_actions:
            action = self._MAP.custom_actions[url_ending]
        elif method == 'POST':
            if url_ending == 'action':
                try:
                    if req.json:
                        body_action = list(req.json.keys())[0]
                        action = taxonomy.ACTION_UPDATE + '/' + body_action
                    else:
                        action = taxonomy.ACTION_CREATE
                except ValueError:
                    action = taxonomy.ACTION_CREATE
            elif url_ending not in self._MAP.path_kw:
                action = taxonomy.ACTION_UPDATE
            else:
                action = taxonomy.ACTION_CREATE
        elif method == 'GET':
            if url_ending in self._MAP.path_kw:
                action = taxonomy.ACTION_LIST
            else:
                action = taxonomy.ACTION_READ
        elif method == 'PUT' or method == 'PATCH':
            action = taxonomy.ACTION_UPDATE
        elif method == 'DELETE':
            action = taxonomy.ACTION_DELETE
        elif method == 'HEAD':
            action = taxonomy.ACTION_READ
        else:
            action = taxonomy.UNKNOWN

        return action

    def _get_service_info(self, endp):
        service = Service(
            type=self._MAP.service_endpoints.get(
                endp['type'],
                taxonomy.UNKNOWN),
            name=endp['name'],
            id=endp['endpoints'][0].get('id', endp['name']),
            admin_endp=endpoint.Endpoint(
                name='admin',
                url=endp['endpoints'][0].get('adminURL', taxonomy.UNKNOWN)),
            private_endp=endpoint.Endpoint(
                name='private',
                url=endp['endpoints'][0].get('internalURL', taxonomy.UNKNOWN)),
            public_endp=endpoint.Endpoint(
                name='public',
                url=endp['endpoints'][0].get('publicURL', taxonomy.UNKNOWN)))

        return service

    def _build_typeURI(self, req, service_type):
        """Build typeURI of target.

        Combines service type and corresponding path for greater detail.
        """
        type_uri = ''
        prev_key = None
        for key in re.split('/', req.path):
            key = self._clean_path(key)
            if key in self._MAP.path_kw:
                type_uri += '/' + key
            elif prev_key in self._MAP.path_kw:
                type_uri += '/' + self._MAP.path_kw[prev_key]
            prev_key = key
        return service_type + type_uri

    def _build_target(self, req, service):
        """Build target resource."""
        target_typeURI = (
            self._build_typeURI(req, service.type)
            if service.type != taxonomy.UNKNOWN else service.type)
        target = resource.Resource(typeURI=target_typeURI,
                                   id=service.id, name=service.name)
        if service.admin_endp:
            target.add_address(service.admin_endp)
        if service.private_endp:
            target.add_address(service.private_endp)
        if service.public_endp:
            target.add_address(service.public_endp)
        return target

    def get_target_resource(self, req):
        """Retrieve target information.

        If discovery is enabled, target will attempt to retrieve information
        from service catalog. If not, the information will be taken from
        given config file.
        """
        service_info = Service(type=taxonomy.UNKNOWN, name=taxonomy.UNKNOWN,
                               id=taxonomy.UNKNOWN, admin_endp=None,
                               private_endp=None, public_endp=None)

        catalog = {}
        try:
            catalog = ast.literal_eval(
                req.environ['HTTP_X_SERVICE_CATALOG'])
        except KeyError:
            msg = _LW('Unable to discover target information because '
                      'service catalog is missing. Either the incoming '
                      'request does not contain an auth token or auth '
                      'token does not contain a service catalog. For '
                      'the latter, please make sure the '
                      '"include_service_catalog" property in '
                      'auth_token middleware is set to "True"')
            self._log.warning(msg)

        default_endpoint = None
        for endp in catalog:
            endpoint_urls = endp['endpoints'][0]
            admin_urlparse = urlparse.urlparse(
                endpoint_urls.get('adminURL', ''))
            public_urlparse = urlparse.urlparse(
                endpoint_urls.get('publicURL', ''))
            req_url = urlparse.urlparse(req.host_url)
            if (req_url.netloc == admin_urlparse.netloc
                    or req_url.netloc == public_urlparse.netloc):
                service_info = self._get_service_info(endp)
                break
            elif (self._MAP.default_target_endpoint_type and
                  endp['type'] == self._MAP.default_target_endpoint_type):
                default_endpoint = endp
        else:
            if default_endpoint:
                service_info = self._get_service_info(default_endpoint)
        return self._build_target(req, service_info)
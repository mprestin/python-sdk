# Copyright 2012-2014 Ravello Systems, Inc.
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

from __future__ import absolute_import, print_function

import sys
import base64
import socket
import logging
import time
import json
import random
import requests

# Python 2.x / 3.x module name differences
try:
    from urllib import parse as urlparse
    from http.cookies import SimpleCookie
except ImportError:
    import urlparse
    from Cookie import SimpleCookie

pyver = sys.version_info[:2]
if pyver not in [(2, 6), (2, 7)] and pyver < (3, 3):
    raise ImportError('Python 2.6, 2.7 or 3.3+ is required')


__all__ = ['random_luid', 'update_luids', 'application_state', 'new_name',
           'RavelloError', 'RavelloClient']

http_methods = {'POST': requests.post, 'GET': requests.get, 'PUT': requests.put, 'DELETE': requests.delete}
DEFAULT_HTTPS_PORT = 443
DEFAULT_HTTP_PORT = 80

def random_luid():
    """Return a new random local ID."""
    return random.randint(0, 1 << 63)


def update_luids(obj):
    """Update the locally unique IDs in *obj*.

    The object must be a dict, or a list of dicts.

    This replaces all "id" keys (directly or indirectly) below *obj* with an
    new random ID generated by :func:`random_luid`. This function is useful
    when adding VMs images to a new or existing application. Every entity in
    an application's design must have a unique local ID. When you're adding
    multiple VMs based on the same image, the IDs are copied and you need to
    use this function to ensure the VMs have unique local IDs again.
    """
    if isinstance(obj, list):
        return [update_luids(elem) for elem in obj]
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if key == 'id':
                obj['id'] = random_luid()
            elif isinstance(value, (dict, list)):
                update_luids(value)
    else:
        return obj


def application_state(app):
    """Return the consolidated state for application *app*.

    The *app* parameter must be a dict as returned by
    :meth:`~RavelloClient.get_application`.

    The consolidated state for an application is the set of distinct states
    for its VMs. As special cases, None is returned if there are no VMs, and
    the single state is returned if there is exactly one state.
    """
    states = list(set((vm['state'] for vm in app.get('deployment', {}).get('vms', []))))
    return states if len(states) > 1 else states[0] if len(states) == 1 else None


def new_name(existing, prefix):
    """Return a name that is not in *existing*.

    The *existing* parameter must be a sequence of strings, or dicts with a
    "name" key. It the latter case, it is typically a list returned by one of
    the "get all" functions like :meth:`RavelloClient.get_applications` or
    :meth:~RavelloClient.get_blueprints`.

    The unique name is generated by appending a number to *prefix*.
    """
    names = set()
    for name in existing:
        if isinstance(name, dict):
            names.add(name['name'])
        else:
            names.add(name)
    for i in range(len(names)+1):
        name = '{0}{1}'.format(prefix, i)
        if name not in names:
            break
    return name


def urlsplit2(url, default_scheme='http'):
    """Like :func:`urllib.parse.urlsplit`, but fills in default values for
    *scheme* (based on *default_scheme*), *port* (depending on scheme), and
    *path* (defaults to "/").
    """
    if '://' not in url:
        url = '{0}://{1}'.format(default_scheme, url)
    result = urlparse.urlsplit(url)
    updates = {}
    if result.port is None:
        port = DEFAULT_HTTPS_PORT if result.scheme == 'https' else DEFAULT_HTTP_PORT
        updates['netloc'] = '{0}:{1}'.format(result.hostname, port)
    if not result.path:
        updates['path'] = '/'
    if updates:
        result = result._replace(**updates)
    return result


def _idempotent(method):
    """Return whether *method* is idempotent."""
    return method in ('GET', 'HEAD', 'PUT')


def _match_filter(obj, flt):
    """Match the object *obj* with filter *flt*."""
    if callable(flt):
        return flt(obj)
    elif not isinstance(flt, dict):
        raise TypeError('expecting a callable or a dict')
    if isinstance(obj, list):
        return [ob for ob in obj if _match_filter(ob, flt)]
    for fkey, fval in flt.items():
        obval = obj.get(fkey)
        if obval is None:
            return False
        elif isinstance(fval, dict):
            if not isinstance(obval, dict) or not _match_filter(obval, fval):
                return False
        elif callable(fval):
            return fval(obval)
        elif fval != obval:
            return False
    return True


class RavelloError(Exception):
    """Exception used by :class:`RavelloClient`."""


class RavelloClient(object):
    """A client for the Ravello API.

    The client is a thin wrapper around the Ravello RESTful API. The client
    manages a single HTTPS connection, and implements login, redirect and retry
    functionality. A single generic :meth:`request` method is provided to issue
    API requests.

    On top of this, most existing RESTful API calls are mapped as methods on
    this class. These mapped methods are simple wrappers around the generic
    :meth:`request` method. Some general comments on this mapping:

    * The calls are named "<method>_<resource>", for example
      ":meth:`create_keypair`" and ":meth:`get_blueprints`". A method is always
      an English verb, while a resource is can be a singular or plural Englush
      noun.
    * The standard methods are "get", "create", "update" and "delete". Not all
      methods are defined for all resources, and some resources have additional
      methods.
    * The available resources are "application", "blueprint", "image",
      "keypair" and "vm". The plural versions of these exist as well.
    * There is no client-side object model. The return value from any API call
      is simply the parsed JSON response.
    * Resources are returned as a dict or a list of dicts. A dict always
      represents a single object, and its key/value pairs correspond to the
      object's attributes. Lists always represents multiple objects.
    * Objects are identifed by a numeric ID, which is always the key "id".
    * All methods that accept an object ID either accept this parameter
      as a simple Python int, or alternatively as a dict with an "id" key
      containing the ID. In the latter case, the dict is typically returned
      previsouly by another API call.
    * HTTP response codes in the 4xx or 5xx range are considered errors, and
      are turned into :class:`RavelloError` exceptions (except for 404 which
      results in a response of ``None``).
    """

    default_url = 'https://cloud.ravellosystems.com/api/v1'
    default_timeout = 60
    default_retries = 3
    default_redirects = 3

    def __init__(self, username=None, password=None, url=None, timeout=None, retries=None, proxy_url=None):
        """Create a new client.

        The *username* and *password* parameters specify the credentials to use
        when connecting to the API. The *timeout* and *retries* parameters
        specify the default network system call time timeout and maximum number
        of retries respectively.
        """
        self._username = username
        self._password = password
        self.timeout = timeout if timeout is not None else self.default_timeout
        self.retries = retries if retries is not None else self.default_retries
        self.redirects = self.default_redirects
        self._logger = logging.getLogger('ravello')
        self._autologin = True
        self._cookies = None
        self._user_info = None
        self._set_url(url or self.default_url)
        self._proxies = {}
        if proxy_url is not None:
            self._proxies = {"http": proxy_url, "https": proxy_url}

    @property
    def url(self):
        """The parsed URL of the API endpoint, which is a
        :class:`urllib.parse.SplitResult` instance."""
        return self._url

    @property
    def connected(self):
        """Whether or not the client is connected to the API."""
        return self._cookies is not None

    @property
    def have_credentials(self):
        """Whether or not credentials are available."""
        return self._username is not None and self._password is not None

    @property
    def logged_in(self):
        """Whether or not the client is logged in."""
        return self._cookies is not None

    @property
    def user_info(self):
        """Return information about the current logged-in user."""
        return self._user_info

    def _set_url(self, url):
        if self.connected:
            raise RuntimeError('cannot change URL when connected')
        self._url = urlsplit2(url)

    def connect(self, url=None, proxy_url=None):
        """Connect to the API.

        It is not mandatory to call this method. If this method is not called,
        the client will automatically connect when required.
        """
        if url is not None:
            self._set_url(url)
        if proxy_url is not None:
            self._proxies = {"http": proxy_url, "https": proxy_url}

    def login(self, username=None, password=None):
        """Login to the API.

        This method performs a login to the API, and store the resulting
        authentication cookie in memory.

        It is not mandatory to call this method. If this method is not called,
        the client will automatically login when required.
        """
        if self.logged_in:
            raise RuntimeError('already logged in')
        if username is not None:
            self._username = username
        if password is not None:
            self._password = password
        self._login()

    def _login(self):
        if not self.have_credentials:
            raise RuntimeError('no credentials set')
        self._logger.debug('performing a username/password login')
        self._autologin = False
        auth = '{0}:{1}'.format(self._username, self._password)
        auth = base64.b64encode(auth.encode('ascii')).decode('ascii')
        headers = [('Authorization', 'Basic {0}'.format(auth))]
        response = self._request('POST', '/login', b'', headers)
        self._cookies = SimpleCookie()
        self._cookies.load(response.headers.get('Set-Cookie'))
        self._autologin = True
        self._user_info = response.entity

    def logout(self):
        """Logout from the API. This invalidates the authentication cookie."""
        if not self.logged_in:
            return
        self.request('POST', '/logout')
        self._cookies = None

    def close(self):
        """Close the connection to the API."""
        if not self.connected:
            return
        self._cookies = None

    # The request() method is the main function. All other methods are a small
    # shim on top of this.

    def request(self, method, path, entity=None, headers=None):
        """Issues a request to the API.

        The parsed entity is returned, or a :class:`RavelloError` exception is
        raised on error.

        This method can be used in case a certain API call has not yet been
        added as a method.
        """
        body = json.dumps(entity).encode('utf8') if entity is not None else b''
        headers = headers if headers is not None else []
        response = self._request(method, path, body, headers)
        return response.entity

    def _request(self, method, path, body=b'', headers=None):
        rpath = self._url.path + path
        abpath = self.default_url + path
        hdict = {'Accept': 'application/json'}
        for key, value in headers:
            hdict[key] = value
        if body:
            hdict['Content-Type'] = 'application/json'
        retries = redirects = 0
        while retries < self.retries and redirects < self.redirects:
            if not self.logged_in and self.have_credentials and self._autologin:
                self._login()
            if self._cookies:
                cookies = ['{0}={1}'.format(c.key, c.coded_value) for c in self._cookies.values()]
                hdict['Cookie'] = '; '.join(cookies)
            try:
                self._logger.debug('request: {0} {1}'.format(method, rpath))
                response = http_methods[method](abpath, data=body, headers=hdict, proxies=self._proxies, timeout=self.timeout)
                status = response.status_code
                ctype = response.headers.get('Content-Type')
                if ctype == 'application/json':
                    entity = response.json()
                else:
                    entity = None
                self._logger.debug('response: {0} ({1})'.format(status, ctype))
                if 200 <= status < 299:
                    if isinstance(entity, dict) and entity.get('id'):
                        if response.headers.get('Content-Location'):
                            href = urlsplit2(response.headers.get('Content-Location')).path
                        elif response.headers.get('Location'):
                            href = urlsplit2(response.headers.get('Location')).path
                        elif method == 'POST':
                            # missing Location header e.g. with /pubkeys
                            href = '{0}/{1}'.format(abpath, entity['id'])
                        else:
                            href = abpath
                        entity['_href'] = href[len(self._url.path):]
                    elif isinstance(entity, list):
                        for elem in entity:
                            if 'id' in elem:
                                elem['_href'] = '{0}/{1}'.format(path, elem['id'])
                elif 300 <= status < 399:
                    loc = response.headers.get('Location')
                    if loc is None:
                        raise RavelloError('no location for {0} response'.format(status))
                    if loc.startswith('/'):
                        rpath = loc
                    else:
                        url = urlsplit2(loc)
                        if url.netloc != self._url.netloc:
                            raise RavelloError('will not chase referral to {0}'.format(loc))
                        rpath = url.path
                    redirects += 1
                elif status == 404:
                    entity = None
                else:
                    code = response.headers.get('ERROR-CODE', 'unknown')
                    msg = response.headers.get('ERROR-MESSAGE', 'unknown')
                    raise RavelloError('got status {0} ({1}/{2})' .format(status, code, msg))
                response.entity = entity
            except (socket.timeout, ValueError) as e:
                self._logger.debug('error: {0!s}'.format(e))
                self.close()
                if not _idempotent(method):
                    self._logger.debug('not retrying {0} request'.format(method))
                    raise RavelloError('request timeout')
                retries += 1
                continue
            break
        if retries == self.retries:
            raise RavelloError('maximum number of retries reached')
        if redirects == self.redirects:
            raise RavelloError('maximum number of redirects reached')
        return response

    def reload(self, obj):
        """Reload the object *obj*.

        The object must have been returned by the API, and must be a dict with
        an ``"_href"`` key.
        """
        href = obj.get('_href')
        if href is None:
            raise RuntimeError('obj must have an "_href" key')
        return self.request('GET', href)

    def wait_for(self, obj, cond, timeout=None):
        """Wait for a condition on *obj* to become true.

        The object *obj* must be reloadable. See :meth:`reload` for more
        details.

        The condition *cond* must be a dict or a callable. If it is a dict, it
        lists the keys and values that the object must have. If it is a
        callable, it will be called with the object as an argument, and it
        should return True or False.

        The *timeout* argument specifies the total time to wait. If not
        specified, it will default to the system call timeout passed to the
        constructor.

        If the condition does not become true before the timeout, a
        :class:`RavelloError` exception is raised.
        """
        end_time = time.time() + timeout
        while end_time > time.time():
            obj = self.reload(obj)
            if _match_filter(obj, cond):
                break
            time.sleep(5)
        if end_time < time.time():
            raise RavelloError('timeout waiting for condition')

    # Mapped API calls below

    def get_application(self, app):
        """Return the application with ID *app*, or None if it does not
        exist."""
        if isinstance(app, dict): app = app['id']
        return self.request('GET', '/applications/{0}'.format(app))

    def get_applications(self, filter=None):
        """Return a list with all applications.

        The *filter* argument can be used to return only a subset of the
        applications. See the description of the *cond* argument to
        :meth:`wait_for`.
        """
        apps = self.request('GET', '/applications')
        if filter is not None:
            apps = _match_filter(apps, filter)
        return apps

    def create_application(self, app):
        """Create a new application.

        The *app* parameter must be a dict describing the application to
        create.

        The new application is returned.
        """
        return self.request('POST', '/applications', app)

    def update_application(self, app):
        """Update an existing application.

        The *app* parameter must be the updated application. The way to update
        an application (or any other resource) is to first retrieve it, make
        the updates client-side, and then use this method to make the update.

        The updated application is returned.
        """
        return self.request('PUT', '/applications/{0}'.format(app['id']), app)

    def delete_application(self, app):
        """Delete an application with ID *app*."""
        if isinstance(app, dict): app = app['id']
        self.request('DELETE', '/applications/{0}'.format(app))

    def publish_application(self, app, req=None):
        """Publish the application with ID *app*.

        The *req* parameter, if provided, must be a dict with publish
        parameters.
        """
        if isinstance(app, dict):
            app = app['id']
        self.request('POST', '/applications/{0}/publish'.format(app), req)

    def start_application(self, app, req=None):
        """Start the application with ID *app*.

        The *req* parameter, if provided, must be a dict with start
        parameters.
        """
        if isinstance(app, dict): app = app['id']
        self.request('POST', '/applications/{0}/start'.format(app), req)

    def stop_application(self, app, req=None):
        """Stop the application with ID *app*.

        The *req* parameter, if provided, must be a dict with stop
        parameters.
        """
        if isinstance(app, dict): app = app['id']
        self.request('POST', '/applications/{0}/stop'.format(app), req)

    def restart_application(self, app, req=None):
        """Restart the application with ID *app*.

        The *req* parameter, if provided, must be a dict with restart
        parameters.
        """
        if isinstance(app, dict): app = app['id']
        self.request('POST', '/applications/{0}/restart'.format(app), req)

    def publish_application_updates(self, app, autostart=True):
        """Publish updates for the application with ID *app*."""
        if isinstance(app, dict): app = app['id']
        url = '/applications/{0}/publishUpdates'.format(app)
        if not autostart:
            url += '?startAllDraftVms=false'
        self.request('POST', url)

    def set_application_expiration(self, app, req):
        """Set the expiration for the application with ID *app*.

        The *req* parameter must be a dict describing the new expiration.
        """
        if isinstance(app, dict): app = app['id']
        self.request('POST', '/applications/{0}/setExpiration'.format(app), req)

    def get_application_publish_locations(self, app, req=None):
        """Get a list of locations where *app* can be published."""
        if isinstance(app, dict): app = app['id']
        url = '/applications/{0}/findPublishLocations'.format(app)
        return self.request('POST', url, req)

    def get_blueprint_publish_locations(self, bp, req=None):
        """Get a list of locations where *bp* can be published."""
        if isinstance(bp, dict): bp = bp['id']
        url = '/blueprints/{0}/findPublishLocations'.format(bp)
        return self.request('POST', url, req)

    def get_vm(self, app, vm):
        """Return the vm with ID *vm* in the appplication with ID *app*,
        or None if it does not exist.
        """
        if isinstance(app, dict): app = app['id']
        if isinstance(vm, dict): vm = vm['id']
        return self.request('GET', '/applications/{0}/vms/{1}'.format(app, vm))

    def get_vms(self, app, filter=None):
        """Return a list with all vms (for a given app).

        The *filter* argument can be used to return only a subset of the
        applications. See the description of the *cond* argument to
        :meth:`wait_for`.
        """
        if isinstance(app, dict): app = app['id']
        apps = self.request('GET', '/applications/{0}/vms'.format(app))
        if filter is not None:
            apps = _match_filter(apps, filter)
        return apps

    def start_vm(self, app, vm):
        """Start the VM with ID *vm* in the application with ID *app*."""
        if isinstance(app, dict): app = app['id']
        if isinstance(vm, dict): vm = vm['id']
        self.request('POST', '/applications/{0}/vms/{1}/start'.format(app, vm))

    def stop_vm(self, app, vm):
        """Stop the VM with ID *vm* in the application with ID *app*."""
        if isinstance(app, dict): app = app['id']
        if isinstance(vm, dict): vm = vm['id']
        self.request('POST', '/applications/{0}/vms/{1}/stop'.format(app, vm))

    def poweroff_vm(self, app, vm):
        """Power off the VM with ID *vm* in the application with ID *app*."""
        if isinstance(app, dict): app = app['id']
        if isinstance(vm, dict): vm = vm['id']
        self.request('POST', '/applications/{0}/vms/{1}/poweroff'.format(app, vm))

    def restart_vm(self, app, vm):
        """Restart the VM with ID *vm* in the application with ID *app*."""
        if isinstance(app, dict): app = app['id']
        if isinstance(vm, dict): vm = vm['id']
        self.request('POST', '/applications/{0}/vms/{1}/restart'.format(app, vm))

    def redeploy_vm(self, app, vm):
        """Redeploy the VM with ID *vm* in the application with ID *app*."""
        if isinstance(app, dict): app = app['id']
        if isinstance(vm, dict): vm = vm['id']
        self.request('POST', '/applications/{0}/vms/{1}/redeploy'.format(app, vm))

    def get_vnc_url(self, app, vm):
        """Get the VNC URL for the VM with ID *vm* in the application with ID *app*."""
        if isinstance(app, dict): app = app['id']
        if isinstance(vm, dict): vm = vm['id']
        headers = [('Accept', 'text/plain')]
        url = self.request('GET', '/applications/{0}/vms/{1}/vncUrl'.format(app, vm),
                           headers=headers)
        return url.decode('iso-8859-1')

    def get_blueprint(self, bp):
        """Return the blueprint with ID *bp*, or None if it does not exist."""
        if isinstance(bp, dict): bp = bp['id']
        return self.request('GET', '/blueprints/{0}'.format(bp))

    def get_blueprints(self, filter=None):
        """Return a list with all blueprints.

        The *filter* argument can be used to return only a subset of the
        applications. See the description of the *cond* argument to
        :meth:`wait_for`.
        """
        bps = self.request('GET', '/blueprints')
        if filter is not None:
            bps = _match_filter(bps, filter)
        return bps

    def create_blueprint(self, bp):
        """Create a new blueprint.

        The *bp* parameter must be a dict describing the blueprint to
        create.

        The new blueprint is returned.
        """
        return self.request('POST', '/blueprints', bp)

    def delete_blueprint(self, bp):
        """Delete the blueprint with ID *bp*."""
        if isinstance(bp, dict): bp = bp['id']
        self.request('DELETE', '/blueprints/{0}'.format(bp))

    def get_image(self, img):
        """Return the image with ID *img*, or None if it does not exist."""
        if isinstance(img, dict): img = img['id']
        return self.request('GET', '/images/{0}'.format(img))

    def get_images(self, filter=None):
        """Return a list with all images.

        The *filter* argument can be used to return only a subset of the
        images. See the description of the *cond* argument to
        :meth:`wait_for`.
        """
        imgs = self.request('GET', '/images')
        if filter is not None:
            imgs = _match_filter(imgs, filter)
        return imgs

    def update_image(self, img):
        """Update an existing image.

        The *img* parameter must be the updated image.  The updated image is
        returned.
        """
        return self.request('PUT', '/images/{0}'.format(img['id']), img)

    def delete_image(self, img):
        """Delete the image with ID *img*."""
        if isinstance(img, dict): img = img['id']
        self.request('DELETE', '/images/{0}'.format(img))

    def get_diskimage(self, img):
        """Return the disk image with ID *img*, or None if it does not exist."""
        if isinstance(img, dict): img = img['id']
        return self.request('GET', '/diskImages/{0}'.format(img))

    def get_diskimages(self, filter=None):
        """Return a list with all disk images.

        The *filter* argument can be used to return only a subset of the
        disk images. See the description of the *cond* argument to
        :meth:`wait_for`.
        """
        imgs = self.request('GET', '/diskImages')
        if filter is not None:
            imgs = _match_filter(imgs, filter)
        return imgs

    def update_diskimage(self, img):
        """Update an existing image.

        The *img* parameter must be the updated image.  The updated disk image
        is returned.
        """
        return self.request('PUT', '/diskImages/{0}'.format(img['id']), img)

    def delete_diskimage(self, img):
        """Delete the image with ID *img*."""
        if isinstance(img, dict): img = img['id']
        self.request('DELETE', '/diskImages/{0}'.format(img))

    def get_keypair(self, kp):
        """Return the keypair with ID *kp*, or None if it does not exist."""
        if isinstance(kp, dict): kp = kp['id']
        return self.request('GET', '/keypairs/{0}'.format(kp))

    def get_keypairs(self, filter=None):
        """Return a list with all keypairs.

        The *filter* argument can be used to return only a subset of the
        keypairs.  See the description of the *cond* argument to
        :meth:`wait_for`.
        """
        kps = self.request('GET', '/keypairs')
        if filter is not None:
            kps = _match_filter(kps, filter)
        return kps

    def create_keypair(self, kp):
        """Create a new keypair.

        The *kp* parameter must be a dict describing the keypair to create.

        The new blueprint is returned.
        """
        return self.request('POST', '/keypairs', kp)

    def update_keypair(self, kp):
        """Update an existing keypair.

        The *kp* parameter must be the updated keypair. The updated keypair is
        returned.
        """
        return self.request('PUT', '/keypairs/{0}'.format(kp['id']), kp)

    def delete_keypair(self, kp):
        """Delete the keypair with ID *kp*."""
        if isinstance(kp, dict): kp = kp['id']
        self.request('DELETE', '/keypairs/{0}'.format(kp))

    def generate_keypair(self):
        """Generate a new keypair and return it."""
        return self.request('POST', '/keypairs/generate')

    def get_user(self, user):
        """Return the user with ID *user*, or None if it does not exist."""
        if isinstance(user, dict): user = user['id']
        return self.request('GET', '/users/{0}'.format(user))

    def get_users(self, filter=None):
        """Return a list with all users.

        The *filter* argument can be used to return only a subset of the
        users. See the description of the *cond* argument to :meth:`wait_for`.
        """
        users = self.request('GET', '/users')
        if filter is not None:
            users = _match_filter(users, filter)
        return users

    def create_user(self, user):
        """Invite a new user to organization.

        The *user* parameter must be a dict describing the user to invite.

        The new user is returned.
        """
        return self.request('POST', '/users', user)

    def update_user(self, user, userId):
        """Update an existing user.

        The *user* parameter must be the updated user. The way to update a
        user (or any other resource) is to first retrieve it, make the
        updates client-side, and then use this method to make the update.
        In this case, note however that you can only provide email, name,
        roles, and surname (and email cannot be changed).
        
        The updated user is returned.
        """
        return self.request('PUT', '/users/{0}'.format(userId), user)

    def delete_user(self, user):
        """Delete a user with ID *user*."""
        if isinstance(user, dict): user = user['id']
        self.request('DELETE', '/users/{0}'.format(user))

    def changepw_user(self, passwords, user):
        """Change the password of a user with ID *user*.

        The *passwords* parameter must be a dict describing the existing
        and new passwords.
        """
        return self.request('PUT', '/users/{0}/changepw'.format(user), passwords)

    def get_billing(self, filter=None):
        """Return a list with all applications' charges incurred since
        beginning of the month.

        The *filter* argument can be used to return only a subset of the
        applications. See the description of the *cond* argument to
        :meth:`wait_for`.
        """
        billing = self.request('GET', '/billing')
        if filter is not None:
            billing = _match_filter(billing, filter)
        return billing

    def get_billing_for_month(self, year, month):
        """Return a list with all applications' charges incurred during the
        specified month and year.
        """
        return self.request('GET', '/billing?year={0}&month={1}'.format(year, month))

    def get_events(self):
        """Return a list of all possible event names."""
        return self.request('GET', '/events')

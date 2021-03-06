# -*- coding: utf-8 -*-
"""
OnionShare | https://onionshare.org/

Copyright (C) 2016 Micah Lee <micah@micahflee.com>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from stem.control import Controller
from stem import SocketError
from stem.connection import MissingPassword, UnreadableCookieFile
import os, sys, tempfile, shutil, urllib

from . import socks
from . import helpers, strings

class NoTor(Exception):
    """
    This exception is raised if onionshare can't find a Tor control port
    to connect to, or if it can't find a Tor socks5 proxy to proxy though.
    """
    pass

class TorTooOld(Exception):
    """
    This exception is raised if onionshare needs to use a feature of Tor or stem
    (like stealth ephemeral onion services) but the version you have installed
    is too old.
    """
    pass

class Onion(object):
    """
    Onion is an abstraction layer for connecting to the Tor control port and
    creating onion services. OnionShare supports creating onion services
    using two methods:

    - Modifying the Tor configuration through the control port is the old
      method, and will be deprecated in favor of ephemeral onion services.
    - Using the control port to create ephemeral onion servers is the
      preferred method.

    This class detects the versions of Tor and stem to determine if ephemeral
    onion services are supported. If not, it falls back to modifying the
    Tor configuration.
    """
    def __init__(self, transparent_torification=False, stealth=False):
        self.transparent_torification = transparent_torification
        self.stealth = stealth

        # files and dirs to delete on shutdown
        self.cleanup_filenames = []
        self.service_id = None

        # if the TOR_CONTROL_PORT environment variable is set, use that
        # otherwise, default to Tor Browser, Tor Messenger, and system tor ports
        env_port = os.environ.get('TOR_CONTROL_PORT')
        if env_port:
            ports = [int(env_port)]
        else:
            ports = [9151, 9153, 9051]

        # if the TOR_AUTHENTICATION_PASSWORD is set, use that to authenticate
        password = os.environ.get('TOR_AUTHENTICATION_PASSWORD')

        # connect to the tor controlport
        found_tor = False
        self.c = None
        for port in ports:
            try:
                self.c = Controller.from_port(port=port)
                self.c.authenticate(password)
                found_tor = True
                break
            except SocketError:
                pass
            except MissingPassword:
                raise NoTor(strings._("ctrlport_missing_password").format(str(ports)))
            except UnreadableCookieFile:
                raise NoTor(strings._("ctrlport_unreadable_cookie").format(str(ports)))
        if not found_tor:
            raise NoTor(strings._("cant_connect_ctrlport").format(str(ports)))

        # do the versions of stem and tor that I'm using support ephemeral onion services?
        tor_version = self.c.get_version().version_str
        list_ephemeral_hidden_services = getattr(self.c, "list_ephemeral_hidden_services", None)
        self.supports_ephemeral = callable(list_ephemeral_hidden_services) and tor_version >= '0.2.7.1'

        # do the versions of stem and tor that I'm using support stealth onion services?
        if self.stealth:
            self.check_for_stealth_support()

    def check_for_stealth_support(self):
        try:
            res = self.c.create_ephemeral_hidden_service({1:1}, basic_auth={'onionshare':None}, await_publication=False)
            tmp_service_id = res.content()[0][2].split('=')[1]
            self.c.remove_ephemeral_hidden_service(tmp_service_id)
            self.supports_stealth = True
        except:
            # ephemeral stealth onion services are not supported
            self.supports_stealth = False

    def start(self, port):
        """
        Start a onion service on port 80, pointing to the given port, and
        return the onion hostname.
        """
        self.auth_string = None
        if self.stealth and not self.supports_stealth:
            raise TorTooOld(strings._('error_stealth_not_supported'))

        print(strings._("config_onion_service").format(int(port)))
        if self.supports_ephemeral:
            print(strings._('using_ephemeral'))

            if self.stealth:
                basic_auth = {'onionshare':None}
            else:
                basic_auth = None

            res = self.c.create_ephemeral_hidden_service({ 80: port }, await_publication=True, basic_auth=basic_auth)
            self.service_id = res.content()[0][2].split('=')[1]
            onion_host = self.service_id + '.onion'

            if self.stealth:
                auth_cookie = res.content()[2][2].split('=')[1].split(':')[1]
                self.auth_string = 'HidServAuth {} {}'.format(onion_host, auth_cookie)

            return onion_host

        else:
            # come up with a onion service directory name
            if helpers.get_platform() == 'Windows':
                self.hidserv_dir = tempfile.mkdtemp()
                self.hidserv_dir = self.hidserv_dir.replace('\\', '/')

            else:
                self.hidserv_dir = tempfile.mkdtemp(suffix='onionshare',dir='/tmp')

            self.cleanup_filenames.append(self.hidserv_dir)

            # set up onion service
            hsdic = self.c.get_conf_map('HiddenServiceOptions') or {
                'HiddenServiceDir': [], 'HiddenServicePort': []
            }
            if self.hidserv_dir in hsdic.get('HiddenServiceDir', []):
                # Maybe a stale service with the wrong local port
                dropme = hsdic['HiddenServiceDir'].index(self.hidserv_dir)
                del hsdic['HiddenServiceDir'][dropme]
                del hsdic['HiddenServicePort'][dropme]
            hsdic['HiddenServiceDir'] = hsdic.get('HiddenServiceDir', [])+[self.hidserv_dir]
            hsdic['HiddenServicePort'] = hsdic.get('HiddenServicePort', [])+[
                '80 127.0.0.1:{0:d}'.format(port)]

            self.c.set_options(self._hsdic2list(hsdic))

            # figure out the .onion hostname
            hostname_file = '{0:s}/hostname'.format(self.hidserv_dir)
            onion_host = open(hostname_file, 'r').read().strip()
            return onion_host

    def wait_for_hs(self, onion_host):
        """
        This function is only required when using non-ephemeral onion services. After
        creating a onion service, continually attempt to connect to it until it
        successfully connects.
        """
        # legacy only, this function is no longer required with ephemeral onion services
        print(strings._('wait_for_hs'))

        ready = False
        while not ready:
            try:
                sys.stdout.write('{0:s} '.format(strings._('wait_for_hs_trying')))
                sys.stdout.flush()

                if self.transparent_torification:
                    # no need to set the socks5 proxy
                    urllib.request.urlopen('http://{0:s}'.format(onion_host))
                else:
                    tor_exists = False
                    ports = [9150, 9152, 9050]
                    for port in ports:
                        try:
                            s = socks.socksocket()
                            s.setproxy(socks.PROXY_TYPE_SOCKS5, '127.0.0.1', port)
                            s.connect((onion_host, 80))
                            s.close()
                            tor_exists = True
                            break
                        except socks.ProxyConnectionError:
                            pass
                    if not tor_exists:
                        raise NoTor(strings._("cant_connect_socksport").format(str(ports)))
                ready = True

                sys.stdout.write('{0:s}\n'.format(strings._('wait_for_hs_yup')))
            except socks.GeneralProxyError:
                sys.stdout.write('{0:s}\n'.format(strings._('wait_for_hs_nope')))
                sys.stdout.flush()
            except socks.SOCKS5Error:
                sys.stdout.write('{0:s}\n'.format(strings._('wait_for_hs_nope')))
                sys.stdout.flush()
            except urllib.error.HTTPError:  # torification error
                sys.stdout.write('{0:s}\n'.format(strings._('wait_for_hs_nope')))
                sys.stdout.flush()
            except KeyboardInterrupt:
                return False
        return True

    def cleanup(self):
        """
        Stop onion services that were created earlier, and delete any temporary
        files that were created.
        """
        if self.supports_ephemeral:
            # cleanup the ephemeral onion service
            if self.service_id:
                try:
                    self.c.remove_ephemeral_hidden_service(self.service_id)
                except:
                    pass
                self.service_id = None

        else:
            # cleanup onion service
            try:
                if self.controller:
                    # Get fresh onion services (maybe changed since last time)
                    # and remove ourselves
                    hsdic = self.controller.get_conf_map('HiddenServiceOptions') or {
                        'HiddenServiceDir': [], 'HiddenServicePort': []
                    }
                    if self.hidserv_dir and self.hidserv_dir in hsdic.get('HiddenServiceDir', []):
                        dropme = hsdic['HiddenServiceDir'].index(self.hidserv_dir)
                        del hsdic['HiddenServiceDir'][dropme]
                        del hsdic['HiddenServicePort'][dropme]
                        self.controller.set_options(self._hsdic2list(hsdic))
                    # Politely close the controller
                    self.controller.close()
            except:
                pass

        # cleanup files
        for filename in self.cleanup_filenames:
            if os.path.isfile(filename):
                os.remove(filename)
            elif os.path.isdir(filename):
                shutil.rmtree(filename)
        self.cleanup_filenames = []

    def _hsdic2list(self, dic):
        """
        Convert what we get from get_conf_map to what we need for set_options.

        For example, if input looks like this:
        {
            'HiddenServicePort': [
                '80 127.0.0.1:47906',
                '80 127.0.0.1:33302'
            ],
            'HiddenServiceDir': [
                '/tmp/onionsharelTfZZu',
                '/tmp/onionsharechDai3'
            ]
        }


        Output will look like this:
        [
            ('HiddenServiceDir', '/tmp/onionsharelTfZZu'),
            ('HiddenServicePort', '80 127.0.0.1:47906'),
            ('HiddenServiceDir', '/tmp/onionsharechDai3'),
            ('HiddenServicePort', '80 127.0.0.1:33302')
        ]
        """
        l = []
        for dir, port in zip(dic['HiddenServiceDir'], dic['HiddenServicePort']):
            l.append(('HiddenServiceDir', dir))
            l.append(('HiddenServicePort', port))
        return l

# Copyright (C) 2006-2007  Robey Pointer <robeypointer@gmail.com>
#
# This file is part of paramiko.
#
# Paramiko is free software; you can redistribute it and/or modify it under the
# terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# Paramiko is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Paramiko; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA.

"""
SSH client & key policies
"""

from binascii import hexlify
import getpass
import os
import socket
import warnings

from paramiko.agent import Agent
from paramiko.common import *
from paramiko.config import SSH_PORT
from paramiko.dsskey import DSSKey
from paramiko.hostkeys import HostKeys
from paramiko.resource import ResourceManager
from paramiko.rsakey import RSAKey
from paramiko.ssh_exception import SSHException, BadHostKeyException
from paramiko.transport import Transport
from paramiko.util import retry_on_signal


class SSHClient (object):
    """
    A high-level representation of a session with an SSH server.  This class
    wraps :class:`Transport`, :class:`Channel`, and :class:`SFTPClient` to take care of most
    aspects of authenticating and opening channels.  A typical use case is::

        client = SSHClient()
        client.load_system_host_keys()
        client.connect('ssh.example.com')
        stdin, stdout, stderr = client.exec_command('ls -l')

    You may pass in explicit overrides for authentication and server host key
    checking.  The default mechanism is to try to use local key files or an
    SSH agent (if one is running).

    .. versionadded:: 1.6
    """

    def __init__(self):
        """
        Create a new SSHClient.
        """
        self._system_host_keys = HostKeys()
        self._host_keys = HostKeys()
        self._host_keys_filename = None
        self._log_channel = None
        self._policy = RejectPolicy()
        self._transport = None
        self._agent = None

    def load_system_host_keys(self, filename=None):
        """
        Load host keys from a system (read-only) file.  Host keys read with
        this method will not be saved back by :class:`save_host_keys`.

        This method can be called multiple times.  Each new set of host keys
        will be merged with the existing set (new replacing old if there are
        conflicts).

        If ``filename`` is left as ``None``, an attempt will be made to read
        keys from the user's local "known hosts" file, as used by OpenSSH,
        and no exception will be raised if the file can't be read.  This is
        probably only useful on posix.

        :param filename: the filename to read, or ``None``
        :type filename: str

        :raises IOError: if a filename was provided and the file could not be
            read
        """
        if filename is None:
            # try the user's .ssh key file, and mask exceptions
            filename = os.path.expanduser('~/.ssh/known_hosts')
            try:
                self._system_host_keys.load(filename)
            except IOError:
                pass
            return
        self._system_host_keys.load(filename)

    def load_host_keys(self, filename):
        """
        Load host keys from a local host-key file.  Host keys read with this
        method will be checked after keys loaded via :class:`load_system_host_keys`,
        but will be saved back by :class:`save_host_keys` (so they can be modified).
        The missing host key policy :class:`AutoAddPolicy` adds keys to this set and
        saves them, when connecting to a previously-unknown server.

        This method can be called multiple times.  Each new set of host keys
        will be merged with the existing set (new replacing old if there are
        conflicts).  When automatically saving, the last hostname is used.

        :param filename: the filename to read
        :type filename: str

        :raises IOError: if the filename could not be read
        """
        self._host_keys_filename = filename
        self._host_keys.load(filename)

    def save_host_keys(self, filename):
        """
        Save the host keys back to a file.  Only the host keys loaded with
        :class:`load_host_keys` (plus any added directly) will be saved -- not any
        host keys loaded with :class:`load_system_host_keys`.

        :param filename: the filename to save to
        :type filename: str

        :raises IOError: if the file could not be written
        """

        # update local host keys from file (in case other SSH clients
        # have written to the known_hosts file meanwhile.
        if self._host_keys_filename is not None:
            self.load_host_keys(self._host_keys_filename)

        f = open(filename, 'w')
        for hostname, keys in self._host_keys.iteritems():
            for keytype, key in keys.iteritems():
                f.write('%s %s %s\n' % (hostname, keytype, key.get_base64()))
        f.close()

    def get_host_keys(self):
        """
        Get the local :class:`HostKeys` object.  This can be used to examine the
        local host keys or change them.

        :return: the local host keys
        :rtype: :class:`HostKeys`
        """
        return self._host_keys

    def set_log_channel(self, name):
        """
        Set the channel for logging.  The default is ``"paramiko.transport"``
        but it can be set to anything you want.

        :param name: new channel name for logging
        :type name: str
        """
        self._log_channel = name

    def set_missing_host_key_policy(self, policy):
        """
        Set the policy to use when connecting to a server that doesn't have a
        host key in either the system or local :class:`HostKeys` objects.  The
        default policy is to reject all unknown servers (using :class:`RejectPolicy`).
        You may substitute :class:`AutoAddPolicy` or write your own policy class.

        :param policy: the policy to use when receiving a host key from a
            previously-unknown server
        :type policy: :class:`MissingHostKeyPolicy`
        """
        self._policy = policy

    def connect(self, hostname, port=SSH_PORT, username=None, password=None, pkey=None,
                key_filename=None, timeout=None, allow_agent=True, look_for_keys=True,
                compress=False, sock=None):
        """
        Connect to an SSH server and authenticate to it.  The server's host key
        is checked against the system host keys (see :class:`load_system_host_keys`)
        and any local host keys (:class:`load_host_keys`).  If the server's hostname
        is not found in either set of host keys, the missing host key policy
        is used (see :class:`set_missing_host_key_policy`).  The default policy is
        to reject the key and raise an :class:`SSHException`.

        Authentication is attempted in the following order of priority:

            - The ``pkey`` or ``key_filename`` passed in (if any)
            - Any key we can find through an SSH agent
            - Any "id_rsa" or "id_dsa" key discoverable in ``~/.ssh/``
            - Plain username/password auth, if a password was given

        If a private key requires a password to unlock it, and a password is
        passed in, that password will be used to attempt to unlock the key.

        :param hostname: the server to connect to
        :type hostname: str
        :param port: the server port to connect to
        :type port: int
        :param username: the username to authenticate as (defaults to the
            current local username)
        :type username: str
        :param password: a password to use for authentication or for unlocking
            a private key
        :type password: str
        :param pkey: an optional private key to use for authentication
        :type pkey: :class:`PKey`
        :param key_filename: the filename, or list of filenames, of optional
            private key(s) to try for authentication
        :type key_filename: str or list(str)
        :param timeout: an optional timeout (in seconds) for the TCP connect
        :type timeout: float
        :param allow_agent: set to False to disable connecting to the SSH agent
        :type allow_agent: bool
        :param look_for_keys: set to False to disable searching for discoverable
            private key files in ``~/.ssh/``
        :type look_for_keys: bool
        :param compress: set to True to turn on compression
        :type compress: bool
        :param sock: an open socket or socket-like object (such as a
            :class:`Channel`) to use for communication to the target host
        :type sock: socket

        :raises BadHostKeyException: if the server's host key could not be
            verified
        :raises AuthenticationException: if authentication failed
        :raises SSHException: if there was any other error connecting or
            establishing an SSH session
        :raises socket.error: if a socket error occurred while connecting
        """
        if not sock:
            for (family, socktype, proto, canonname, sockaddr) in socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM):
                if socktype == socket.SOCK_STREAM:
                    af = family
                    addr = sockaddr
                    break
            else:
                # some OS like AIX don't indicate SOCK_STREAM support, so just guess. :(
                af, _, _, _, addr = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            sock = socket.socket(af, socket.SOCK_STREAM)
            if timeout is not None:
                try:
                    sock.settimeout(timeout)
                except:
                    pass
            retry_on_signal(lambda: sock.connect(addr))

        t = self._transport = Transport(sock)
        t.use_compression(compress=compress)
        if self._log_channel is not None:
            t.set_log_channel(self._log_channel)
        t.start_client()
        ResourceManager.register(self, t)

        server_key = t.get_remote_server_key()
        keytype = server_key.get_name()

        if port == SSH_PORT:
            server_hostkey_name = hostname
        else:
            server_hostkey_name = "[%s]:%d" % (hostname, port)
        our_server_key = self._system_host_keys.get(server_hostkey_name, {}).get(keytype, None)
        if our_server_key is None:
            our_server_key = self._host_keys.get(server_hostkey_name, {}).get(keytype, None)
        if our_server_key is None:
            # will raise exception if the key is rejected; let that fall out
            self._policy.missing_host_key(self, server_hostkey_name, server_key)
            # if the callback returns, assume the key is ok
            our_server_key = server_key

        if server_key != our_server_key:
            raise BadHostKeyException(hostname, server_key, our_server_key)

        if username is None:
            username = getpass.getuser()

        if key_filename is None:
            key_filenames = []
        elif isinstance(key_filename, (str, unicode)):
            key_filenames = [ key_filename ]
        else:
            key_filenames = key_filename
        self._auth(username, password, pkey, key_filenames, allow_agent, look_for_keys)

    def close(self):
        """
        Close this SSHClient and its underlying :class:`Transport`.
        """
        if self._transport is None:
            return
        self._transport.close()
        self._transport = None

        if self._agent != None:
            self._agent.close()
            self._agent = None

    def exec_command(self, command, bufsize=-1, timeout=None, get_pty=False):
        """
        Execute a command on the SSH server.  A new :class:`Channel` is opened and
        the requested command is executed.  The command's input and output
        streams are returned as python ``file``-like objects representing
        stdin, stdout, and stderr.

        :param command: the command to execute
        :type command: str
        :param bufsize: interpreted the same way as by the built-in ``file()`` function in python
        :type bufsize: int
        :param timeout: set command's channel timeout. See :class:`Channel.settimeout`.settimeout
        :type timeout: int
        :return: the stdin, stdout, and stderr of the executing command
        :rtype: tuple(:class:`ChannelFile`, :class:`ChannelFile`, :class:`ChannelFile`)

        :raises SSHException: if the server fails to execute the command
        """
        chan = self._transport.open_session()
        if(get_pty):
            chan.get_pty()
        chan.settimeout(timeout)
        chan.exec_command(command)
        stdin = chan.makefile('wb', bufsize)
        stdout = chan.makefile('rb', bufsize)
        stderr = chan.makefile_stderr('rb', bufsize)
        return stdin, stdout, stderr

    def invoke_shell(self, term='vt100', width=80, height=24, width_pixels=0,
                height_pixels=0):
        """
        Start an interactive shell session on the SSH server.  A new :class:`Channel`
        is opened and connected to a pseudo-terminal using the requested
        terminal type and size.

        :param term: the terminal type to emulate (for example, ``"vt100"``)
        :type term: str
        :param width: the width (in characters) of the terminal window
        :type width: int
        :param height: the height (in characters) of the terminal window
        :type height: int
        :param width_pixels: the width (in pixels) of the terminal window
        :type width_pixels: int
        :param height_pixels: the height (in pixels) of the terminal window
        :type height_pixels: int
        :return: a new channel connected to the remote shell
        :rtype: :class:`Channel`

        :raises SSHException: if the server fails to invoke a shell
        """
        chan = self._transport.open_session()
        chan.get_pty(term, width, height, width_pixels, height_pixels)
        chan.invoke_shell()
        return chan

    def open_sftp(self):
        """
        Open an SFTP session on the SSH server.

        :return: a new SFTP session object
        :rtype: :class:`SFTPClient`
        """
        return self._transport.open_sftp_client()

    def get_transport(self):
        """
        Return the underlying :class:`Transport` object for this SSH connection.
        This can be used to perform lower-level tasks, like opening specific
        kinds of channels.

        :return: the Transport for this connection
        :rtype: :class:`Transport`
        """
        return self._transport

    def _auth(self, username, password, pkey, key_filenames, allow_agent, look_for_keys):
        """
        Try, in order:

            - The key passed in, if one was passed in.
            - Any key we can find through an SSH agent (if allowed).
            - Any "id_rsa" or "id_dsa" key discoverable in ~/.ssh/ (if allowed).
            - Plain username/password auth, if a password was given.

        (The password might be needed to unlock a private key, or for
        two-factor authentication [for which it is required].)
        """
        saved_exception = None
        two_factor = False
        allowed_types = []

        if pkey is not None:
            try:
                self._log(DEBUG, 'Trying SSH key %s' % hexlify(pkey.get_fingerprint()))
                allowed_types = self._transport.auth_publickey(username, pkey)
                two_factor = (allowed_types == ['password'])
                if not two_factor:
                    return
            except SSHException, e:
                saved_exception = e

        if not two_factor:
            for key_filename in key_filenames:
                for pkey_class in (RSAKey, DSSKey):
                    try:
                        key = pkey_class.from_private_key_file(key_filename, password)
                        self._log(DEBUG, 'Trying key %s from %s' % (hexlify(key.get_fingerprint()), key_filename))
                        self._transport.auth_publickey(username, key)
                        two_factor = (allowed_types == ['password'])
                        if not two_factor:
                            return
                        break
                    except SSHException, e:
                        saved_exception = e

        if not two_factor and allow_agent:
            if self._agent == None:
                self._agent = Agent()

            for key in self._agent.get_keys():
                try:
                    self._log(DEBUG, 'Trying SSH agent key %s' % hexlify(key.get_fingerprint()))
                    # for 2-factor auth a successfully auth'd key will result in ['password']
                    allowed_types = self._transport.auth_publickey(username, key)
                    two_factor = (allowed_types == ['password'])
                    if not two_factor:
                        return
                    break
                except SSHException, e:
                    saved_exception = e

        if not two_factor:
            keyfiles = []
            rsa_key = os.path.expanduser('~/.ssh/id_rsa')
            dsa_key = os.path.expanduser('~/.ssh/id_dsa')
            if os.path.isfile(rsa_key):
                keyfiles.append((RSAKey, rsa_key))
            if os.path.isfile(dsa_key):
                keyfiles.append((DSSKey, dsa_key))
            # look in ~/ssh/ for windows users:
            rsa_key = os.path.expanduser('~/ssh/id_rsa')
            dsa_key = os.path.expanduser('~/ssh/id_dsa')
            if os.path.isfile(rsa_key):
                keyfiles.append((RSAKey, rsa_key))
            if os.path.isfile(dsa_key):
                keyfiles.append((DSSKey, dsa_key))

            if not look_for_keys:
                keyfiles = []

            for pkey_class, filename in keyfiles:
                try:
                    key = pkey_class.from_private_key_file(filename, password)
                    self._log(DEBUG, 'Trying discovered key %s in %s' % (hexlify(key.get_fingerprint()), filename))
                    # for 2-factor auth a successfully auth'd key will result in ['password']
                    allowed_types = self._transport.auth_publickey(username, key)
                    two_factor = (allowed_types == ['password'])
                    if not two_factor:
                        return
                    break
                except SSHException, e:
                    saved_exception = e
                except IOError, e:
                    saved_exception = e

        if password is not None:
            try:
                self._transport.auth_password(username, password)
                return
            except SSHException, e:
                saved_exception = e
        elif two_factor:
            raise SSHException('Two-factor authentication requires a password')

        # if we got an auth-failed exception earlier, re-raise it
        if saved_exception is not None:
            raise saved_exception
        raise SSHException('No authentication methods available')

    def _log(self, level, msg):
        self._transport._log(level, msg)


class MissingHostKeyPolicy (object):
    """
    Interface for defining the policy that :class:`SSHClient` should use when the
    SSH server's hostname is not in either the system host keys or the
    application's keys.  Pre-made classes implement policies for automatically
    adding the key to the application's :class:`HostKeys` object (:class:`AutoAddPolicy`),
    and for automatically rejecting the key (:class:`RejectPolicy`).

    This function may be used to ask the user to verify the key, for example.
    """

    def missing_host_key(self, client, hostname, key):
        """
        Called when an :class:`SSHClient` receives a server key for a server that
        isn't in either the system or local :class:`HostKeys` object.  To accept
        the key, simply return.  To reject, raised an exception (which will
        be passed to the calling application).
        """
        pass


class AutoAddPolicy (MissingHostKeyPolicy):
    """
    Policy for automatically adding the hostname and new host key to the
    local :class:`HostKeys` object, and saving it.  This is used by :class:`SSHClient`.
    """

    def missing_host_key(self, client, hostname, key):
        client._host_keys.add(hostname, key.get_name(), key)
        if client._host_keys_filename is not None:
            client.save_host_keys(client._host_keys_filename)
        client._log(DEBUG, 'Adding %s host key for %s: %s' %
                    (key.get_name(), hostname, hexlify(key.get_fingerprint())))


class RejectPolicy (MissingHostKeyPolicy):
    """
    Policy for automatically rejecting the unknown hostname & key.  This is
    used by :class:`SSHClient`.
    """

    def missing_host_key(self, client, hostname, key):
        client._log(DEBUG, 'Rejecting %s host key for %s: %s' %
                    (key.get_name(), hostname, hexlify(key.get_fingerprint())))
        raise SSHException('Server %r not found in known_hosts' % hostname)


class WarningPolicy (MissingHostKeyPolicy):
    """
    Policy for logging a python-style warning for an unknown host key, but
    accepting it. This is used by :class:`SSHClient`.
    """
    def missing_host_key(self, client, hostname, key):
        warnings.warn('Unknown %s host key for %s: %s' %
                      (key.get_name(), hostname, hexlify(key.get_fingerprint())))

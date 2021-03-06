import time
import threading
import socket
import logging

from dynamo.core.components.host import ServerHost, OutOfSyncError
from dynamo.core.components.master import MasterServer, AppManager
from dynamo.core.components.board import UpdateBoard

LOG = logging.getLogger(__name__)

class ServerManager(object):
    """
    Manager for the application and updates table and the connections to other servers.
    """

    def __init__(self, config):
        # Create a master server interface
        self.master = MasterServer.get_instance(config.master.module, config.master.config)
        self.master.readonly_config = config.master.readonly_config
        self.master.connect()
        self.master_host = self.master.get_host()

        if self.master_host != 'localhost' and self.master_host != socket.gethostname():
            # Interface to the master server local shadow
            # When the master server dies, this host can become the next master. Need to have
            # data copied locally in preparation.
            self.shadow = MasterServer.get_instance(config.shadow.module, config.shadow.config)
        else:
            self.shadow = None

        # Interface to the local update board
        self.board = UpdateBoard.get_instance(config.board.module, config.board.config)

        # Interface to other servers {hostname: ServerHost}
        self.other_servers = {}

        # If using a remote store, name of the host
        self.store_host = ''

        self.hostname = socket.gethostname()
        
        self.status = ServerHost.STAT_INITIAL

        # Heartbeat is sent in a separate thread
        self.heartbeat = threading.Thread(target = self.send_heartbeat)
        self.heartbeat.daemon = True
        self.heartbeat.start()

    def set_status(self, status, hostname = None):
        """
        Set status of this or another host.
        """

        if hostname is None:
            # this host
            self.master.lock()
            try:
                if self.get_status() == ServerHost.STAT_OUTOFSYNC:
                    self.status = ServerHost.STAT_OUTOFSYNC
                    raise OutOfSyncError('Server out of sync')
    
                self.master.set_status(status, self.hostname)

            finally:
                self.master.unlock()

            self.status = status

        else:
            # another host
            self.master.set_status(status, hostname)

    def reset_status(self):
        """
        Reset server status to initial when it is out of sync.
        """
        self.master.lock()
        try:
            # first check that we are out of sync
            if self.get_status() != ServerHost.STAT_OUTOFSYNC:
                raise RuntimeError('reset_status called when status is not outofsync')

            # then reset
            self.master.set_status(ServerHost.STAT_INITIAL, self.hostname)

        finally:
            self.master.unlock()

        self.status = ServerHost.STAT_INITIAL

    def check_status(self):
        """
        1. Check connection to the master server
        2. Update the status from the master server
        """

        if not self.master.check_connection():
            raise OutOfSyncError('Lost connection to master server')

        self.get_status()

        if self.status == ServerHost.STAT_ERROR:
            raise RuntimeError('Server status is ERROR')

        elif self.status == ServerHost.STAT_OUTOFSYNC:
            raise OutOfSyncError('Server out of sync')

    def get_status(self, hostname = None):
        """
        Read the server status from the master list.
        """
        if hostname is None:
            status = self.master.get_status(self.hostname)
        else:
            status = self.master.get_status(hostname)

        if status is None:
            status_val = None
        else:
            status_val = ServerHost.status_val(status)

        if hostname is None:
            self.status = status_val

        return status_val

    def count_servers(self, status):
        """
        Count the number of servers (including self) in the given status.
        @parameter status  A single SRV_* value or a list of SRV_*
        """
        try:
            len(status)
        except TypeError:
            # single value
            return len(self.master.get_host_list(status = status))
        else:
            # use a set just in case for some reason the host changes the status in the middle of counting
            hostnames = set()
            for stat in status:
                hostnames.update(n for n, _, _ in self.master.get_host_list(status = stat))
            return len(hostnames)

    def get_updates(self):
        """
        Return entries from the local update board as an iterable.
        """
        self.board.lock()
        try:
            update_commands = self.board.get_updates()
            has_updates = False
            for cmd, obj in update_commands:
                has_updates = True
                yield (cmd, obj)

            if has_updates:
                self.board.flush()

        finally:
            self.board.unlock()

        return

    def send_heartbeat(self):
        """
        Send the heartbeat to the master server. Additionally check for status updates made by peers.
        If a peer tries to update the inventory content of this server and fails for some reason, the
        server status is forced to be out-of-sync.
        """

        while True:
            if self.status != ServerHost.STAT_INITIAL:
                self.master.send_heartbeat()

                if self.shadow is not None:
                    self.shadow.copy(self.master)
    
            time.sleep(30)

    def reconnect_master(self):
        """
        Find and connect to the new master server.
        """
        if not self.shadow:
            # Master server was local
            raise RuntimeError('Cannot reconnect to local master server shadow.')

        module, config = self.shadow.get_next_master(self.master_host)
        
        self.master = MasterServer.get_instance(module, config)
        self.master.connect()
        self.master_host = self.master.get_host()

        if self.master_host == 'localhost' or self.master_host == socket.gethostname():
            self.shadow = None

    def find_remote_store(self, hostname = ''):
        """
        Find a remote host backed up by a persistency store and has status 'online'.
        return None if no server is found, or a pair (module_name, config) if found.
        """
        while True:
            self.collect_hosts()

            is_updating = False
            for server in self.other_servers.itervalues():
                if hostname and server.hostname != hostname:
                    continue

                if not server.has_store:
                    continue

                if server.status == ServerHost.STAT_ONLINE:
                    # store_config = (module, config, version)
                    store_config = self.master.get_store_config(server.hostname)
                    if store_config is None:
                        continue

                    return (server.hostname,) + store_config
                    
                elif server.status == ServerHost.STAT_UPDATING:
                    is_updating = True

            if is_updating:
                time.sleep(5)
            else:
                self.set_status(ServerHost.STAT_ERROR)
                raise RuntimeError('Could not find a remote persistency store to connect to.')

    def register_remote_store(self, hostname):
        self.store_host = hostname
        self.master.declare_remote_store(hostname)

    def collect_hosts(self):
        """
        Keep the other host list up-to-date.
        """
        known_hosts = set()

        for hostname, status, has_store in self.master.get_host_list():
            if hostname == self.hostname:
                continue

            try:
                server = self.other_servers[hostname]
            except KeyError:
                board_conf = self.master.get_board_config(hostname)
                if board_conf is None:
                    # shouldn't happen
                    continue

                server = ServerHost(hostname)
                server.board = UpdateBoard.get_instance(board_conf[0], board_conf[1])

                self.other_servers[hostname] = server

            server.has_store = (has_store != 0)
            server.status = ServerHost.status_val(status)

            known_hosts.add(hostname)

        for hostname in set(self.other_servers.keys()) - known_hosts:
            self.other_servers.pop(hostname)

    def send_updates(self, update_commands):
        """
        Send the list of update commands to all online servers.

        @param update_commands  List of two-tuples (cmd, obj)
        """
        # Write-enabled process and server start do not happen simultaneously.
        # No servers could have come online while we were running a write-enabled process - other_servers is the full list
        # of running servers.

        processed = set()

        while True:
            self.master.lock()

            try:
                self.collect_hosts()
    
                # update only one server at a time to minimize the time in lock
                for server in self.other_servers.itervalues():
                    if server.hostname not in processed:
                        break
                else:
                    # all processed, we are done
                    break

                if server.status == ServerHost.STAT_ONLINE:
                    processed.add(server.hostname)

                    self.set_status(ServerHost.STAT_UPDATING, server.hostname)
                    try:
                        server.board.write_updates(update_commands)
                    except:
                        LOG.error('Error while sending updates to %s. Setting server state to OUTOFSYNC.', server.hostname)
                        self.set_status(ServerHost.STAT_OUTOFSYNC, server.hostname)
                    else:
                        LOG.info('Sent %d update commands to %s.', len(update_commands), server.hostname)
                        

                elif server.status == ServerHost.STAT_UPDATING:
                    # this server is still processing updates from the previous write process
                    continue

                else:
                    # any other status means the server is not running
                    processed.add(server.hostname)

            finally:
                self.master.unlock()

            time.sleep(1)

    def disconnect(self):
        """
        Go offline and delete the entry from the master server list.
        """
        self.master.disconnect()
        for server in self.other_servers.itervalues():
            if server.board:
                server.board.disconnect()

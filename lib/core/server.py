import os
import sys
import pwd
import time
import logging
import hashlib
import shlex
import signal
import multiprocessing
import Queue

from dynamo.core.inventory import DynamoInventory
from dynamo.core.registry import DynamoRegistry
from dynamo.utils.signaling import SignalBlocker

LOG = logging.getLogger(__name__)
CHANGELOG = logging.getLogger('changelog')

def killproc(proc):
    uid = os.geteuid()
    os.seteuid(0)
    proc.terminate()
    os.seteuid(uid)
    proc.join(5)

class Dynamo(object):
    """Main daemon class."""

    def __init__(self, config):
        LOG.info('Initializing Dynamo server %s.', __file__)

        ## User names
        # User with full privilege (still not allowed to write to inventory store)
        self.full_user = config.user
        # Restricted user
        self.read_user = config.read_user

        ## Create the registry
        self.registry = DynamoRegistry(config.registry)
        self.registry_config = config.registry.clone()

        ## Create the inventory
        self.inventory = DynamoInventory(config.inventory, load = False)
        self.inventory_config = config.inventory.clone()

        ## Load the inventory content (filter according to debug config)
        load_opts = {}
        if 'debug' in config:
            for objs in ['groups', 'sites', 'datasets']:
                included = config.debug.get('included_' + objs, None)
                excluded = config.debug.get('excluded_' + objs, None)
    
                load_opts[objs] = (included, excluded)
        
        LOG.info('Loading the inventory.')
        self.inventory.load(**load_opts)

    def run(self):
        """
        Infinite-loop main body of the daemon.
        Step 1: Poll the registry for one uploaded script.
        Step 2: If a script is found, check the authorization of the script.
        Step 3: Spawn a child process for the script.
        Step 4: Collect completed child processes. Get updates from the write-enabled child process if there is one.
        Step 5: Sleep for N seconds.
        """

        LOG.info('Started dynamo daemon.')

        child_processes = []

        # There can only be one child process with write access at a time. We pass it a Queue to communicate back.
        # writing_process is a tuple (proc, queue) when some process is writing
        writing_process = (0, None)

        try:
            LOG.info('Start polling for executables.')

            first_wait = True
            sleep_time = 0

            while True:
                self.registry.backend.query('UNLOCK TABLES')

                ## Step 4 (easier to do here because we use "continue"s)
                completed_processes = self.collect_processes(child_processes, writing_process)

                if writing_process[0] in [exec_id for exec_id, status in completed_processes]:
                    writing_process = (0, None)

                ## Step 5 (easier to do here because we use "continue"s)
                time.sleep(sleep_time)

                ## Step 1: Poll
                LOG.debug('Polling for executables.')

                # UNLOCK statement at the top of the while loop
                self.registry.backend.query('LOCK TABLES `action` WRITE')

                sql = 'SELECT s.`id`, s.`write_request`, s.`title`, s.`path`, s.`args`, s.`user_id`, u.`name`'
                sql += ' FROM `action` AS s INNER JOIN `users` AS u ON u.`id` = s.`user_id`'
                sql += ' WHERE s.`status` = \'new\''
                if writing_process[0] != 0:
                    # we don't allow write_requesting executables while there is one running
                    sql += ' AND s.`write_request` = 0'
                sql += ' ORDER BY s.`timestamp` LIMIT 1'
                result = self.registry.backend.query(sql)

                if len(result) == 0:
                    if len(child_processes) == 0 and first_wait:
                        LOG.info('Waiting for executables.')
                        first_wait = False

                    sleep_time = 0.5

                    LOG.debug('No executable found, sleeping for %d seconds.', sleep_time)

                    continue

                ## Step 2: If a script is found, check the authorization of the script.
                exec_id, write_request, title, path, args, user_id, user_name = result[0]

                first_wait = True
                sleep_time = 0

                if not os.path.exists(path + '/exec.py'):
                    LOG.info('Executable %s from user %s (write request: %s) not found.', title, user_name, write_request)
                    self.registry.backend.query('UPDATE `action` SET `status` = \'notfound\' WHERE `id` = %s', exec_id)
                    continue

                LOG.info('Found executable %s from user %s (write request: %s)', title, user_name, write_request)

                proc_args = (path, args)

                if write_request:
                    if not self.check_write_auth(title, user_id, path):
                        LOG.warning('Executable %s from user %s is not authorized for write access.', title, user_name)
                        # send a message

                        self.registry.backend.query('UPDATE `action` SET `status` = \'authfailed\' where `id` = %s', exec_id)
                        continue

                    queue = multiprocessing.Queue()
                    proc_args += (queue,)

                    writing_process = (exec_id, queue)

                ## Step 3: Spawn a child process for the script
                self.registry.backend.query('UPDATE `action` SET `status` = \'run\' WHERE `id` = %s', exec_id)

                proc = multiprocessing.Process(target = self._run_one, name = title, args = proc_args)
                child_processes.append((exec_id, proc, user_name, path))

                proc.daemon = True
                proc.start()

                LOG.info('Started executable %s (%s) from user %s (PID %d).', title, path, user_name, proc.pid)

        except KeyboardInterrupt:
            LOG.info('Server process was interrupted.')

        except:
            # log the exception
            LOG.warning('Exception in server process. Terminating all child processes.')
            raise

        finally:
            # If the main process was interrupted by Ctrl+C:
            # Ctrl+C will pass SIGINT to all child processes (if this process is the head of the
            # foreground process group). In this case calling terminate() will duplicate signals
            # in the child. Child processes have to always ignore SIGINT and be killed only from
            # SIGTERM sent by the line below.

            self.registry.backend.query('UNLOCK TABLES')

            for exec_id, proc, user_name, path in child_processes:
                LOG.warning('Terminating %s (%s) requested by %s (PID %d)', proc.name, path, user_name, proc.pid)

                killproc(proc)

                if proc.is_alive():
                    LOG.warning('Child process %d did not return after 5 seconds.', proc.pid)

                self.registry.backend.query('UPDATE `action` SET `status` = \'killed\' where `id` = %s', exec_id)

    def check_write_auth(self, title, user_id, path):
        # check authorization
        with open(path + '/exec.py') as source:
            checksum = hashlib.md5(source.read()).hexdigest()

        sql = 'SELECT `user_id` FROM `authorized_executables` WHERE `title` = %s AND `checksum` = UNHEX(%s)'
        for auth_user_id in self.registry.backend.query(sql, title, checksum):
            if auth_user_id == 0 or auth_user_id == user_id:
                return True

        return False

    def collect_processes(self, child_processes, writing_process):
        completed_processes = []

        ichild = 0
        while ichild != len(child_processes):
            exec_id, proc, user_name, path = child_processes[ichild]

            status = ''

            # Was the job aborted in the registry?
            result = self.registry.backend.query('SELECT `status` FROM `action` WHERE `id` = %s', exec_id)
            if len(result) == 0 or result[0] != 'run':
                status = 'killed'
                killproc(proc)
                proc.join(60)

            elif exec_id == writing_process[0]:
                # If this is the writing process, read data from the queue
                
                # read_state: 0 -> nothing written yet, 1 -> read OK, 2 -> failure
                read_state, update_commands = self.collect_updates(writing_process[1])

                if read_state == 1:
                    status = 'done'

                    # Block system signals and get update done
                    with SignalBlocker(logger = LOG):
                        for cmd, obj in update_commands:
                            if cmd == DynamoInventory.CMD_UPDATE:
                                self.inventory.update(obj, write = True, changelog = CHANGELOG)
                            elif cmd == DynamoInventory.CMD_DELETE:
                                CHANGELOG.info('Deleting %s', str(obj))
                                self.inventory.delete(obj, write = True)

                elif read_state == 2:
                    status = 'failed'
                    killproc(proc)

                if read_state != 0:
                    proc.join(60)
    
            if proc.is_alive():
                if not status:
                    ichild += 1
                    continue
                else:
                    # status set -> the process must be complete but did not join within 60 seconds
                    LOG.error('Executable %s (%s) from user %s is stuck (Status %s).', proc.name, path, user_name, status)
            else:
                if not status:
                    if proc.exitcode == 0:
                        status = 'done'
                    else:
                        status = 'failed'

                LOG.info('Executable %s (%s) from user %s completed (Exit code %d Status %s).', proc.name, path, user_name, proc.exitcode, status)

            # process completed or is alive but stuck -> remove from the list and set status in the table
            child_proc = child_processes.pop(ichild)
            completed_processes.append((child_proc[0], status))

            self.registry.backend.query('UPDATE `action` SET `status` = %s, `exit_code` = %s where `id` = %s', status, proc.exitcode, exec_id)

        return completed_processes

    def collect_updates(self, queue):
        print_every = 1000
        updates_received = 0
        deletes_received = 0

        reading = False
        update_commands = []

        while True:
            try:
                # If drain is True, we are calling this function to wait to empty out the queue.
                # In case the child process fails to put EOM at the end, we time out in 60 seconds.
                cmd, obj = queue.get(block = reading, timeout = 60)
            except Queue.Empty:
                if reading:
                    # The child process crashed or timed out
                    return 2, update_commands
                else:
                    return 0, update_commands
            else:
                reading = True

                if LOG.getEffectiveLevel() == logging.DEBUG:
                    LOG.debug('From queue: %d %s', cmd, obj)

                if cmd == DynamoInventory.CMD_UPDATE:
                    updates_received += 1
                    update_commands.append((cmd, obj))
                elif cmd == DynamoInventory.CMD_DELETE:
                    deletes_received += 1
                    update_commands.append((cmd, obj))

                if cmd == DynamoInventory.CMD_EOM or len(update_commands) % print_every == 0:
                    LOG.info('Received %d updates and %d deletes.', updates_received, deletes_received)

                if cmd == DynamoInventory.CMD_EOM:
                    return 1, update_commands
        
    def _run_one(self, path, args, queue = None):
        # Set the uid of the process
        os.seteuid(0)
        os.setegid(0)

        if queue is None:
            pwnam = pwd.getpwnam(self.read_user)
        else:
            pwnam = pwd.getpwnam(self.full_user)

        os.setgid(pwnam.pw_gid)
        os.setuid(pwnam.pw_uid)

        # Redirect STDOUT and STDERR to file, close STDIN
        stdout = sys.stdout
        stderr = sys.stderr
        sys.stdout = open(path + '/_stdout', 'a')
        sys.stderr = open(path + '/_stderr', 'a')
        sys.stdin.close()

        ## Ignore SIGINT - see note above proc.terminate()
        ## We will react to SIGTERM by raising KeyboardInterrupt
        from dynamo.utils.signaling import SignalConverter
        
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        signal_converter = SignalConverter()
        signal_converter.set(signal.SIGTERM)

        # Set argv
        sys.argv = [path + '/exec.py']
        if args:
            sys.argv += shlex.split(args) # split using shell-like syntax

        # Reset logging
        # This is a rather hacky solution relying perhaps on the implementation internals of
        # the logging module. It might stop working with changes to the logging.
        # The assumptions are:
        #  1. All loggers can be reached through Logger.manager.loggerDict
        #  2. All logging.shutdown() does is call flush() and close() over all handlers
        #     (i.e. calling the two is enough to ensure clean cutoff from all resources)
        #  3. root_logger.handlers is the only link the root logger has to its handlers
        for logger in [logging.getLogger()] + logging.Logger.manager.loggerDict.values():
            while True:
                try:
                    handler = logger.handlers.pop()
                except AttributeError:
                    # logger is just a PlaceHolder and does not have .handlers
                    break
                except IndexError:
                    break
    
                handler.flush()
                handler.close()

        # Re-initialize
        #  - inventory store with read-only connection
        #  - registry backend with read-only connection
        # This is for security and simply for concurrency - multiple processes
        # should not share the same DB connection
        backend_config = self.registry_config.backend
        self.registry.set_backend(backend_config.interface, backend_config.readonly_config)

        persistency_config = self.inventory_config.persistency
        self.inventory.init_store(persistency_config.module, persistency_config.readonly_config)

        # Pass my registry and inventory to the executable through core.executable
        import dynamo.core.executable as executable
        executable.registry = self.registry
        executable.inventory = self.inventory

        if queue is not None:
            executable.read_only = False
            # create a list of updated and deleted objects the executable can fill
            executable.inventory._update_commands = []

        try:
            execfile(path + '/exec.py', {'__name__': '__main__'})
        except SystemExit as exc:
            if exc.code == 0:
                pass
            else:
                raise

        if queue is not None:
            nobj = len(self.inventory._update_commands)
            sys.stderr.write('Sending %d updated objects to the server process.\n' % nobj)
            sys.stderr.flush()
            wm = 0.
            for iobj, (cmd, obj) in enumerate(self.inventory._update_commands):
                if float(iobj) / nobj * 100. > wm:
                    sys.stderr.write(' %.0f%%..' % (float(iobj) / nobj * 100.))
                    sys.stderr.flush()
                    wm += 5.

                try:
                    queue.put((cmd, obj))
                except:
                    sys.stderr.write('Exception while sending %s %s\n' % (DynamoInventory._cmd_str[cmd], str(obj)))
                    raise

            if nobj != 0:
                sys.stderr.write(' 100%.\n')
                sys.stderr.flush()
            
            # Put end-of-message
            queue.put((DynamoInventory.CMD_EOM, None))

        # Queue stays available on the other end even if we terminate the process

        sys.stdout.close()
        sys.stderr.close()
        sys.stdout = stdout
        sys.stderr = stderr

        return 0
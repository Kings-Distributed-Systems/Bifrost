from .py_storage import *
from .ReadWriteLock import ReadWriteLock
import time, posix_ipc
import numpy as np
import os, sys, socket
import subprocess, signal
from threading import Thread, Event, Lock
from subprocess import call, Popen, PIPE
import atexit

#Simple python global read write lock
NODE_LOCK       = ReadWriteLock()
#variable that is being locked
NODE_IS_RUNNING = False

class Npm():
    '''
    Npm is a class that manages all calls to npm.
    '''
    def __init__(self, cwd = os.getcwd()):
        self.cwd = cwd
        if not (os.path.exists(cwd + '/node_modules/npy-js') and os.path.exists(cwd + '/node_modules/mmap.js') and os.path.exists(cwd + '/node_modules/xxhash') and os.path.exists(cwd + '/node_modules/nodeshm') ):
            self.run(['npm', 'init', '--yes'])
            self.run(['npm', 'install', 
                      'git+https://github.com/Kings-Distributed-Systems/npy-js.git', 'git+https://github.com/bungabear/mmap.js', 'nodeshm', 'xxhash'])

    def run(self, cmd):
        '''
        Helper function to run some command using npm.
        Useful for managing working directory of npm and where the 
        stdout pipes are pointing.

        Also helpful to block until command completes.
        '''
        process = Popen(cmd, cwd = self.cwd, stdout = subprocess.PIPE)
        while True:
            output = process.stdout.readline().decode('utf-8')
            if output == '' and process.poll() is not None:
                break
            if output:
                print(output.strip())
        returnCode = process.poll()
        return returnCode

    def install(self,*args):
        self.run(['npm', 'install', *args])

    def uninstall(self, *args):
        self.run(['npm', 'uninstall', *args])

    def list_modules(self, *args):
        self.run(['npm', 'list', *args])


class NodeSTDProc(Thread):
    '''
    Helper class that is run in another thread from the main thread.
    This runs in the background collecting information from the node
    process and deciding how to manage it.

    In particular, this manages stdout and completion of node process
    execution.
    '''
    def __init__(self, process):
        super(NodeSTDProc, self).__init__()
        self.process     = process
        self._stop_event = Event()
        self.daemon      = True
        self.start()

    def stop(self):
        '''
        Stop this thread.
        '''
        self._stop_event.set()

    def run(self):
        '''Main loop for the thread.'''
        while not self._stop_event.is_set():

            global NODE_IS_RUNNING
            global NODE_LOCK
            output = self.process.stdout.readline().decode('utf-8')
            #if process.poll() returns something,
            #this means that the process has ended. End this thread too.
            if self.process.poll() is not None:
                NODE_LOCK.acquire_write()
                NODE_IS_RUNNING = False
                NODE_LOCK.release_write()
                break
            #if our output is an empty string, do nothing.
            if output == '':
                continue
            #'if our output is not an empty string, try processing it.
            if output:
                try:
                    #if our output is json serializeable, check if
                    #it has type=== done before setting NODE_IS_RUNNING to false
                    output_json = json.loads(output)
                    if output_json['type'] == 'done':
                        NODE_LOCK.acquire_write()
                        NODE_IS_RUNNING = False
                        NODE_LOCK.release_write()
                    else:
                        #otherwise print out the json
                        if (output and len(output.strip()) > 0):
                            print(output.strip())
                    continue
                except Exception as e:
                    #otherwise print out the json
                    if (output and len(output.strip()) > 0):
                        print(output.strip())
                    continue 





class Node():
    '''
    This class is a helper class to manage the node process. 
    '''
    def __init__(self, cwd= os.getcwd()):
        self.cwd = cwd
        self.serializer_custom_funcs = {}
        self.deserializer_custom_funcs = {}
        #the replFile is the main file that preps the node runtime for use with this module
        self.replFile = os.path.dirname(os.path.realpath(__file__)) + '/main.js'
        #The variable synchronization manager
        self.vs = VariableSync()

        self.init_process()


    def init_process(self):
        '''
        Initialize the process by running node with a larger
        max-old-space-size and with all the important information
        regarding the shared memory name and the repl file.
        '''
        env = os.environ
        #make sure to add the current path to the node_path
        env["NODE_PATH"] = self.cwd + '/node_modules'
        #ready the node process
        self.process = Popen(['node',
                              '--max-old-space-size=32000',
                              self.replFile,
                              self.vs.SHARED_MEMORY_NAME], cwd=self.cwd,stdin=subprocess.PIPE,
                              env=env,
                              stdout=subprocess.PIPE)

        #ready the node stdout manager
        self.nstdproc = NodeSTDProc(self.process)

    def register_custom_serializer(self, func, var_type):
        '''
        Register a custom serializer for a particular variable type
        '''
        if var_type is not str:
            var_type = str(var_type)
        self.serializer_custom_funcs[var_type] = func
        return


    def register_custom_deserializer(self, func, var_type):
        '''
        Register a custom deserializer for a particular variable type
        '''
        if var_type is not str:
            var_type = str(var_type)
        self.deserializer_custom_funcs[var_type] = func

    def run(self, script, vars = {}, timeout=None):
        '''
        The main function which runs some node script.

        Will synchronize variables and wait a max of timeout before cancelling job
        run. Note that timeout is default to None and if it is None will never timeout.

        '''

        #synchronize variables first
        self.vs.syncto(vars, self.serializer_custom_funcs, warn=False)

        #get the lock and mark as running.
        global NODE_IS_RUNNING
        global NODE_LOCK
        NODE_LOCK.acquire_write()
        NODE_IS_RUNNING = True
        NODE_LOCK.release_write()
        #Send script to process.
        retCode = self.write(script)

        if retCode < 0:
            print("Could not run script")
            return

        #Keep running until the stdout process marks
        #NODE_IS_RUNNING to False.
        #This is a little dangerous as we need to make sure that NODE_IS_RUNNING
        #Will, at some point, resolve to False.
        flag = NODE_IS_RUNNING
        start = time.time()
        while flag:
            try:
                NODE_LOCK.acquire_read()
                flag = NODE_IS_RUNNING
                NODE_LOCK.release_read()
                if timeout is not None:
                    if (time.time() - start) > timeout:
                        self.cancel()
                        NODE_LOCK.acquire_write()
                        NODE_IS_RUNNING = False
                        NODE_LOCK.release_write()
                        print("Process took longer than " + str(timeout))
            except KeyboardInterrupt:
                self.cancel()
                NODE_LOCK.acquire_write()
                NODE_IS_RUNNING = False
                NODE_LOCK.release_write()
                print("Process was interrupted.")
                raise KeyboardInterrupt
        new_vars = self.vs.syncfrom(self.deserializer_custom_funcs, warn=False)
        for key in new_vars.keys():
            vars[key] = new_vars[key]
        return vars

    def clean_lock(self):
        global NODE_LOCK
        global NODE_IS_RUNNING
        NODE_IS_RUNNING = False
        NODE_LOCK = ReadWriteLock()


    def write(self, s):
        '''
        Helper function to submit node script to node process.
        '''
        try:
            string_to_send = json.dumps(
                {'script': s}
            )
            self.process.stdin.write(string_to_send.encode('utf-8'))
            self.process.stdin.flush()
        except Exception as e:
            global NODE_IS_RUNNING
            global NODE_LOCK
            if 'Broken pipe' in str(e):
                self.cancel()
                print("Pipe broke and was restarted")
            else:
                self.cancel()
                print("Pipe died for some reason: ")
                print(str(e))
            NODE_LOCK.acquire_write()
            NODE_IS_RUNNING = False
            NODE_LOCK.release_write()
            return -1
        return 1

    def cancel(self, restart=True):
        try:
            os.kill(self.process.pid, signal.SIGSTOP)
            self.nstdproc.stop()
        except Exception as e:
            print(e)
        try:
            self.nstdproc.stop()
        except Exception as e:
            print(e)
        if restart:
            self.init_process()

    def clear(self):
        self.cancel()


npm = Npm()
node = Node()
memName = node.vs.SHARED_MEMORY_NAME

#When python exists please do the following
@atexit.register
def onEnd():
    global memName
    global node

    #Clean up everything.... Include shm file and mmap stuff.
    try:
        if hasattr(node, 'process'):
            os.kill(node.process.pid, signal.SIGSTOP)
    except:
        print("Could not kill process. May already be dead.")
    try:
        if hasattr(node, 'nstdproc'):
            node.nstdproc.stop()
    except:
        print("Could not stop nstdproc. May already be dead.")

    posix_ipc.unlink_shared_memory(memName)
    print("Memory map has been destroyed")




import fcntl, struct, termios, os
import subprocess
import select
import msgpack
import pty

from io import BufferedWriter
from threading import Thread
from typing import Optional, TYPE_CHECKING

from minindn.play.term.cbuf import CircularByteBuffer
from minindn.play.consts import WSKeys, WSFunctions

if TYPE_CHECKING:
    from minindn.play.term.term import TermExecutor

class PtyManager:
    ptys = {}
    thread: Thread
    executor: 'TermExecutor'
    poller = select.poll()

    def __init__(self, executor):
        self.executor = executor
        self.thread = Thread(target=self.ui_out_pty_thread, args=(), daemon=True)
        self.thread.start()

    def register(self, pty: 'Pty'):
        self.ptys[pty.master] = pty
        self.poller.register(pty.master, select.POLLIN)

    def unregister(self, pty: 'Pty'):
        self.poller.unregister(pty.master)
        pty.cleanup()
        if pty.master in self.ptys:
            del self.ptys[pty.master]

    # Send output to UI thread
    def ui_out_pty_thread(self):
        while True:
            # Check all processes
            for pty in list(self.ptys.values()):
                if pty.process is not None and pty.process.poll() is not None:
                    self.unregister(pty)
                    continue

            # Check all file descriptors and block for 1/4 s
            for (fd, status) in self.poller.poll(250):
                if fd not in self.ptys:
                    self.poller.unregister(fd)
                    continue
                pty = self.ptys[fd]

                # Check if poller is closed
                if status == select.POLLHUP:
                    self.unregister(pty)
                    continue

                # Find the number of bytes available to read
                bytes_available = fcntl.ioctl(pty.master, termios.FIONREAD, struct.pack('I', 0))
                bytes_available = struct.unpack('I', bytes_available)[0]
                bytes_to_read = min(bytes_available, 4096)

                # This should never really happen
                if bytes_to_read == 0:
                    continue

                # Read everything available and send to UI
                try:
                    bytes = os.read(pty.master, bytes_to_read)
                    self.executor._send_pty_out(bytes, pty.id)
                    pty.buffer.write(bytes)
                except Exception as e:
                    print(e)
                    self.unregister(pty)
                    continue

class Pty:
    id: str
    name: str
    master: int
    slave: int
    stdin: BufferedWriter
    buffer: CircularByteBuffer
    executor: 'TermExecutor'
    process: Optional[subprocess.Popen] = None

    def __init__(self, executor, id: str, name: str):
        self.master, self.slave = pty.openpty()
        self.buffer = CircularByteBuffer(16000)
        self.executor = executor
        self.id = id
        self.name = name
        self.stdin = os.fdopen(self.master, 'wb')
        executor.pty_list[id] = self

    def cleanup(self):
        self.executor.socket.send_all(msgpack.dumps({
            WSKeys.MSG_KEY_FUN: WSFunctions.CLOSE_TERMINAL,
            WSKeys.MSG_KEY_ID: self.id,
        }))

        try:
            os.close(self.master)
            os.close(self.slave)
        except OSError:
            pass

        if self.id in self.executor.pty_list:
            del self.executor.pty_list[self.id]

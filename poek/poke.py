#!/usr/bin/env python3
import argparse
import errno
import os
import pwnlib.util.misc
import pwnlib.term
import pwnlib.term.text
import pwnlib.util.net
import select
import socket
import struct
import sys
import tarfile
import tempfile
import time
import traceback

PORT = 1337

parser = argparse.ArgumentParser(
    description = "Poke",
)

parser.add_argument(
    '--verbose', '-v',
    action='store_true',
    help='Verbose output',
    )

parser.add_argument(
    '--port', '-p',
    type=int,
    default=PORT,
    help='Port to listen on for file list requests',
    )

parser.add_argument(
    '--watch', '-w',
    metavar='<dir>',
    help='Watch directory and serve everything added to it',
    )

parser.add_argument(
    'paths',
    metavar='<path>',
    nargs='+',
    )

args = parser.parse_args()

def _log(emblem, msg):
    t = time.strftime('%T', time.localtime())
    print('%s %s %s' % \
        (pwnlib.term.text.magenta(t),
         emblem, msg), file=sys.stderr)

def debug(s):
    if args.verbose:
        _log(pwnlib.term.text.cyan('D'), s)

def info(s):
    _log(pwnlib.term.text.blue('I'), s)

def warn(s):
    _log(pwnlib.term.text.yellow('W'), s)

def err(s):
    _log(pwnlib.term.text.red('E'), s)

class EventLoop:
    def __init__(self):
        self.rfds = {}
        self.wfds = {}

    def watch_read(self, fd, cb):
        self.rfds[fd] = cb

    def watch_write(self, fd, cb):
        self.wfds[fd] = cb

    def unwatch_read(self, fd):
        if fd in self.rfds:
            del self.rfds[fd]

    def unwatch_write(self, fd):
        if fd in self.wfds:
            del self.wfds[fd]

    def unwatch(self, fd):
        self.unwatch_read(fd)
        self.unwatch_write(fd)

    def run(self):
        while True:
            try:
                self._loop()
            except KeyboardInterrupt:
                info('Interrupted')
                break
            except Exception as e:
                err('An exception occurred: %r' % e)
                for line in traceback.format_exc().splitlines():
                    debug(line)

    def _loop(self):
        rfds = self.rfds.keys()
        wfds = self.wfds.keys()
        try:
            rfds, wfds, _ = select.select(rfds, wfds, [], 1)
        except select.error as e:
            if e[0] != errno.EINTR:
                raise
            return
        for fd in rfds:
            self.rfds[fd](fd)
        for fd in wfds:
            self.wfds[fd](fd)

event_loop = EventLoop()

class Selectable:
    def fileno(self):
        return self.sock.fileno()
    def watch_read(self, cb):
        event_loop.watch_read(self, cb)
    def watch_write(self, cb):
        event_loop.watch_write(self, cb)
    def unwatch_read(self):
        event_loop.unwatch_read(self)
    def unwatch_write(self):
        event_loop.unwatch_write(self)
    def unwatch(self):
        event_loop.unwatch(self)

class TCPConnect(Selectable):
    def __init__(self, addr, port):
        self.sock = socket.socket(socket.AF_INET,
                                  socket.SOCK_STREAM)
        self.sock.setblocking(False)
        assert errno.EINPROGRESS == self.sock.connect_ex((addr, port))
        def cb(self):
            self.unwatch()
            if 0 == self.sock.connect_ex((self.addr, self.port)):
                self.on_connected()
            else:
                self.on_refused()
        self.watch_write(cb)
        self.addr = addr
        self.port = port

def bind_first_free(sock, port):
    while True:
        try:
            sock.bind(('', port))
        except socket.error as e:
            if e.errno == errno.EADDRINUSE:
                port += 1
                continue
            raise
        break

class TCPListen(Selectable):
    def __init__(self, port = 0, backlog = 10):
        self.sock = socket.socket(socket.AF_INET,
                                  socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setblocking(False)
        bind_first_free(self.sock, port)
        self.port = self.sock.getsockname()[1]
        self.sock.listen(backlog)
        def cb(self):
            try:
                sock, (addr, _) = self.sock.accept()
            except socket.error as e:
                if e.errno == errno.EWOULDBLOCK:
                    return
                raise
            self.on_connection(sock, addr)
        self.watch_read(cb)

class UDPListen(Selectable):
    def __init__(self, port = 0):
        self.sock = socket.socket(socket.AF_INET,
                                  socket.SOCK_DGRAM,
                                  socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.sock.setblocking(False)
        bind_first_free(self.sock, port)
        self.port = self.sock.getsockname()[1]
        def cb(self):
            data, (addr, _) = self.sock.recvfrom(4096)
            self.on_data(data, addr)
        self.watch_read(cb)

self_addrs = []
for _iface, addrs in pwnlib.util.net.interfaces4().items():
    self_addrs += addrs
items = []

class Directory(TCPConnect):
    def __init__(self, addr, port):
        TCPConnect.__init__(self, addr, port)
    def on_connected(self):
        debug('Connected to %s:%d, sending file list' % \
              (self.addr, self.port))
        for i in items:
            self.sock.send(struct.pack('!H', i.port) + \
                           i.path.encode() + b'\x00')
        self.sock.send(b'\x00\x00')
        self.sock.close()
    def on_refused(self):
        warn('%s refused connection on port %d' % \
             (self.addr, self.port))

class PeekHandler(UDPListen):
    def __init__(self, port):
        UDPListen.__init__(self, port)
        info('Listening on port %d' % self.port)
    def on_data(self, data, addr):
        if len(data) != 8 or data[:6] != b'POKEME':
            debug('Ignoring bogus request from %s' % addr)
            return
        if addr in self_addrs:
            debug('Ignoring request from self (%s)' % addr)
            return
        port = struct.unpack('!H', data[6:8])[0]
        debug('%s wants file list' % addr)
        Directory(addr, port)

# Compatibility with newer versions of pwntools
# https://github.com/Gallopsled/pwntools/pull/2242
def delete_cell_compat(cell):
    if hasattr(cell, 'delete'):
        cell.delete()

class Transfer(Selectable):
    def __init__(self, sock, addr, item):
        self.numb = 0
        self.sock = sock
        self.addr = addr
        self.path = item.path
        self.start = time.time()
        self.last_update = 0
        try:
            if self.path[-1] == '/':
                self.fd = tempfile.TemporaryFile(prefix='poke')
                name = os.path.basename(self.path[:-1])
                tar = tarfile.open(fileobj=self.fd, mode='w')
                tar.add(self.path, arcname=name)
                tar.close()
                self.fd.seek(0)
            else:
                self.fd = open(self.path, 'rb')
        except IOError as e:
            if e.errno == errno.ENOENT:
                warn('Could not open "%s" for reading' % self.path)
                return
            raise
        prefix = pwnlib.term.text.magenta('[Active]') + \
                 pwnlib.term.text.blue(' I ') + \
                 '"%s" => %s (' % (self.path, self.addr)
        self.h_prefix = pwnlib.term.output(prefix, float=True)
        self.h_progress = pwnlib.term.output('', float=True)
        self.h_suffix = pwnlib.term.output(')\n', float=True)
        def cb(self):
            # time.sleep(0.1)
            # data = self.fd.read(200)
            data = self.fd.read(4096)
            if data:
                try:
                    self.sock.sendall(data)
                except socket.error as e:
                    if e.errno in (errno.ECONNRESET, errno.EPIPE):
                        self.finish()
                        warn('%s closed connection' % self.addr)
                        return
                    raise
                self.numb += len(data)
                self.update()
            else:
                self.finish()
                info('"%s" => %s completed' % (self.path, self.addr))
        self.watch_write(cb)
    def finish(self):
        delete_cell_compat(self.h_prefix)
        delete_cell_compat(self.h_progress)
        delete_cell_compat(self.h_suffix)
        self.sock.close()
        self.unwatch()
    def update(self):
        now = time.time()
        if now - self.last_update > 0.1:
            bps = self.numb / (now - self.start)
            progress = '%s, %s/s' % \
                       (pwnlib.util.misc.size(self.numb),
                        pwnlib.util.misc.size(bps))
            self.h_progress.update(progress)
            self.last_update = now

class Item(TCPListen):
    def __init__(self, path, port=0):
        if not os.path.isdir(path) and not os.path.isfile(path):
            raise IOError('No such file or directory: %s' % path)
        path = path.rstrip('/')
        if os.path.isdir(path):
            path += '/'
        self.path = path
        TCPListen.__init__(self, port)
        items.append(self)
        info('Port %5d: "%s"' % (self.port, self.path))
    def __str__(self):
        return self.path
    def on_connection(self, sock, addr):
        info('%s wants "%s"' % \
             (addr, self.path))
        Transfer(sock, addr, self)

def main():
    pwnlib.term.init()
    # Compatibility with newer versions of pwntools
    # https://github.com/Gallopsled/pwntools/pull/2242
    if hasattr(pwnlib.term.term, "setup_done") and not pwnlib.term.term.setup_done:
        pwnlib.term.term.setupterm()

    PeekHandler(args.port)
    for p in args.paths:
        try:
            Item(p, port=args.port)
        except IOError as e:
            err(e)

    event_loop.run()

if __name__ == '__main__':
    main()

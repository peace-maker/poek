#!/usr/bin/env python3
import select
import errno
import socket
import struct
import sys
import os
import argparse
import time
import traceback
import pwnlib
import pwnlib.util.misc
import pwnlib.term
import pwnlib.ui
import tempfile
import tarfile

PORT = 1337

parser = argparse.ArgumentParser(
    description = "Peek",
)

parser.add_argument(
    '--verbose', '-v',
    action='store_true',
    help='Verbose output',
    )

parser.add_argument(
    '--port',
    type=int,
    default=PORT,
    help='Port to request file list on',
    )

parser.add_argument(
    'host',
    metavar='<host>',
    default='<broadcast>',
    nargs='?',
    help='The host to fetch files from (default: broadcast)',
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

lsock = None
lport = 0

def request_file_list():
    addr = args.host
    port = args.port
    sock = socket.socket(socket.AF_INET,
                         socket.SOCK_DGRAM,
                         socket.IPPROTO_UDP)
    sock.bind(('', 0))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    data = b'POKEME' + struct.pack('!H', lport)
    sock.sendto(data, (addr, port))

def recvn(sock, n):
    data = b''
    while len(data) < n:
        data += sock.recv(n - len(data))
    return data

def recvuntil(sock, stop, drop=True):
    data = b''
    while not data.endswith(stop):
        data += sock.recv(1)
    if drop:
        data = data[:-len(stop)]
    return data

files = []
cur_file = 0
do_quit = False
quit_h = None
files_h = []
transfers = []

# Compatibility with newer versions of pwntools
# https://github.com/Gallopsled/pwntools/pull/2242
def delete_cell_compat(cell):
    if hasattr(cell, 'delete'):
        cell.delete()

class Transfer:
    def __init__(self, f):
        addr, port, path = f
        isdir = path[-1] == '/'
        path = path.rstrip('/')
        base, ext = os.path.splitext(os.path.basename(path))
        name = base + ext
        if os.path.exists(name):
            overwrite = pwnlib.ui.yesno('File exists; overwrite?', False)
            if not overwrite:
                n = 1
                while os.path.exists(name):
                    name = '%s.%d%s' % (base, n, ext)
                    n += 1
        if isdir:
            name += '/'
        self.name = name

        if isdir:
            self.fd = tempfile.TemporaryFile(prefix='peek')
        else:
            try:
                self.fd = open(name, 'wb')
            except IOError:
                err('Could not open "%s" for writing' % name)
                return

        self.addr = addr
        self.port = port
        self.sock = socket.socket(socket.AF_INET,
                                  socket.SOCK_STREAM)
        try:
            self.sock.connect((addr, port))
        except socket.error as e:
            if e.errno == errno.ECONNREFUSED:
                warn('%s refused connection on port %d' % (addr, port))
                return
            raise
        self.sock.setblocking(False)

        self.numb = 0
        self.start = time.time()
        self.last_update = 0
        prefix = pwnlib.term.text.magenta('[Active]') + \
                 pwnlib.term.text.blue(' I ') + \
                 '"%s" <= %s (' % (path, addr)
        self.h_prefix = pwnlib.term.output(prefix, float=True, priority=5)
        self.h_progress = pwnlib.term.output('', float=True, priority=5)
        self.h_suffix = pwnlib.term.output(')\n', float=True, priority=5)
        transfers.append(self)

    def fileno(self):
        return self.sock.fileno()

    def process(self):
        data = self.sock.recv(4096)
        if not data:
            self.finish()
            info('"%s" <= %s completed (%s)' % (self.name, self.addr, pwnlib.util.misc.size(self.numb)))
            return
        self.numb += len(data)
        self.fd.write(data)
        self.update()

    def finish(self):
        delete_cell_compat(self.h_prefix)
        delete_cell_compat(self.h_progress)
        delete_cell_compat(self.h_suffix)
        self.sock.close()
        if self.name[-1] == '/':
            self.fd.flush()
            self.fd.seek(0)
            tar = tarfile.open(fileobj=self.fd, mode='r')
            tar.extractall()
            tar.close()
        else:
            self.fd.close()
        transfers.remove(self)

    def update(self):
        now = time.time()
        if now - self.last_update > 0.1:
            bps = self.numb / (now - self.start)
            progress = '%s, %s/s' % \
                       (pwnlib.util.misc.size(self.numb),
                        pwnlib.util.misc.size(bps))
            self.h_progress.update(progress)
            self.last_update = now

    def cancel(self):
        self.finish()
        warn('"%s" <= %s canceled' % (self.path, self.addr))

def fmt_file(f, selected=False):
    addr, port, path = f
    host = pwnlib.term.text.magenta('%15s:%-5d ' % (addr, port))
    if selected:
        path = pwnlib.term.text.reverse(path)
    return host + path + '\n'

def finish():
    delete_cell_compat(quit_h)
    for h, f in zip(files_h, files):
        h.update(fmt_file(f))
    for t in transfers:
        t.sock.close()

def loop():
    global cur_file, do_quit, last_request
    try:
        rfds, _, _ = select.select([sys.stdin, lsock] + list(transfers),
                                   [], [], 1)
    except select.error as e:
        if e[0] != errno.EINTR:
            raise
        return

    for fd in rfds:
        if isinstance(fd, Transfer):
            fd.process()

    if lsock in rfds:
        sock, (addr, _) = lsock.accept()
        debug('Receiving file list from %s' % addr)
        while True:
            port = struct.unpack('!H', recvn(sock, 2))[0]
            if port == 0:
                sock.close()
                break
            path = recvuntil(sock, b'\x00', drop=True).decode()
            f = (addr, port, path)
            if f not in files:
                h = pwnlib.term.output(fmt_file(f, not files), float=True)
                files.append(f)
                files_h.append(h)

    if sys.stdin in rfds:
        k = pwnlib.term.key.get()
        if not k == 'q':
            do_quit = False
            quit_h.update('')

        if k == 'r':
            info('Requesting files')
            request_file_list()
            last_request = time.time()
        elif k == '<up>':
            if cur_file > 0:
                files_h[cur_file].update(fmt_file(files[cur_file]))
                cur_file -= 1
                files_h[cur_file].update(fmt_file(files[cur_file],
                                                  selected=True))
        elif k == '<down>':
            if cur_file < len(files) - 1:
                files_h[cur_file].update(fmt_file(files[cur_file]))
                cur_file += 1
                files_h[cur_file].update(fmt_file(files[cur_file],
                                                  selected=True))

        elif k in ('<space>', '<enter>') and files:
            Transfer(files[cur_file])

        elif k == 'q':
            if transfers and not do_quit:
                msg = 'Active transfers; type `q` again to force exit'
                # quit_h.update('  ' + pwnlib.term.text.on_red(msg))
                quit_h.update('  ' + pwnlib.term.text.bold_yellow(msg))
                do_quit = True
            else:
                finish()
                sys.exit()

        elif k == 'a':
            for f in files:
                Transfer(f)

        elif k in ('h', '?', '<f1>'):
            keys = (('q', 'Quit'),
                    ('r', 'Refresh'),
                    ('a', 'Get all'),
                    ('SPC', 'Download'),
                    )
            help = []
            for key, desc in keys:
                help.append('%s%s%s' % \
                            (pwnlib.term.text.bold_green(key),
                             pwnlib.term.text.magenta(':'),
                             desc,
                             ))
            sep = pwnlib.term.text.magenta(', ')
            info(sep.join(help))

def main():
    global lsock, lport, quit_h
    lsock = socket.socket(socket.AF_INET,
                      socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.setblocking(False)
    lsock.bind(('', 0))
    lport = lsock.getsockname()[1]
    lsock.listen(10)

    pwnlib.term.init()
    # Compatibility with newer versions of pwntools
    # https://github.com/Gallopsled/pwntools/pull/2242
    if hasattr(pwnlib.term.term, "setup_done") and not pwnlib.term.term.setup_done:
        pwnlib.term.term.setupterm()
    quit_h = pwnlib.term.output('', float=True, priority=15)
    request_interval = 5
    last_request = 0
    while True:
        now = time.time()
        if now - last_request > request_interval:
            request_file_list()
            last_request = now
        try:
            loop()
        except KeyboardInterrupt:
            if transfers:
                transfers[-1].cancel()
            else:
                finish()
                info('Interrupted')
                break
        except Exception as e:
            raise
            err('An exception occurred: %r' % e)
            for line in traceback.format_exc().splitlines():
                debug(line)

if __name__ == '__main__':
    main()

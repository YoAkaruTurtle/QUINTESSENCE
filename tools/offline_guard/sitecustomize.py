"""Test-process guard, inherited by Python children through PYTHONPATH.

This is an accidental-access regression guard, not a hostile-code sandbox.
"""
import ipaddress
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(os.environ['QUINTESSENCE_TEST_OUTPUT']).resolve()
if OUTPUT == ROOT or ROOT in OUTPUT.parents or OUTPUT in ROOT.parents:
    raise RuntimeError('test output and checkout must be disjoint directory trees')

def within(path, root):
    return path == root or root in path.parents

def check_path(value, writing=False):
    if isinstance(value, int) or value is None:
        return
    value = os.fsdecode(value)
    if value == ':memory:' or value.lower() == os.devnull.lower():
        return
    path = Path(value).resolve()
    # Anything adjacent to this checkout is outside the test input boundary,
    # except the explicitly selected external output area.
    read_roots = (ROOT, OUTPUT, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
    if not writing and not any(within(path, root) for root in read_roots):
        raise PermissionError('test access outside code/output boundary')
    if writing and (within(path, ROOT) or not within(path, OUTPUT)):
        raise PermissionError('test writes must stay in external output directory')

def audit(event, args):
    if event == 'open':
        mode = args[1] or ''
        flags = args[2] or 0
        check_path(args[0], any(x in mode for x in 'wax+') or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)))
    elif event == 'sqlite3.connect':
        check_path(args[0], True)
    elif event in ('os.mkdir', 'os.remove', 'os.rmdir'):
        check_path(args[0], True)
    elif event in ('os.rename',):
        check_path(args[0], True); check_path(args[1], True)
    elif event in ('socket.connect', 'socket.bind', 'socket.sendto'):
        address = args[-1]
        if not isinstance(address, tuple) or not loopback(address[0]):
            raise PermissionError('only loopback sockets permitted for offline tests')

def loopback(host):
    if host == 'localhost': return True
    try: return ipaddress.ip_address(host).is_loopback
    except ValueError: return False

original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex
original_resolve = socket.getaddrinfo
def connect(self, address):
    if not isinstance(address, tuple) or not loopback(address[0]):
        raise PermissionError('non-loopback network disabled for offline tests')
    return original_connect(self, address)
def connect_ex(self, address):
    if not isinstance(address, tuple) or not loopback(address[0]):
        raise PermissionError('non-loopback network disabled for offline tests')
    return original_connect_ex(self, address)
def resolve(host, *args, **kwargs):
    if not loopback(host): raise PermissionError('external DNS disabled for offline tests')
    return original_resolve(host, *args, **kwargs)
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = resolve
sys.addaudithook(audit)
sys._quintessence_offline_guard = True

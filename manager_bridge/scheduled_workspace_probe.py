"""Private one-shot workspace continuity evidence; never execution authority.

The owner retains the challenge hash outside the scheduled prompt. Access to this
same-user workspace can forge receipts: this is not remote attestation.
"""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import stat
import time
from .host_preflight import _root

LIMIT = 4096


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _read(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        s = os.fstat(stream.fileno())
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid()
                or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1 or s.st_size > LIMIT):
            raise ValueError('private bounded regular probe file required')
        raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('probe file exceeds bound')
    return raw


def _write(directory, name, value):
    raw = _encode(value)
    if len(raw) > LIMIT:
        raise ValueError('probe exceeds bound')
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory)


def _open(root):
    p = _root(root)
    fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    s = os.fstat(fd)
    if (s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700
            or (s.st_dev, s.st_ino) != (p.stat().st_dev, p.stat().st_ino)):
        os.close(fd)
        raise ValueError('private directory changed')
    return fd


def _clock(now):
    if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        raise ValueError('finite positive time required')
    return now


def prepare(root, *, lifetime_seconds=300):
    if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= 3600:
        raise ValueError('bounded probe lifetime required')
    now = _clock(time.time())
    value = {'version': 1, 'challenge': secrets.token_hex(16),
             'nonce': secrets.token_hex(32), 'created_at': now,
             'expires_at': now + lifetime_seconds}
    fd = _open(root)
    try:
        _write(fd, 'challenge.json', value)
    finally:
        os.close(fd)
    return {'challenge_sha256': hashlib.sha256(_encode(value)).hexdigest(),
            'execution_authorized': False}


def _challenge(fd, expected_hash, now):
    if type(expected_hash) is not str or len(expected_hash) != 64:
        raise ValueError('owner-retained challenge hash required')
    raw = _read(fd, 'challenge.json')
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_hash):
        raise ValueError('challenge replaced')
    value = json.loads(raw)
    if set(value) != {'version', 'challenge', 'nonce', 'created_at', 'expires_at'} or value['version'] != 1:
        raise ValueError('exact probe schema required')
    start, end = _clock(value['created_at']), _clock(value['expires_at'])
    if not start <= now < end or not 0 < end - start <= 3600:
        raise ValueError('probe expired or clock moved backwards')
    return value


def _signature(challenge, body):
    return hmac.new(bytes.fromhex(challenge['nonce']), _encode(body), hashlib.sha256).hexdigest()


def respond(root, *, expected_hash):
    now = _clock(time.time())
    fd = _open(root)
    try:
        challenge = _challenge(fd, expected_hash, now)
        body = {'version': 1, 'challenge_sha256': expected_hash, 'observed_at': now,
                'pid': os.getpid(), 'boot_sha256': hashlib.sha256(
                    Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()}
        _write(fd, 'receipt.json', dict(body, hmac_sha256=_signature(challenge, body)))
    finally:
        os.close(fd)
    return {'receipt_created': True, 'execution_authorized': False}


def verify(root, *, expected_hash):
    now = _clock(time.time())
    fd = _open(root)
    try:
        challenge = _challenge(fd, expected_hash, now)
        receipt = json.loads(_read(fd, 'receipt.json'))
        if set(receipt) != {'version', 'challenge_sha256', 'observed_at', 'pid', 'boot_sha256', 'hmac_sha256'}:
            raise ValueError('exact receipt schema required')
        signature = receipt.pop('hmac_sha256')
        if type(signature) is not str or not hmac.compare_digest(signature, _signature(challenge, receipt)):
            raise ValueError('receipt integrity mismatch')
        if (receipt['version'] != 1 or receipt['challenge_sha256'] != expected_hash
                or not challenge['created_at'] <= _clock(receipt['observed_at']) <= now
                or type(receipt['pid']) is not int or receipt['pid'] <= 0):
            raise ValueError('receipt binding or time mismatch')
    finally:
        os.close(fd)
    return {'workspace_read_write_verified': True, 'scope': 'private_workspace_continuity_only',
            'scheduler_run_verified': False, 'always_on_verified': False,
            'cold_boot_verified': False, 'authentication_verified': False,
            'execution_authorized': False}

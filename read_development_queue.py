"""Read or complete Taesan development requests with a one-time signature."""
import argparse
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ORIGIN = 'https://bellavita-taesan-test.onrender.com'
PATH = '/development-queue'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def signature(key, method, body=b''):
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    payload = f'{method}\n{PATH}\n{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}'
    return f'Taesan-Signature {timestamp}.{nonce}.{key.sign(payload.encode()).hex()}'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('key_file', type=Path)
    parser.add_argument('--complete', nargs=2, metavar=('JOB', 'PR_URL'))
    args = parser.parse_args()
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(args.key_file.read_text().strip()))
    method = 'POST' if args.complete else 'GET'
    body = urlencode(dict(zip(('job', 'result'), args.complete))).encode() if args.complete else b''
    request = Request(ORIGIN + PATH, data=body if args.complete else None, method=method,
                      headers={'Authorization': signature(key, method, body)})
    with build_opener(NoRedirect).open(request, timeout=15) as response:
        print(response.read().decode())


if __name__ == '__main__':
    main()

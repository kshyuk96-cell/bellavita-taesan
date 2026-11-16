"""One unauthenticated, bounded GET; pinned validated address, no redirects or actions."""
import hashlib
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit
from datetime import datetime,timezone
from .handoff_intake import target_url


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,port,address):super().__init__(host,port=port,timeout=10,context=ssl.create_default_context());self.address=address
    def connect(self):self.sock=self._context.wrap_socket(socket.create_connection((self.address,self.port),timeout=self.timeout),server_hostname=self.host)

class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self,host,port,address):super().__init__(host,port=port,timeout=10);self.address=address
    def connect(self):self.sock=socket.create_connection((self.address,self.port),timeout=self.timeout)


def validate_addresses(addresses,*,local_fixture=False):
    if not addresses:raise ValueError('no destination address')
    for value in addresses:
        address=ipaddress.ip_address(value)
        if not address.is_global and not (local_fixture and address.is_loopback):raise ValueError('private/link-local destination not allowed')
    return addresses[0]


def probe_url(url,*,local_fixture=False):
    parsed=urlsplit(target_url(url));port=parsed.port or (443 if parsed.scheme=='https' else 80)
    try:
        addresses=sorted({r[4][0] for r in socket.getaddrinfo(parsed.hostname,port,type=socket.SOCK_STREAM)})
        selected=validate_addresses(addresses,local_fixture=local_fixture)
        cls=PinnedHTTPS if parsed.scheme=='https' else PinnedHTTP
        connection=cls(parsed.hostname,port,selected)
        try:
            connection.request('GET',parsed.path or '/',headers={'Host':parsed.netloc,'User-Agent':'TaesanHandoffReadOnly/1','Accept-Encoding':'identity'})
            response=connection.getresponse();data=response.read(1_000_001)
            if len(data)>1_000_000:raise ValueError('response limit')
            status=response.status
        finally:connection.close()
        return dict(scope='unauthenticated_http_reachability_only',url=url,status=status,observed_at=datetime.now(timezone.utc).isoformat(),response_bytes=len(data),response_sha256=hashlib.sha256(data).hexdigest(),redirect_followed=False,authenticated=False,source_revision_verified=False,ui_executed=False,verdict='reachable' if 200<=status<300 else 'redirect_not_followed' if 300<=status<400 else 'http_error')
    except (OSError,ValueError,http.client.HTTPException):return dict(scope='unauthenticated_http_reachability_only',url=url,verdict='unverified',retry_performed=False,source_revision_verified=False,ui_executed=False)

"""Read-only support for Charmcard's public legacy HTTP/CP949 wedding cards."""
import http.client
import ipaddress
import re
import socket
from urllib.parse import urljoin,urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler,Request,build_opener,getproxies
from lxml import html

HOSTS=frozenset({'charmcard.com','www.charmcard.com'})
CARD_PATH=re.compile(r'/m/[A-Za-z0-9_-]{1,100}/index\.html')
IMAGE_PATH=re.compile(r'/m/[A-Za-z0-9_-]{1,100}/img2/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,160}\.(?:jpe?g|png|webp)',re.I)

def _public_url(value,path_pattern):
    try:
        parsed=urlsplit(value)
        return (parsed.scheme in ('http','https') and parsed.netloc in HOSTS and not parsed.query
                and not parsed.fragment and path_pattern.fullmatch(parsed.path) is not None)
    except (TypeError,ValueError):return False

def is_card_url(value):return _public_url(value,CARD_PATH)
def is_image_url(value):return _public_url(value,IMAGE_PATH)
def canonical_url(value):
    if not is_card_url(value):raise ValueError('참카드 청첩장 주소를 확인하세요.')
    return 'http://charmcard.com'+urlsplit(value).path

class PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        addresses=list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(self.host,self.port,type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):raise ValueError('공개 참카드 주소만 읽을 수 있습니다.')
        self.sock=socket.create_connection((addresses[0],self.port),self.timeout)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def _read(response,image):
    if response.status!=200:raise ValueError('참카드 원본 서버 응답 '+str(response.status))
    limit=5_000_000 if image else 2_000_000;raw=response.read(limit+1)
    if len(raw)>limit:raise ValueError('참카드 원본 용량을 확인하세요.')
    if image and not (response.getheader('Content-Type') or '').lower().startswith('image/'):raise ValueError('참카드 원본 사진 형식을 확인하세요.')
    return raw

def fetch(value,*,image=False):
    if not (is_image_url(value) if image else is_card_url(value)):raise ValueError('참카드 공개 원본 주소를 확인하세요.')
    parsed=urlsplit(value)
    # The provider serves these existing cards/images only over HTTP; its HTTPS
    # virtual host returns 404. Never downgrade any other host or follow redirects.
    headers={'User-Agent':'Mozilla/5.0 (compatible; Bellavita/1.0)','Accept':'image/*' if image else 'text/html'}
    if getproxies().get('http'):
        # Managed runtimes may require their configured egress proxy. This path
        # still accepts only the same fixed public provider and strict asset path.
        addresses=socket.getaddrinfo('charmcard.com',80,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):raise ValueError('공개 참카드 주소만 읽을 수 있습니다.')
        try:
            with build_opener(NoRedirect()).open(Request('http://charmcard.com'+parsed.path,headers=headers),timeout=10) as response:return _read(response,image)
        except HTTPError as error:raise ValueError('참카드 원본 서버 응답 '+str(error.code)) from None
    connection=PublicHTTPConnection('charmcard.com',80,timeout=10)
    try:
        connection.request('GET',parsed.path,headers=headers)
        return _read(connection.getresponse(),image)
    finally:connection.close()

def decode(raw):
    if not isinstance(raw,bytes):raise ValueError('참카드 원본 문서 형식을 확인하세요.')
    declared=re.search(br'charset\s*=\s*[\'"]?\s*([A-Za-z0-9_-]+)',raw[:5000],re.I)
    charset=declared[1].decode('ascii').lower() if declared else ''
    codecs=('utf-8-sig',) if raw.startswith(b'\xef\xbb\xbf') else ('cp949','utf-8') if charset in ('euc-kr','euckr','cp949','ks_c_5601-1987') else ('utf-8','cp949')
    for codec in codecs:
        try:return raw.decode(codec)
        except UnicodeDecodeError:continue
    raise ValueError('참카드 한글 문서 인코딩을 확인하세요.')

def fields(source,raw):
    source=canonical_url(source);tree=html.fromstring(decode(raw) if isinstance(raw,bytes) else raw)
    def text(node):return ' '.join(' '.join(node.itertext()).split())
    def by_class(name):return tree.xpath('//*[contains(concat(" ",normalize-space(@class)," "),$token)]',token=' '+name+' ')
    def content(name):
        nodes=by_class(name)
        return text(nodes[0]) if nodes else ''
    families=[]
    for side in ('groom','bride'):
        def person(class_name):
            nodes=by_class(class_name)
            matches=nodes[0].xpath('.//*[contains(concat(" ",normalize-space(@class)," ")," name ")]/span') if nodes else []
            return re.sub(r'^\(\s*故\s*\)\s*','故 ',text(matches[0])) if matches else ''
        parents=[person(side+'_'+role) for role in ('father','mother')]
        # This source contains no birth-order wording. Keep that field blank
        # rather than inferring 장남/장녀 from a name or the side of the card.
        families.append(('·'.join(value for value in parents if value),'',person(side)))
    photos=[];folder=urlsplit(source).path.rsplit('/',1)[0]+'/'
    for value in tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," swiper-slide ")]//img/@src|//*[contains(concat(" ",normalize-space(@class)," ")," top_pic ")]//img/@src'):
        url=urljoin(source,value)
        if is_image_url(url) and urlsplit(url).path.startswith(folder) and url not in photos:photos.append(url)
    return {'date':content('top_space_t1'),'venue':content('hall_name'),'address':content('hall_addr'),
            'families':families,'photos':photos[:20],'source':source,'greeting':content('con_box_t3'),
            'warnings':['원본에 장남·장녀 등의 관계가 없어 관계 항목만 직접 확인해 주세요.']}

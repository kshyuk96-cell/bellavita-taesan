"""Read-only order facts for published invitation events. No customer details leave Render."""
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

ROW_CAP = 3000
EVENT_CAP = 60
PAIR_PATH = re.compile(r'/i/([a-f0-9]{32})/(93500|99000)/order')
KEY = re.compile(r'[A-Za-z0-9_-]{1,120}')
SEOUL = ZoneInfo('Asia/Seoul')
EXPLICIT_CALENDAR = re.compile(r'(?<!\d)(20\d{2})\s*(?:년|[./-])\s*(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})(?!\d)')
START_FIELDS = ('entrance', 'entranceDate', 'funeralStartDate', 'funeralStartAt')
DEPARTURE_FIELDS = ('departure', 'departureDate', 'departureAt', 'funeralDepartureDate', 'funeralDepartureAt')


def read_events(root):
    # Unlike the public library query, this must never auto-register/rebuild.
    file=Path(root)/'erp-invitation-library'/'state.json'
    return json.loads(file.read_text(encoding='utf-8')) if file.is_file() else None


def url_key(value, *, path_only=False):
    if not isinstance(value,str) or len(value)>1500:return ''
    try:
        x=urlsplit(value)
        if x.scheme not in ('http','https') or not x.netloc or x.username or x.password or x.fragment:return ''
        return x.path if path_only else value
    except ValueError:return ''


def groups_from_catalog(catalog, origin):
    groups={};url_owners={};pair_owners={};origin_url=urlsplit(origin)
    for key,row in catalog.items():
        if not KEY.fullmatch(key) or not isinstance(row,dict):continue
        stable=row.get('invitationId')
        stable=stable if isinstance(stable,str) and KEY.fullmatch(stable) else ''
        event=('event:'+stable) if stable else ('publication:'+key)
        g=groups.setdefault(event,{'id':event,'versions':[],'urls':set(),'pairs':{},'facts':[],
                                    'unlinked':0,'external':False,'versionUnverified':not bool(stable)})
        g['versions'].append((key,row))
        source=url_key(row.get('sourceUrl'))
        if source:
            g['urls'].add(source)
            url_owners.setdefault(source,set()).add(event)
        for link in row.get('links') or []:
            if not isinstance(link,dict):continue
            for field in ('url','orderUrl'):
                url=url_key(link.get(field))
                if url:
                    g['urls'].add(url)
                    url_owners.setdefault(url,set()).add(event)
            order=url_key(link.get('orderUrl'))
            x=urlsplit(order) if order else None
            m=PAIR_PATH.fullmatch(x.path) if x and x.scheme=='https' and x.netloc==origin_url.netloc and not x.query else None
            if not m or (row.get('external') and not row.get('orderOnly')):
                g['external']=True
                continue
            pair=(m[1],int(m[2]))
            product=str(link.get('productId') or '')
            pair_owners.setdefault(pair,set()).add(event)
            if pair in g['pairs'] and product and g['pairs'][pair] and g['pairs'][pair]!=product:
                g['unlinked']+=1
                g['pairs'][pair]='!ambiguous'
            elif pair not in g['pairs'] or not g['pairs'][pair]:
                g['pairs'][pair]=product
    return groups,url_owners,pair_owners


def calendar_date(value):
    """Explicit YYYY-MM-DD, Korean full-year date or zoned timestamp; never infer years."""
    if not isinstance(value,str) or len(value)>240:
        return None
    raw=value.strip()
    if re.fullmatch(r'20\d\d-\d\d-\d\d',raw):
        try:return dt.date.fromisoformat(raw)
        except ValueError:return None
    try:
        if 'T' in raw and re.fullmatch(r'20\d\d-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?',raw):
            stamp=dt.datetime.fromisoformat(raw.replace('Z','+00:00'))
            return (stamp.astimezone(SEOUL) if stamp.tzinfo else stamp).date()
    except ValueError:
        return None
    matches=EXPLICIT_CALENDAR.findall(raw)
    if len(matches)!=1:return None
    try:return dt.date(*map(int,matches[0]))
    except ValueError:return None


def journal_dates(journal, keys):
    """Read immutable, exactly identified successful publish values without initializing a worker."""
    if not keys or not journal.is_file():
        return {}
    try:
        with sqlite3.connect(journal.resolve().as_uri()+'?mode=ro',uri=True,timeout=10) as c:
            c.row_factory=sqlite3.Row
            out={}
            q="""SELECT public_id,invitation_id,input FROM jobs
                 WHERE action='publish' AND state='done' AND public_id IN (SELECT value FROM json_each(?))
                 ORDER BY created_at DESC"""
            for r in c.execute(q,(json.dumps(keys),)):
                if r['public_id'] in out:continue
                try:
                    v=json.loads(r['input'])
                    if v.get('kind')=='memorial' and isinstance(v.get('values'),dict):
                        out[r['public_id']]=(r['invitation_id'],v['values'])
                except (AttributeError,ValueError,TypeError):
                    continue
            return out
    except (sqlite3.Error,OSError):
        return {}


def memorial_window(row, publication, today):
    # The obituary parser has explicit entrance (입실) and departure (발인);
    # admission is 입관 and deathDate is 별세일, neither is an entry date.
    saved_id,saved=publication or ('',{})
    stable=row.get('invitationId')
    if stable and saved_id and stable!=saved_id:
        saved={}
    inline=row.get('values') if isinstance(row.get('values'),dict) else {}
    def read(fields):
        values=[calendar_date(table[key]) for table in (saved,inline,row)
                for key in fields if table.get(key) not in ('',None)]
        days={value for value in values if value is not None}
        if len(days)>1 or (values and not days):return None,True
        return next(iter(days),None),False
    departure,dep_conflict=read(DEPARTURE_FIELDS)
    entrance,start_conflict=read(START_FIELDS)
    if start_conflict or dep_conflict or (entrance and departure and entrance>departure):
        return 'check',None
    if (entrance and entrance>today) or (departure and departure<today):
        return 'skip',None
    if departure is None:
        return 'check',None
    return 'include',departure


def query(root,origin,params,*,journal=None):
    from erp_invitation_order_edits import metadata
    wanted=set(filter(None,params.get('ids',[''])[0].split(',')))
    if len(wanted)>EVENT_CAP or any(not KEY.fullmatch(x) for x in wanted):raise ValueError('행사 범위를 확인하세요.')
    start,end=params.get('from',[''])[0],params.get('to',[''])[0]
    today=dt.date.fromisoformat(params['today'][0]) if params.get('today') else dt.datetime.now(SEOUL).date()
    if not wanted:
        a,b=dt.date.fromisoformat(start),dt.date.fromisoformat(end)
        if (b-a).days!=6 or a.weekday()!=0 or not a<=today<=b:raise ValueError('한국시간 기준 한 주를 선택하세요.')
    catalog=read_events(root)
    if not isinstance(catalog,dict):return {'events':[],'catalogUnavailable':True,'sourcesUnavailable':True}
    groups,urls,pairs=groups_from_catalog(catalog,origin)
    candidates=[]
    for g in groups.values():
        visible=[(key,row) for key,row in g['versions'] if not row.get('deleted')]
        if wanted:
            visible=[(key,row) for key,row in visible if key in wanted]
        if not visible:continue
        key,row=max(visible,key=lambda pair:str(pair[1].get('createdAt') or ''))
        candidates.append((g,key,row))
    journal=Path(journal) if journal is not None else Path(root)/'erp-invitations'/'journal.sqlite'
    schedules=journal_dates(journal,[key for _,key,row in candidates if row.get('kind')=='memorial'])
    selection=[];date_check=0
    for g,key,row in candidates:
        kind=row.get('kind','')
        day=calendar_date(row.get('date'))
        if kind=='memorial':
            decision,day=memorial_window(row,schedules.get(key),today)
            if not wanted and decision!='include':
                if decision=='check':date_check+=1
                continue
        elif not wanted:
            if day is None:
                date_check+=1;continue
            if not a<=day<=b:continue
        g.update(title=str(row.get('title') or '')[:180],date=day.isoformat() if day else '',kind=kind)
        selection.append(g)
    selection.sort(key=lambda g:(g['date'],g['id']))
    capped=len(selection)>EVENT_CAP;selection=selection[:EVENT_CAP]
    owned={}
    for g in selection:
        for pair,pid in g['pairs'].items():
            if len(pairs[pair])!=1 or pid=='!ambiguous':
                g['unlinked']+=1
            else:owned[pair]=(g,pid)
    file=Path(root)/'state.sqlite3'
    unavailable=bool(owned) and not file.is_file()
    row_capped=False
    if owned and not unavailable:
        with sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True,timeout=10) as c:
            c.row_factory=sqlite3.Row
            has_operations=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_operations'").fetchone()
            # Limit the result to the selected pairs and keep each receipt ID unique.
            payload=json.dumps([{'j':j,'p':p} for j,p in owned])
            source_legacy='(SELECT order_number FROM order_operations WHERE id=substr(i.id,1,8)||\'-\'||substr(i.id,9,4)||\'-\'||substr(i.id,13,4)||\'-\'||substr(i.id,17,4)||\'-\'||substr(i.id,21) LIMIT 1)' if has_operations else 'NULL'
            q=f"""SELECT i.id,i.job,i.price,i.state,i.details,{source_legacy} legacy
                   FROM invitation_orders i JOIN json_each(?) pair
                   ON i.job=json_extract(pair.value,'$.j') AND i.price=json_extract(pair.value,'$.p')
                   ORDER BY i.id LIMIT ?"""
            try:orders=c.execute(q,(payload,ROW_CAP+1)).fetchall()
            except sqlite3.OperationalError:orders=[];unavailable=True
        overrides=metadata(root)
        row_capped=len(orders)>ROW_CAP
        for row in orders[:ROW_CAP]:
            g,pid=owned[(row['job'],row['price'])]
            try:
                d=json.loads(row['details']); source_id='payapp:'+row['id']
                if not re.fullmatch(r'payapp:[a-f0-9]{32}',source_id) or (pid and str(d.get('product') or '')!=pid):
                    raise ValueError()
                options=[v for k,v in d.get('options',[]) if k=='상품 선택' and isinstance(v,str)]
                overlay=overrides.get(source_id) or {}
                g['facts'].append({'sourceId':source_id,'legacyOrderId':str(row['legacy'] or overlay.get('erp_order_id') or ''),
                                   'state':row['state'],'hidden':bool(overlay.get('hidden')),'quantity':1,
                                   'product':options[0][:160] if len(options)==1 else ''})
            except (ValueError,TypeError,AttributeError,KeyError):
                g['unlinked']+=1
    for g in selection:
        g['ambiguousUrls']=sorted(url for url in g['urls'] if len(urls[url])!=1)
        g['urls']=sorted(url for url in g['urls'] if len(urls[url])==1)
        g['itemIds']=[key for key,_ in g.pop('versions')]
        g.pop('pairs')
    return {'events':selection,'capped':capped or row_capped,'sourcesUnavailable':unavailable,
            'catalogUnavailable':False,'dateCheck':date_check}


def handle_read(handler,method,path,root,origin):
    # The bridge has already authenticated. Never initialize the Engine for
    # this route, whose cold-start registration can write catalog state.
    if method!='GET':
        body,status={'error':'조회만 허용됩니다.'},405
    else:
        try:
            params=parse_qs(urlsplit(getattr(handler,'path',path)).query)
            body,status=query(root,origin,params),200
        except (ValueError,TypeError,KeyError,sqlite3.Error,OSError):
            body,status={'error':'초대장 주문 집계 원본을 확인하세요.'},502
    raw=json.dumps(body,ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header('Content-Type','application/json; charset=utf-8')
    handler.send_header('Cache-Control','private, no-store')
    handler.send_header('Content-Length',str(len(raw)))
    handler.end_headers()
    if method!='HEAD':handler.wfile.write(raw)
    return True

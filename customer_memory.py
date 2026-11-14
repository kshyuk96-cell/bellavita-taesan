"""Confirmed customer defaults, distinct from the person/organization on the inscription.

No field other than sender is reused. History is evidence for review, never an identity binding.
"""
import json,re,sqlite3
from dataclasses import dataclass

@dataclass(frozen=True)
class SenderDecision:
    status: str
    sender: str
    source: str
    reason: str
    evidence: tuple = ()

class CustomerMemory:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS customer_profiles (id TEXT PRIMARY KEY, name TEXT, default_sender TEXT, default_source TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS customer_bindings (scope TEXT, external_id TEXT, customer_id TEXT, confirmed_by TEXT, PRIMARY KEY(scope,external_id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS sender_variants (id TEXT PRIMARY KEY, customer_id TEXT, sender TEXT, cue TEXT, confirmed_by TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS sender_evidence (customer_id TEXT, row_id TEXT, sender TEXT, evidence_type TEXT, PRIMARY KEY(customer_id,row_id,evidence_type))')
        self.db.execute('CREATE TABLE IF NOT EXISTS customer_order_rooms (room TEXT PRIMARY KEY, confirmed_by TEXT NOT NULL)')
        self.db.commit()
    def confirm_profile(self,customer_id,name,sender,confirmed_by):
        if not all(isinstance(v,str) and v.strip() for v in [customer_id,name,sender,confirmed_by]):raise ValueError('Verified profile fields required')
        self.db.execute('INSERT INTO customer_profiles VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,default_sender=excluded.default_sender,default_source=excluded.default_source',(customer_id,name,sender,confirmed_by));self.db.commit()
    def bind(self,scope,external_id,customer_id,confirmed_by):
        if not all(isinstance(v,str) and v.strip() for v in [scope,external_id,confirmed_by]):raise ValueError('Confirmed source binding required')
        if not self.db.execute('SELECT 1 FROM customer_profiles WHERE id=?',(customer_id,)).fetchone():raise ValueError('Unknown customer profile')
        previous=self.db.execute('SELECT customer_id FROM customer_bindings WHERE scope=? AND external_id=?',(scope,external_id)).fetchone()
        if previous and previous[0]!=customer_id:raise ValueError('Binding belongs to a different customer; review before replacing')
        self.db.execute('INSERT OR IGNORE INTO customer_bindings VALUES (?,?,?,?)',(scope,external_id,customer_id,confirmed_by));self.db.commit()
    def confirm_variant(self,variant_id,customer_id,sender,cue,confirmed_by):
        if not all(isinstance(v,str) and v.strip() for v in [variant_id,customer_id,sender,cue,confirmed_by]):raise ValueError('Confirmed variant required')
        if not self.db.execute('SELECT 1 FROM customer_profiles WHERE id=?',(customer_id,)).fetchone():raise ValueError('Unknown customer profile')
        prior=self.db.execute('SELECT customer_id FROM sender_variants WHERE id=?',(variant_id,)).fetchone()
        if prior and prior[0]!=customer_id:raise ValueError('Variant belongs to another customer')
        self.db.execute('INSERT INTO sender_variants VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET sender=excluded.sender,cue=excluded.cue,confirmed_by=excluded.confirmed_by',(variant_id,customer_id,sender,cue,confirmed_by));self.db.commit()
    def record_history(self,customer_id,row_id,sender):
        # Do not promote other people's inscriptions ordered by this customer into defaults.
        self.db.execute('INSERT OR IGNORE INTO sender_evidence VALUES (?,?,?,?)',(customer_id,str(row_id),sender,'sheet_history'));self.db.commit()
    def resolve(self,scope,external_id,*,current_text='',explicit_sender='',sender_fragments=(),selected_variant=''):
        # Explicit current-order text does not need an inferred identity.
        if explicit_sender.strip():return SenderDecision('auto',explicit_sender.strip(),'current_order','이번 주문에 명시한 보내는분')
        if sender_fragments:
            if not all(isinstance(v,str) and v.strip() for v in sender_fragments):raise ValueError('Sender fragments must be confirmed current-order inscription segments')
            return SenderDecision('auto','\n'.join(dict.fromkeys(v.strip() for v in sender_fragments)),'current_order_fragments','이번 주문의 직함 조각을 원문 순서로 보존')
        binding=self.db.execute('SELECT customer_id,confirmed_by FROM customer_bindings WHERE scope=? AND external_id=?',(scope,external_id)).fetchone()
        if not binding:return SenderDecision('review','','unbound_customer','이름만으로 고객을 연결하지 않음: 수집 방/발신자 연결 필요')
        customer_id=binding[0]
        profile=self.db.execute('SELECT name,default_sender,default_source FROM customer_profiles WHERE id=?',(customer_id,)).fetchone()
        if not profile:return SenderDecision('review','','missing_profile','고객 기본값 없음')
        variants=list(self.db.execute('SELECT id,sender,cue,confirmed_by FROM sender_variants WHERE customer_id=?',(customer_id,)))
        if selected_variant:
            matches=[row for row in variants if row[0]==selected_variant]
            if len(matches)!=1:return SenderDecision('review','','unknown_variant','현재 고객에게 확인된 직함 선택지가 아님')
            variant=matches[0]
            return SenderDecision('auto',variant[1],'confirmed_variant','이번 주문에서 지정한 직함', (variant[3],))
        # A URL's path, preview, or event guest's name is not an instruction about sender identity.
        context=re.sub(r'https?://[^\s]+','',current_text)
        if re.search(r'말고|아니고|제외|빼고|쓰지|넣지|하지|취소',context):
            return SenderDecision('review','','negative_instruction','제외·취소 지시가 있어 이번 문구 확인 필요')
        cues=[row for row in variants if row[2] in context]
        if len(cues)==1:return SenderDecision('auto',cues[0][1],'confirmed_context','이번 주문에서 확인된 직함 단서', (cues[0][3],))
        if len(cues)>1:return SenderDecision('review','','multiple_titles','직함 단서가 여러 개라 선택 필요')
        if re.search(r'말고|아니고|이번에는|이번엔|명의|직함|직책|변경|수정|지난번|저번처럼|평소랑.*다르게',context):
            return SenderDecision('review','','unresolved_override','기본값과 다른 지시의 구체적인 보내는분 확인 필요')
        if re.search(r'보내(?:시는|는)?\s*(?:분|사람)|회장|대표|군수|CFO|CEO|지회장|사무총장',context,re.I):
            return SenderDecision('review','','unparsed_sender_text','이번 대화의 새로운 명의/직함을 먼저 해석해야 함')
        return SenderDecision('auto',profile[1],'confirmed_default','링크만 있는 단골 주문에 사용자 확정 기본값 적용',(profile[2],binding[1]))

SENDER_LABEL=re.compile(r'(?m)^[ \t]*(?:보내시는\s*분|보내는\s*분|보내는\s*사람)\s*[:：][ \t]*(.+)$')

def enrich_order_input(message,memory,scope,external_id):
    """Adapter prepared for existing Taesan order_intake.extract(message).

    The source scope/id must be verified original customer identity, NOT the employee's
    Kakao channel user id. Link-only routing must be enabled only in an allowed order room.
    """
    explicit=SENDER_LABEL.search(message)
    decision=memory.resolve(scope,external_id,current_text=message,explicit_sender=explicit[1] if explicit else '')
    if decision.status!='auto':return message,decision
    if explicit:
        normalized=SENDER_LABEL.sub(lambda m:'보내는 분: '+m[1].strip(),message)
        return normalized,decision
    # Preserve approved multi-line titles in the single sender label accepted by current Taesan.
    sender=' / '.join(part.strip() for part in decision.sender.splitlines() if part.strip())
    return '주문 접수\n'+message.strip()+'\n보내는 분: '+sender,decision

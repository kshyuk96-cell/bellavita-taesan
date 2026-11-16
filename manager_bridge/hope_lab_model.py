"""Local reference application for test-adapter verification, not the customer service.

No provider/network calls. All money/card rows are synthetic simulator events.
Oracles live separately in taesan_core.hope_tour_qa.
"""
from decimal import Decimal, ROUND_FLOOR
import hashlib
from copy import deepcopy


class LabBlocked(Exception):
    pass


def _amount(value):
    if type(value) is not int or not 0<=value<=10**12:raise ValueError('bounded integer required')
    return value


def _id(prefix,key):return prefix+'-'+hashlib.sha256(key.encode()).hexdigest()[:24]


def validate(action,payload):
    fields={'signup':set(),'set_card':{'scenario'},'purchase':{'product','room','quantity'},
      'cancel_request':{'booking'},'cancel_review':{'refund'},'cancel_approve':{'refund'},
      'cancel_execute':{'refund'},'withdraw_request':set(),'withdraw_review':{'withdrawal'},
      'withdraw_approve':{'withdrawal'},'withdraw_execute':{'withdrawal'},
      'resolve':{'member','outcome'},'corporate_purchase':set()}
    if action not in fields or type(payload) is not dict or not set(payload)<=fields[action]:
        raise ValueError('fixed synthetic command fields required')
    if action=='set_card' and payload.get('scenario') not in ('success','decline','unknown'):
        raise ValueError('simulator scenario required')
    if action=='purchase':
        if payload.get('product') not in ('P01','P02','P03','P04'):raise ValueError('fixture product required')
        if type(payload.get('quantity',1)) is not int or not 1<=payload.get('quantity',1)<=2:
            raise ValueError('fixture quantity 1..2 required')
        if payload.get('room','standard') not in ('standard','inside','balcony'):raise ValueError('fixture room required')
    for key in ('booking','refund','withdrawal'):
        if key in payload and (type(payload[key]) is not str or not 1<=len(payload[key])<=100):raise ValueError('fixture reference required')
    if action=='resolve' and (payload.get('member') not in {f'U{i:02}' for i in range(1,11)} or payload.get('outcome') not in ('success','decline')):
        raise ValueError('fixed simulator reconciliation required')


def apply(state,contract,actor,action,payload,key):
    validate(action,payload)
    state=deepcopy(state);role,subject=actor['role'],actor['subject']
    if role=='unauthorized':raise PermissionError('fixture permission denied')
    roles={'signup':'member','set_card':'member','purchase':'member','cancel_request':'member',
      'cancel_review':'finance','cancel_approve':'owner','cancel_execute':'owner','withdraw_request':'member',
      'withdraw_review':'finance','withdraw_approve':'owner','withdraw_execute':'owner','resolve':'owner','corporate_purchase':'member'}
    if role!=roles[action]:raise PermissionError('fixture role denied')
    if action=='corporate_purchase':raise LabBlocked('corporate_policy_pending')
    member=state['members'].get(subject)
    def journal(uid,bucket,amount,reference,kind,reverses=None):
        state['journal'].append(dict(id=_id('j',key+':'+str(len(state['journal']))),member=uid,
            bucket=bucket,amount=amount,reference=reference,kind=kind,reverses=reverses,
            origin=state['members'][uid][{'DP':'principal_origin','BP':'bonus_origin','SP':'special_origin'}[bucket]]))
    def credit_initial(uid):
        m=state['members'][uid]
        if m['status']!='new' and m['status']!='pending_first_payment':raise LabBlocked('membership_already_active')
        monthly={'hope':50000,'happy':100000,'premium':200000}[m['grade']];fee=monthly//2
        ref=_id('initial',uid)
        state['cards'].append(dict(id=_id('card',key),member=uid,kind='initial',amount=monthly+fee,reference=ref,status='approved'))
        m.update(principal_origin=ref,bonus_origin=ref,status='active',principal=monthly,DP=monthly,BP=fee,recurring=True,next_due='2026-12-16',signup_fee=fee)
        journal(uid,'DP',monthly,ref,'monthly_principal');journal(uid,'BP',fee,ref,'signup_benefit')
        return dict(status='active',paid=monthly+fee,principal=monthly,signup_fee=fee,BP=fee)
    def purchase(uid,selection,request_key):
        m=state['members'][uid]
        if m['status']!='active':raise LabBlocked('membership_not_active')
        product=state['products'][selection['product']];quantity=selection.get('quantity',1);room=selection.get('room','standard')
        if room not in product['rooms'] or product['stock']<quantity or product['rooms'][room]<quantity:
            raise LabBlocked('inventory_unavailable')
        price=product.get('room_prices',{}).get(room,product['price'])*quantity
        available={k:Decimal(m[k]) for k in ('DP','BP','SP')};left=Decimal(price)
        plan={k:0 for k in ('DP','BP','SP','card')}
        plan['DP']=int(min(left,available['DP']));left-=plan['DP']
        if left and (available['BP'] or available['SP']) and contract['example_bonus_order'] is None:
            raise LabBlocked('bonus_order_policy_pending')
        rates={'hope':(15,10,50),'happy':(20,15,70),'premium':(25,18,100)}
        percentage=rates[m['grade']][('domestic','overseas','culture').index(product['category'])]
        cap=(Decimal(price)*percentage/100).to_integral_value(rounding=ROUND_FLOOR)
        for bucket in (contract['example_bonus_order'] or 'BP_SP').split('_'):
            allowed=min(available[bucket],cap) if bucket=='BP' else available[bucket]
            plan[bucket]=int(min(left,allowed));left-=plan[bucket]
        plan['card']=int(left)
        booking_id=_id('booking',request_key)
        for bucket in ('DP','BP','SP'):
            m[bucket]-=plan[bucket]
            if plan[bucket]:journal(uid,bucket,-plan[bucket],booking_id,'purchase')
        m['used']+=plan['DP'];product['stock']-=quantity;product['rooms'][room]-=quantity
        if plan['card']:
            state['cards'].append(dict(id=_id('card',request_key),member=uid,kind='purchase',
                amount=plan['card'],reference=booking_id,status='approved'))
        deposit=int((Decimal(price)*product['deposit_bps']/10000).to_integral_value(rounding=ROUND_FLOOR))
        state['bookings'][booking_id]=dict(id=booking_id,member=uid,product=selection['product'],room=room,
            quantity=quantity,total=price,plan=plan,status='confirmed',deposit_gross=deposit,
            balance_gross=price-deposit,balance_due=product['due'],payment_mode='full_simulator_payment')
        return deepcopy(state['bookings'][booking_id])
    if action in ('signup','set_card','purchase','withdraw_request'):
        if subject in state['pending']:raise LabBlocked('reconciliation_required_no_replay')
        if member is None:raise PermissionError('member identity required')
    if action=='set_card':
        member['card']=payload['scenario'];result=dict(scenario=member['card'])
    elif action in ('signup','purchase'):
        if any(w['member']==subject for w in state['withdrawals'].values()):raise LabBlocked('withdrawal_in_progress')
        if action=='signup':
            if member['status'] not in ('new','pending_first_payment'):raise LabBlocked('membership_already_requested')
            if member['card']=='unregistered':raise LabBlocked('card_not_registered')
            if member['card']=='decline':
                member['status']='pending_first_payment';result=dict(status='declined',effects_applied=False)
            elif member['card']=='unknown':
                state['pending'][subject]=dict(action=action,payload=payload,request_key=key)
                member['status']='pending_first_payment';result=dict(status='reconciliation_required',automatic_retry=False)
            else:result=credit_initial(subject)
        else:
            original=deepcopy(state);result=purchase(subject,payload,key)
            if result['plan']['card'] and member['card']!='success':
                scenario=member['card'];state=original
                if scenario=='unregistered':raise LabBlocked('card_not_registered')
                if scenario=='decline':result=dict(status='declined',effects_applied=False)
                else:
                    state['pending'][subject]=dict(action=action,payload=payload,request_key=key)
                    result=dict(status='reconciliation_required',automatic_retry=False)
    elif action=='resolve':
        uid=payload['member'];pending=state['pending'].get(uid)
        if pending is None:raise LabBlocked('no_uncertain_payment')
        if payload['outcome']=='decline':
            result=dict(status='declined',effects_applied=False)
        elif pending['action']=='signup':result=credit_initial(uid)
        else:result=purchase(uid,pending['payload'],pending['request_key'])
        del state['pending'][uid]
    elif action=='cancel_request':
        booking=state['bookings'].get(payload.get('booking'))
        if not booking or booking['member']!=subject:raise PermissionError('own booking required')
        if booking['status']!='confirmed':raise LabBlocked('booking_not_cancellable')
        if contract['cancellation_fee_won'] is None:raise LabBlocked('cancellation_policy_pending')
        if any(r['booking']==booking['id'] for r in state['refunds'].values()):raise LabBlocked('refund_already_requested')
        ref=_id('refund',key);result=dict(id=ref,booking=booking['id'],status='requested',fee=0)
        state['refunds'][ref]=deepcopy(result)
    elif action.startswith('cancel_'):
        refund=state['refunds'].get(payload.get('refund'))
        if refund is None:raise LabBlocked('refund_not_found')
        if action=='cancel_review':
            if refund['status']!='requested':raise LabBlocked('refund_not_reviewable')
            refund.update(status='reviewed',reviewer=subject)
        elif action=='cancel_approve':
            if refund['status']!='reviewed' or refund['reviewer']==subject:raise LabBlocked('independent_review_required')
            refund.update(status='approved',approver=subject)
        else:
            if refund['status']!='approved' or refund['approver']!=subject:raise LabBlocked('approved_refund_required')
            booking=state['bookings'][refund['booking']];m=state['members'][booking['member']]
            for bucket in ('DP','BP','SP'):
                amount=booking['plan'][bucket];m[bucket]+=amount
                if amount:
                    original=next(j['id'] for j in state['journal'] if j['reference']==booking['id'] and j['bucket']==bucket and j['kind']=='purchase')
                    journal(booking['member'],bucket,amount,booking['id'],'purchase_reversal',reverses=original)
            m['used']-=booking['plan']['DP'];product=state['products'][booking['product']]
            product['stock']+=booking['quantity'];product['rooms'][booking['room']]+=booking['quantity']
            if booking['plan']['card']:
                state['cards'].append(dict(id=_id('card-reversal',key),member=booking['member'],kind='purchase_reversal',
                    amount=-booking['plan']['card'],reference=booking['id'],status='cancelled'))
            booking['status']='cancelled';refund['status']='refunded'
        result=deepcopy(refund)
    elif action=='withdraw_request':
        if member['status']!='active':raise LabBlocked('suspension_withdrawal_policy_pending')
        if any(b['member']==subject and b['status']=='confirmed' for b in state['bookings'].values()):
            raise LabBlocked('active_booking_requires_review')
        if any(w['member']==subject for w in state['withdrawals'].values()):raise LabBlocked('withdrawal_already_requested')
        ceiling=int(max(Decimal(0),Decimal(member['principal'])-member['used']-member['returned']-member['deductions']))
        member['recurring']=False;ref=_id('withdrawal',key)
        result=dict(id=ref,member=subject,status='requested',ceiling=ceiling,free_bonus_cash=0,bonus_disposition='policy_pending')
        state['withdrawals'][ref]=deepcopy(result)
    elif action.startswith('withdraw_'):
        withdrawal=state['withdrawals'].get(payload.get('withdrawal'))
        if withdrawal is None:raise LabBlocked('withdrawal_not_found')
        if action=='withdraw_review':
            if withdrawal['status']!='requested':raise LabBlocked('withdrawal_not_reviewable')
            withdrawal.update(status='reviewed',reviewer=subject)
        elif action=='withdraw_approve':
            if withdrawal['status']!='reviewed' or withdrawal['reviewer']==subject:raise LabBlocked('independent_review_required')
            withdrawal.update(status='approved',approver=subject)
        else:
            if withdrawal['status']!='approved' or withdrawal['approver']!=subject:raise LabBlocked('approved_withdrawal_required')
            m=state['members'][withdrawal['member']]
            if withdrawal['member'] in state['pending'] or any(b['member']==withdrawal['member'] and b['status']=='confirmed' for b in state['bookings'].values()):
                raise LabBlocked('active_effects_require_review')
            ceiling=max(0,m['principal']-m['used']-m['returned']-m['deductions'])
            if ceiling!=withdrawal['ceiling'] or ceiling>m['DP']:raise LabBlocked('principal_changed_requires_review')
            m['DP']-=ceiling;m['returned']+=ceiling;m['status']='withdrawn'
            if ceiling:
                journal(withdrawal['member'],'DP',-ceiling,withdrawal['id'],'withdrawal')
                state['cards'].append(dict(id=_id('withdraw-return',key),member=withdrawal['member'],
                    kind='withdrawal',amount=-ceiling,reference=withdrawal['id'],original_reference=m['principal_origin'],status='returned'))
            withdrawal['status']='returned'
        result=deepcopy(withdrawal)
    else:raise ValueError('unsupported command')
    validate_state(state)
    return state,result


def validate_state(state):
    for member in state['members'].values():
        for field in ('DP','BP','SP','principal','used','returned','deductions'):_amount(member[field])
        if member['DP']!=max(0,member['principal']-member['used']-member['returned']-member['deductions']):
            raise ValueError('principal conservation failed')
    for uid,member in state['members'].items():
        for bucket in ('DP','BP','SP'):
            if sum(j['amount'] for j in state['journal'] if j['member']==uid and j['bucket']==bucket)!=member[bucket]:
                raise ValueError('journal balance conservation failed')
    for product in state['products'].values():
        if product['stock']<0 or any(v<0 for v in product['rooms'].values()) or sum(product['rooms'].values())!=product['stock']:
            raise ValueError('inventory conservation failed')

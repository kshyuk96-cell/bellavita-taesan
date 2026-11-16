"""Independent rational oracle for the documented TEST room allocation, not live policy."""
from fractions import Fraction

PLAN={'CP':0,'SP':100000,'BP':100001,'DP':600000,'card':200010}
TOTAL=1000011


def split_integer(total,weights):
    if type(total) is not int or not 0<=total<=10**15 or not weights or len(weights)>100 or any(type(w) is not int or w<0 for w in weights) or sum(weights)<total:raise ValueError('bounded nonnegative integer allocation required')
    if not sum(weights):return [0]*len(weights)
    quotas=[Fraction(total*w,sum(weights)) for w in weights];values=[q.numerator//q.denominator for q in quotas]
    ranked=sorted(range(len(weights)),key=lambda i:(-(quotas[i]-values[i]),i))
    for index in ranked[:total-sum(values)]:values[index]+=1
    return values


def room_expectation():
    totals=[(TOTAL+1)//2,TOTAL//2];weights=totals.copy();sources=[{},{}]
    for book,amount in PLAN.items():
        allocated=split_integer(amount,weights)
        for i,value in enumerate(allocated):sources[i][book]=value;weights[i]-=value
    return [dict(id=i+1,total=totals[i],sources=sources[i]) for i in range(2)]


def inspect_rounding(observed):
    expected=room_expectation();slices=[{k:r[k] for k in ('id','total','sources')} for r in observed['slices']]
    records=[dict(id='member-total',expression='floor(2*515470*9700/10000)',variables=dict(adults=2,normal_unit_KRW=515470,discount_bps=300),units='KRW',rounding='floor',expected=TOTAL,actual=observed['total'])]
    for i,row in enumerate(expected):
        for book,value in row['sources'].items():records.append(dict(id='room-'+str(i+1)+'-'+book,expression='floor(exact proportional quota), then largest fractional remainder; ties by ascending room index',variables=dict(plan=PLAN,room_totals=[500006,500005],allocation_order=list(PLAN)),units='KRW / point TEST 1:1',rounding='largest remainder; stable room index tie',expected=value,actual=observed['slices'][i]['sources'][book]))
    refunds=observed['afterSecond']['refunds'];first=refunds[0];second=refunds[1]
    first_expected=dict(expected[0]['sources'],card=0);second_expected=expected[1]['sources']
    for tag,row,values in [('before-balance',first,first_expected),('after-balance',second,second_expected)]:
        for book,value in values.items():records.append(dict(id=tag+'-'+book,expression='room original source; card only already paid; zero TEST fee',variables=dict(room_sources=values,fee_bps=0),units='KRW / point TEST 1:1',rounding='exact allocated integer',expected=value,actual=row['quote']['refund'][book]))
    for record in records:record.update(status='PASS' if type(record['actual']) is int and record['actual']==record['expected'] else 'FAIL',policy_approval='TEST slice sequence CP/SP/BP/DP/card, BP/SP relative order and cancellation-fee assumptions pending operational approval')
    return dict(slices_match=slices==expected and observed['plan']==PLAN,expected_slices=expected,actual_slices=slices,records=records)

"""Unapplied shipping-time formulas: explicit notes label only, never the event clock."""
import datetime as dt,re

def proposal(selector='$C$5'):
    if selector not in ('$C$5','$I$5'):raise ValueError('Known form selector required')
    return '=IFERROR(LET(d,INDIRECT("액자화환!B"&'+selector+'),n,INDIRECT("액자화환!J"&'+selector+'),k,(LEN(n)-LEN(SUBSTITUTE(n,"배송시각 ","")))/LEN("배송시각 "),t,IFERROR(REGEXEXTRACT(n,"(?:^|[ /])배송시각 ((?:[01][0-9]|2[0-3]):[0-5][0-9])(?:$|[ /])"),""),IF(OR(d="",k=0),"미확인",IF(OR(k<>1,t=""),"확인 필요",TEXT(d,"mm.dd")&" "&t))),"확인 필요")'

def model(day,notes):
    """Reference behavior, not an execution of Google Sheets formulas."""
    count=str(notes or '').count('배송시각 ')
    if not day or count==0:return '미확인'
    clocks=re.findall(r'(?:^|[ /])배송시각 ((?:[01][0-9]|2[0-3]):[0-5][0-9])(?:$|[ /])',str(notes))
    if count!=1 or len(clocks)!=1:return '확인 필요'
    try:return dt.date.fromisoformat(day).strftime('%m.%d')+' '+clocks[0]
    except (ValueError,TypeError):return '확인 필요'

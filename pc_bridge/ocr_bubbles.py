"""Conservative segmentation for verified light-theme yellow/white Kakao bubbles.

No fallback to combining the entire chat or the draft input. Other themes are
unsupported until separately verified. OCR-derived messages always need review.
"""
import struct
from pathlib import Path


def bubble_rectangles(path,bounds):
    data=Path(path).read_bytes()
    if data[:2]!=b'BM':raise ValueError('BMP required')
    offset=struct.unpack_from('<I',data,10)[0]
    size,width,signed_height,planes,bits,compression=struct.unpack_from('<IiiHHI',data,14)
    height=abs(signed_height)
    if size<40 or planes!=1 or bits not in (24,32) or compression!=0 or not 0<width<=4096 or not 0<height<=4096:
        raise ValueError('Unsupported capture format')
    stride=((width*bits+31)//32)*4;pixel=bits//8
    if offset+stride*height>len(data):raise ValueError('Incomplete capture')
    if not (0<=bounds['left']<bounds['right']<=width and 0<=bounds['top']<bounds['bottom']<=height):
        raise ValueError('Unverified capture bounds')
    rows=[]
    for y in range(bounds['top'],bounds['bottom']):
        start=offset+(height-1-y if signed_height>0 else y)*stride
        xs=[]
        for x in range(bounds['left'],bounds['right']):
            position=start+x*pixel;b,g,r=data[position:position+3]
            if (r>=240 and 180<=g<=250 and b<=40) or min(r,g,b)>=248:xs.append(x)
        if xs and xs[-1]-xs[0]>=45:rows.append((y,xs[0],xs[-1]+1))
    groups=[]
    for y,left,right in rows:
        if groups and y-groups[-1]['bottom']<=2:
            group=groups[-1];group['bottom']=y+1;group['left']=min(group['left'],left);group['right']=max(group['right'],right)
        else:groups.append({'top':y,'bottom':y+1,'left':left,'right':right})
    return [group for group in groups if group['bottom']-group['top']>=12 and
            group['top']>bounds['top']+2 and group['bottom']<bounds['bottom']-2]


def message_texts(lines,rectangles):
    messages=[]
    for box in rectangles:
        text=[]
        for line in lines:
            words=[word['text'] for word in line.get('words',[]) if
                   word['top']>=box['top'] and word['bottom']<=box['bottom'] and
                   word['left']>=box['left'] and word['right']<=box['right']]
            if words:text.append(' '.join(words))
        if text:messages.append('\n'.join(text))
    return messages

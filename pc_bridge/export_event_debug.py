"""Export only the synthetic event-test bubble, never the whole conversation."""
import argparse,asyncio,json,struct,zipfile
from pathlib import Path
from background_capture import capture
from background_ocr import recognize
from ocr_bubbles import bubble_rectangles,message_texts


def crop_bmp(source,destination,box):
    data=Path(source).read_bytes()
    if data[:2]!=b'BM':raise ValueError('BMP required')
    offset=struct.unpack_from('<I',data,10)[0]
    size,width,signed_height,planes,bits,compression=struct.unpack_from('<IiiHHI',data,14)
    height=abs(signed_height)
    if size<40 or planes!=1 or bits not in (24,32) or compression!=0:raise ValueError('Unsupported BMP')
    left,top,right,bottom=(int(box[k]) for k in ('left','top','right','bottom'))
    if not 0<=left<right<=width or not 0<=top<bottom<=height:raise ValueError('Invalid crop bounds')
    pixel=bits//8;stride=((width*bits+31)//32)*4
    if len(data)<offset+stride*height:raise ValueError('Incomplete BMP')
    cropped_width,cropped_height=right-left,bottom-top
    cropped_stride=((cropped_width*bits+31)//32)*4;rows=[]
    for y in range(bottom-1,top-1,-1):
        row=offset+(height-1-y if signed_height>0 else y)*stride
        rows.append(data[row+left*pixel:row+right*pixel]+bytes(cropped_stride-cropped_width*pixel))
    pixels=b''.join(rows)
    header=struct.pack('<2sIHHI',b'BM',54+len(pixels),0,0,54)
    header+=struct.pack('<IiiHHIIiiII',40,cropped_width,cropped_height,1,bits,0,len(pixels),0,0,0,0)
    Path(destination).write_bytes(header+pixels)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--room',required=True)
    args=parser.parse_args();root=Path(args.config).resolve().parent
    source=root/'event-debug-private-full.bmp';image=root/'event-test-only.bmp';text=root/'event-test-only-ocr.txt'
    output=root/'event-ocr-debug.zip'
    try:
        cfg=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        report=capture(cfg,args.room,source)
        recognized=asyncio.run(recognize(source,include_lines=True,save_text=False))
        if not recognized.get('ocr_available'):raise ValueError('OCR unavailable')
        choices=[]
        for box in bubble_rectangles(source,report['chat_bounds']):
            bodies=message_texts(recognized['lines'],[box])
            if bodies and '2099-12-14' in bodies[0] and '테스트' in bodies[0]:choices.append((box,bodies[0]))
        if len(choices)!=1:raise ValueError('Unique visible synthetic event-test bubble not found')
        box,body=choices[0];crop_bmp(source,image,box);text.write_text(body,encoding='utf-8-sig')
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(image,image.name);archive.write(text,text.name)
        print(json.dumps({'exported':True,'test_bubble_only':True,'full_chat_included':False,
                          'path':str(output),'uploaded':False}))
    except Exception as error:
        if output.exists():output.unlink()
        print(json.dumps({'exported':False,'error_type':type(error).__name__}))
    finally:
        if source.exists():source.unlink()


if __name__=='__main__':main()

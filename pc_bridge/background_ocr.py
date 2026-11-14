"""Windows OCR on a local allowlisted-window capture; no upload or UI input."""
import argparse
import asyncio
import json
import os
from pathlib import Path


def scaled_size(width,height,limit):
    if min(width,height,limit)<=0:raise ValueError('Invalid OCR dimensions')
    factor=min(3,limit/max(width,height))
    return max(1,round(width*factor)),max(1,round(height*factor))


async def recognize(path,*,include_lines=False,save_text=True):
    import winrt.windows.foundation
    import winrt.windows.foundation.collections
    import winrt.windows.globalization
    import winrt.windows.storage.streams
    from winrt.windows.storage import StorageFile,FileAccessMode
    from winrt.windows.graphics.imaging import (BitmapDecoder,BitmapPixelFormat,BitmapAlphaMode,
        BitmapTransform,BitmapInterpolationMode,ExifOrientationMode,ColorManagementMode)
    from winrt.windows.media.ocr import OcrEngine
    languages=list(OcrEngine.available_recognizer_languages)
    korean=next((language for language in languages if language.language_tag.lower().startswith('ko')),None)
    if korean is None:
        return {'ocr_available':False,'error_type':'korean_ocr_language_missing',
                'available_language_tags':[language.language_tag for language in languages]}
    engine=OcrEngine.try_create_from_language(korean)
    if engine is None:return {'ocr_available':False,'error_type':'korean_ocr_engine_unavailable'}
    source=await StorageFile.get_file_from_path_async(str(path.resolve()))
    stream=await source.open_async(FileAccessMode.READ)
    bitmap=None;converted=None
    try:
        decoder=await BitmapDecoder.create_async(stream)
        transform=BitmapTransform()
        transform.scaled_width,transform.scaled_height=scaled_size(decoder.pixel_width,decoder.pixel_height,OcrEngine.max_image_dimension)
        transform.interpolation_mode=BitmapInterpolationMode.CUBIC
        converted=await decoder.get_software_bitmap_transformed_async(BitmapPixelFormat.BGRA8,
            BitmapAlphaMode.PREMULTIPLIED,transform,ExifOrientationMode.IGNORE_EXIF_ORIENTATION,
            ColorManagementMode.DO_NOT_COLOR_MANAGE)
        scale_x=decoder.pixel_width/converted.pixel_width
        scale_y=decoder.pixel_height/converted.pixel_height
        result=await engine.recognize_async(converted)
        output=path.with_name('background-ocr-test.txt')
        if save_text:output.write_text('\n'.join(line.text for line in result.lines),encoding='utf-8-sig')
        report={'ocr_available':True,'language':korean.language_tag,
                'recognized_lines':len(result.lines),'recognized_characters':len(result.text),
                'text_path':str(output) if save_text else None,'text_uploaded':False,'order_accuracy_verified':False}
        if include_lines:
            lines=[]
            for line in result.lines:
                boxes=[word.bounding_rect for word in line.words]
                if not boxes:continue
                lines.append({'text':line.text,'top':min(b.y for b in boxes)*scale_y,
                              'bottom':max(b.y+b.height for b in boxes)*scale_y,
                              'left':min(b.x for b in boxes)*scale_x,'right':max(b.x+b.width for b in boxes)*scale_x,
                              'words':[{'text':word.text,'left':word.bounding_rect.x*scale_x,'top':word.bounding_rect.y*scale_y,
                                        'right':(word.bounding_rect.x+word.bounding_rect.width)*scale_x,
                                        'bottom':(word.bounding_rect.y+word.bounding_rect.height)*scale_y} for word in line.words]})
            report['lines']=lines
        return report
    finally:
        for resource in (converted,bitmap,stream):
            close=getattr(resource,'close',None)
            if callable(close):close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image');args=parser.parse_args()
    path=Path(args.image) if args.image else Path(os.environ['LOCALAPPDATA'])/'TaesanPlanningTest'/'background-render-test.bmp'
    try:
        if not path.is_file():raise FileNotFoundError()
        print(json.dumps(asyncio.run(recognize(path)),ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'ocr_available':False,'error_type':type(error).__name__,'text_uploaded':False}))


if __name__=='__main__':main()

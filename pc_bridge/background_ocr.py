"""Windows OCR on a local allowlisted-window capture; no upload or UI input."""
import argparse
import asyncio
import json
import os
from pathlib import Path


async def recognize(path):
    import winrt.windows.foundation
    import winrt.windows.foundation.collections
    import winrt.windows.globalization
    import winrt.windows.storage.streams
    from winrt.windows.storage import StorageFile,FileAccessMode
    from winrt.windows.graphics.imaging import BitmapDecoder,SoftwareBitmap,BitmapPixelFormat,BitmapAlphaMode
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
        bitmap=await decoder.get_software_bitmap_async()
        converted=SoftwareBitmap.convert(bitmap,BitmapPixelFormat.BGRA8,BitmapAlphaMode.PREMULTIPLIED)
        result=await engine.recognize_async(converted)
        output=path.with_name('background-ocr-test.txt')
        output.write_text('\n'.join(line.text for line in result.lines),encoding='utf-8-sig')
        return {'ocr_available':True,'language':korean.language_tag,
                'recognized_lines':len(result.lines),'recognized_characters':len(result.text),
                'text_path':str(output),'text_uploaded':False,'order_accuracy_verified':False}
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

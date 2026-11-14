"""Package only approved diagnostics; never include local reports or order data."""
import argparse,zipfile
from pathlib import Path

FILES=('START_DIAGNOSE.cmd','diagnose.ps1','README_KO.txt','VERSION.json')

def build(output,source=None):
    source=Path(source or Path(__file__).resolve().parent);output=Path(output)
    if output.suffix.lower()!='.zip':raise ValueError('ZIP output required')
    if not (source/'diagnose.ps1').read_bytes().startswith(b'\xef\xbb\xbf'):raise ValueError('Windows PowerShell 5 needs UTF-8 BOM for Korean text')
    cmd=(source/'START_DIAGNOSE.cmd').read_bytes()
    if b'\r\n' not in cmd or b'\\r\\n' in cmd:raise ValueError('CMD must have real CRLF lines')
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        for name in FILES:z.write(source/name,name)
    return output

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args();print(build(args.output))

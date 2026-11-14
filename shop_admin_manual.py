"""Operator manual served from the same tracked Markdown source."""
from html import escape
from pathlib import Path

SOURCE=Path(__file__).with_name('docs')/'shop-admin-manual.md'


def page(admin_path):
    sections=[];current=[];title='관리 매뉴얼'
    for line in SOURCE.read_text().splitlines():
        if line.startswith('## '):
            if current:sections.append((title,''.join(current)))
            title=line[3:];current=[]
        elif line.startswith('# '):continue
        elif line.strip():current.append('<p>'+escape(line)+'</p>')
    if current:sections.append((title,''.join(current)))
    articles=''.join('<article id="manual-'+str(i)+'"><h2>'+escape(title)+'</h2>'+content+'</article>' for i,(title,content) in enumerate(sections))
    nav=''.join('<a href="#manual-'+str(i)+'">'+escape(title)+'</a> ' for i,(title,_) in enumerate(sections))
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>벨라비타 관리자 매뉴얼</title><style>body{background:#f5f5ef;color:#243329;font:16px/1.7 system-ui;margin:0}main{max-width:900px;margin:auto;padding:22px}article{background:white;border:1px solid #dce3d8;border-radius:10px;padding:20px;margin:18px 0}p{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#28513a}input{display:block;width:100%;box-sizing:border-box;font:inherit;padding:12px;margin:12px 0}nav{display:flex;gap:12px;flex-wrap:wrap}@media print{nav,input,.manual-links{display:none}article{break-inside:avoid}}</style><main><h1>벨라비타 관리자 매뉴얼</h1><p class="manual-links"><a href="'''+escape(admin_path)+'''">관리자로 돌아가기</a> · <a href="'''+escape(admin_path)+'''/manual.md">매뉴얼 파일 받기</a></p><label>매뉴얼 검색<input id="manual-search" type="search" placeholder="결제 확인, 배송, 상품 수정 등"></label><p id="manual-count" role="status"></p><nav>'''+nav+'</nav>'+articles+'''</main><script>const search=document.getElementById('manual-search');search.addEventListener('input',()=>{let count=0;for(const section of document.querySelectorAll('article')){section.hidden=!section.textContent.toLocaleLowerCase('ko-KR').includes(search.value.trim().toLocaleLowerCase('ko-KR'));if(!section.hidden)count++;}document.getElementById('manual-count').textContent=count?count+'개 항목':'검색 결과가 없습니다.';});</script></html>'''

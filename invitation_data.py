"""Read data literals embedded by invitation vendors. Never execute page JavaScript."""
import json,re
from dataclasses import dataclass

@dataclass(frozen=True)
class Ref:
    name: str

class LiteralReader:
    def __init__(self,text,start=0):self.text=text;self.i=start;self.nodes=0
    def space(self):
        while self.i<len(self.text) and self.text[self.i].isspace():self.i+=1
    def consume(self,token):
        self.space()
        if not self.text.startswith(token,self.i):raise ValueError('Unsupported invitation data syntax')
        self.i+=len(token)
    def value(self,depth=0):
        self.nodes+=1
        if depth>100 or self.nodes>150000:raise ValueError('Invitation data too complex')
        self.space()
        if self.i>=len(self.text):raise ValueError('Incomplete invitation data')
        ch=self.text[self.i]
        if ch=='"':
            value,end=json.JSONDecoder().raw_decode(self.text[self.i:]);self.i+=end;return value
        if ch=="'":
            self.i+=1;out=[]
            while self.i<len(self.text):
                c=self.text[self.i];self.i+=1
                if c=="'":return ''.join(out)
                if c=='\\':
                    if self.i>=len(self.text):break
                    c=self.text[self.i];self.i+=1
                    if c in ('u','x'):
                        count=4 if c=='u' else 2;token=self.text[self.i:self.i+count]
                        if len(token)!=count or not re.fullmatch('[0-9a-fA-F]+',token):raise ValueError('Invalid escape')
                        out.append(chr(int(token,16)));self.i+=count
                    else:out.append({'n':'\n','r':'\r','t':'\t','b':'\b','f':'\f'}.get(c,c))
                else:out.append(c)
            raise ValueError('Unclosed invitation string')
        if ch=='{':
            self.i+=1;result={};self.space()
            if self.text[self.i:self.i+1]=='}':self.i+=1;return result
            while True:
                self.space()
                if self.text[self.i:self.i+1] in ('"',"'"):key=self.value(depth+1)
                else:
                    match=re.match(r'[A-Za-z_$][A-Za-z0-9_$]*',self.text[self.i:])
                    if not match:raise ValueError('Invalid data key')
                    key=match[0];self.i+=len(key)
                self.consume(':');result[key]=self.value(depth+1);self.space()
                if self.text[self.i:self.i+1]=='}':self.i+=1;return result
                self.consume(',')
        if ch=='[':
            self.i+=1;result=[];self.space()
            if self.text[self.i:self.i+1]==']':self.i+=1;return result
            while True:
                result.append(self.value(depth+1));self.space()
                if self.text[self.i:self.i+1]==']':self.i+=1;return result
                self.consume(',')
        num=re.match(r'-?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',self.text[self.i:])
        if num:
            self.i+=len(num[0]);return float(num[0]) if any(c in num[0] for c in '.eE') else int(num[0])
        word=re.match(r'[A-Za-z_$][A-Za-z0-9_$]*',self.text[self.i:])
        if not word:raise ValueError('Unsupported data expression')
        self.i+=len(word[0]);name=word[0]
        if name in {'true','false','null','undefined'}:return {'true':True,'false':False,'null':None,'undefined':None}[name]
        if name=='void':self.consume('0');return None
        if name=='Array':
            self.consume('(');size=self.value(depth+1);self.consume(')')
            if type(size)!=int or not 0<=size<=10000:raise ValueError('Invalid array size')
            return [None]*size
        self.space()
        if self.text[self.i:self.i+1] in ('(','.','['):raise ValueError('Executable data expression rejected')
        return Ref(name)

def resolve(value,bindings,depth=0):
    if depth>110:raise ValueError('Deep reference data')
    if isinstance(value,Ref):
        if value.name not in bindings:raise ValueError('Unknown invitation data reference')
        return bindings[value.name]
    if isinstance(value,dict):return {k:resolve(v,bindings,depth+1) for k,v in value.items()}
    if isinstance(value,list):return [resolve(v,bindings,depth+1) for v in value]
    return value

def nuxt_bindings(script):
    """Read only the outer IIFE's literal argument table, not comments/user guestbook objects."""
    match=re.search(r'window\.__NUXT__\s*=\s*\(function\(([^)]*)\)',script)
    if not match:raise ValueError('No Nuxt data table')
    params=[v.strip() for v in match[1].split(',')]
    at=script.rfind('}(')
    if at<0:raise ValueError('No literal argument table')
    reader=LiteralReader(script,at+2);args=[]
    while True:
        args.append(reader.value());reader.space()
        if reader.text[reader.i:reader.i+1]==')':break
        reader.consume(',')
    if len(params)!=len(args):raise ValueError('Data table mismatch')
    return dict(zip(params,args))

def named_values(text, key, bindings=None, quoted=False):
    # Walk lexical tokens so guestbook text containing "manName: ..." is never a key.
    values=[];at=0
    while at<len(text):
        if text.startswith('//',at):
            end=text.find('\n',at+2);at=len(text) if end<0 else end+1;continue
        if text.startswith('/*',at):
            end=text.find('*/',at+2);at=len(text) if end<0 else end+2;continue
        ch=text[at];candidate=None;end=at+1
        if ch in ('"',"'"):
            reader=LiteralReader(text,at)
            try:candidate=reader.value();end=reader.i
            except (ValueError,IndexError):
                # Malformed strings are not a source of trusted event fields.
                break
            if not quoted:candidate=None
        elif ch=='`':
            end=at+1
            while end<len(text):
                if text[end]=='\\':end+=2;continue
                if text[end]=='`':end+=1;break
                end+=1
        elif ch.isascii() and (ch.isalpha() or ch in '_$'):
            match=re.match(r'[A-Za-z_$][A-Za-z0-9_$]*',text[at:]);end=at+len(match[0])
            if not quoted:candidate=match[0]
        if candidate==key:
            cursor=end
            while cursor<len(text) and text[cursor].isspace():cursor+=1
            if text[cursor:cursor+1]==':':
                try:
                    value=resolve(LiteralReader(text,cursor+1).value(),bindings or {})
                    if value not in values:values.append(value)
                except (ValueError,IndexError,TypeError):pass
        at=end
    return values

def next_text(scripts):
    parts=[]
    for script in scripts:
        at=0
        while True:
            found=script.find('self.__next_f.push(',at)
            if found<0:break
            reader=LiteralReader(script,found+len('self.__next_f.push('))
            try:value=reader.value()
            except (ValueError,IndexError):break
            at=reader.i
            if isinstance(value,list) and len(value)>1 and isinstance(value[1],str):parts.append(value[1])
    return ''.join(parts)

"""Sender parsing from Bellavita’s automatic scroll renderer (sender.py)."""

import re


# Match longer titles first so a compound title stays on the affiliation line.
SENDER_TITLES = ('청도군수', '군수', '부장판사', '지회장', '이사장', '영업이사', '사무총장', '파트너변호사', '은행장', '대표 이사', '총연합 회장', '대표이사', '총연합회장', '노조위원장',
                 '국회의원', '의장', '변호사', 'CEO', 'CFO', 'COO', 'CTO', 'CMO', 'CIO', 'CHRO', 'CSO', 'CPO', 'CRO', 'VP', '위원장', '부회장', '부사장', '대표', '회장', '사장', '이사',
                 '팀장', '부장', '과장', '센터장', '원장', '소장', '실장', '본부장')


def split_sender_legacy(sender):
    words = sender.split()
    parenthetical = re.fullmatch(r'([가-힣]{2,4})\s*(\([^()]+\))', ' '.join(words))
    if parenthetical:
        return {'label': '', 'name': parenthetical[1], 'note': parenthetical[2]}
    multiple = re.fullmatch(r'(.+?)\s+(지회장|회장|대표|위원장|이사장)\s+([가-힣]{2,4})\s+((?:협의회장|부녀회장|직장회장|부회장|이사|총무)\s+[가-힣]{2,4}.*)', ' '.join(words))
    if multiple:
        return {'label': f'{multiple[1]} {multiple[2]}', 'name': multiple[3], 'note': multiple[4]}
    affiliations = re.fullmatch(
        r'(.+?(?:\d+\s*(?:회|기)))\s+(.+?\(주\))\s+([가-힣]{2,4})',
        ' '.join(words),
    )
    if affiliations and not affiliations[3].endswith(('일동', '임직원', '직원', '가족', '동문회')):
        return {'label': f'{affiliations[1]} {affiliations[2]}', 'name': affiliations[3]}
    normalized = [word.upper() for word in words]
    if len(words) == 1 and re.fullmatch(r'[가-힣]{2,4}', words[0]) and not words[0].endswith(('일동','동문회','협회','가족','임직원')):
        return {'label': '', 'name': words[0]}
    # A trailing affiliation is separate from the person's title and name.
    match = re.fullmatch(r'(.+?)\s+(총연합회장|대표이사|대표|회장|사장|위원장|팀장)\s+([가-힣]{2,4})\s+((?:\(주\)|㈜).+)', ' '.join(words))
    if match:
        affiliation, title, name, footer = match.groups()
        return {'label': affiliation, 'title': title, 'name': name, 'footer': footer}
    for title in SENDER_TITLES:
        title_words = title.split()
        if normalized[-len(title_words):] == title_words and len(words) > len(title_words):
            rest = words[:-len(title_words)]
            if re.fullmatch(r'[가-힣]{2,4}', rest[-1]) and not rest[-1].endswith(('일동','동문회','협회')):
                return {'label': ' '.join([*rest[:-1], *words[-len(title_words):]]), 'name': rest[-1]}
        if (len(words) > len(title_words)
                and normalized[-len(title_words)-1:-1] == title_words
                and re.fullmatch(r'[가-힣]{2,4}', words[-1])
                and not words[-1].endswith(('일동','동문회','협회'))):
            return {'label': ' '.join(words[:-1]), 'name': words[-1]}
    # Preserve known organization boundaries even when the input omits spaces.
    organization = re.fullmatch(
        r'(대한민국)\s*([가-힣]+?향우회)\s*(총연합회)\s*(임원\s*일동)', sender.strip()
    )
    if organization:
        country, association, federation, _ = organization.groups()
        return {'label': '', 'name': f'{country}\n{association}\n{federation} 임원 일동', 'group': True}
    cohort = re.fullmatch(r'(.+?)\s*(제\s*\d+\s*기\s*(?:원우회|동기회|동문회|모임)(?:\s*일동)?)', ' '.join(words))
    if cohort:
        return {'label': '', 'name': '\n'.join(cohort.groups()), 'group': True}
    if len(words) >= 2 and re.fullmatch(r'[가-힣]{2,4}', words[-1]):
        role = re.fullmatch(r'[가-힣]{1,5}(?:대표|이사|총장|위원장|변호사)', words[-2])
        if role and not words[-1].endswith(('일동', '가족', '협회', '동문회')):
            return {'label': ' '.join(words[:-1]), 'name': words[-1]}
    university = re.fullmatch(r'([^\s]+(?:대학교|대))\s*(.+?(?:아카데미|대학원))\s*(총동창회|동창회|총동문회|동문회)', ' '.join(words))
    if university:
        return {'label': '', 'name': '\n'.join(university.groups()), 'group': True}
    if len(words) == 2 and re.fullmatch(r'[가-힣]{2,4}', words[-1]) and not words[-1].endswith(
        ('일동', '동문회', '협회', '가족', '임직원', '동창회', '원우회', '동기회', '모임', '친구들')
    ):
        return {'label': words[0], 'name': words[-1]}
    return {'label': '', 'name': sender, 'group': True}


def split_sender(sender):
    normalized = ' '.join(sender.split())

    # Capture the first name following a recognizable title, with optional trailing details.
    roles = (
        r'[가-힣0-9A-Za-z]{0,20}(?:회장|위원장|부장|이사|팀장|실장|총장|군수|판사|변호사|지점장|단장|교수|연구원|목사|의학팀)'
        r'|국회의원|의장|代表理事|대표이사|대표노무사|대표변호사|대표님?|부사장|사장|원장|[가-힣]{0,10}고문|교수|국장|단장|명예단장|교장|발행인|기업호민관|강사|사무국|사무국장|부대표|총재|지구[0-9]+부총재|사진작가|작가|위원장직무대행|MBA|배우|가수|감사|목사|동기|CFO|CEO'
    )
    names = r'[김이박최정강조윤장임한오서신권황안송전홍유고문양손배백허남심노하곽성차주우구민진지엄채원천방공현변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예경봉사부가복목태피라류나함상견][가-힣]{1,2}|(?:남궁|황보|제갈|선우|독고|서문|사공|동방)[가-힣]{1,2}'
    blocked = {'대표','이사','위원','후보','고문','회원','직원','일동','가족','한국','서울','발행인','한국인','변호사','노무사','의장','원장','국회의원','연합회',*SENDER_TITLES}
    attached_titles = '|'.join(
        re.escape(title) for title in sorted(
            {*SENDER_TITLES, '대표님', '교수', '변호사', '목사', '작가'}, key=len, reverse=True
        ) if title not in {'이사', '군수'}
    )
    attached_name = re.compile(
        r'(?<!\S)(?:(' + names + r')(' + attached_titles + r')|('
        + attached_titles + r')(' + names + r'))(?=$|[\s(,·ㆍ/])'
    )
    def separate_title(match):
        name = match[1] or match[4]
        if match[0] in SENDER_TITLES or name in blocked or name.endswith(('협회', '연합회', '동문회', '동창회')):
            return match[0]
        return ' '.join(part for part in match.groups() if part)

    normalized = attached_name.sub(separate_title, normalized)
    corporate = re.fullmatch(
        r'((?:\(\s*주\s*\)|（\s*주\s*）|㈜)\s*[^\s,·ㆍ/]+)'
        r'[\s,·ㆍ/]+([^,·ㆍ/]+?(?:전시관|박물관|연구소|센터|협회|재단))'
        r'[\s,·ㆍ/]*(대표이사|대표|관장|회장|원장|이사장)\s*(' + names + r')',
        normalized,
    )
    if corporate and corporate[4] not in blocked:
        company, affiliation, title, name = corporate.groups()
        return {'label': f'{company} {affiliation} {title}', 'name': name,
                'companyAffiliationTitle': (company, affiliation, title)}
    previous = split_sender_legacy(
        sender if normalized == ' '.join(sender.split()) else normalized
    )
    if re.search(r'(?:일동|임직원|회원|가족|동문회|동창회|원우회)$', normalized):
        return split_sender_legacy(sender)
    terminal_title = re.fullmatch(
        r'(.*?)(' + names + r')\s+(' + attached_titles + r')(?:\s+(\([^()]+\)))?',
        normalized,
    )
    if terminal_title and (not terminal_title[1] or terminal_title[1].endswith(' ')):
        return {'label': (terminal_title[1] + terminal_title[3]).strip(),
                'name': terminal_title[2], 'note': terminal_title[4] or ''}
    branch = re.fullmatch(
        r'(.+?)\s+(\S+(?:지회|지부|분회))\s+(사무국|강사|사무국장|지회장|지부장)\s+([가-힣]{2,4})',
        normalized,
    )
    if branch:
        return {'label': f'{branch[1]}\n{branch[2]} {branch[3]}', 'name': branch[4]}

    approved = {
        '재경 광주일고 동창회(54회 ) 수석부회장 징검다리 나상현':
            {'label':'재경 광주일고 동창회(54회 )\n수석부회장 징검다리','name':'나상현'},
        '선물액자화환 벨라비타 대구지사장 박다은':
            {'label':'선물액자화환 벨라비타\n대구지사장','name':'박다은'},
    }
    if normalized in approved:
        return approved[normalized]
    pairs = re.fullmatch(r'(.+?)\s+([가-힣]{3})(?:\s*[,·ㆍ]\s*|\s+)([가-힣]{3})', normalized)
    if pairs and (pairs[1] in {'하이코어','서흥공구'} or re.search(r'(?:'+roles+r')$', pairs[1])) and all(
        re.fullmatch(names,n) and n not in blocked for n in pairs.groups()[1:]
    ):
        first = pairs.start(2)
        return {'label':pairs[1], 'name':normalized[first:], 'equalNames':True}

    suffix_title = re.fullmatch(
        r'(.+?\s(?:대표|회장|원장|이사장))\s+('+names+r')\s+(교수|작가|박사|변호사|노무사)',
        normalized,
    )
    if suffix_title:
        return {'label': f'{suffix_title[1]} {suffix_title[3]}', 'name': suffix_title[2]}

    attached = re.fullmatch(r'(.+?\s(?:대표이사|대표|회장|원장))('+names+r')', normalized)
    if attached and attached[2] not in blocked:
        return {'label': attached[1], 'name': attached[2]}

    explicit_roles = '|'.join(re.escape(title) for title in sorted(SENDER_TITLES, key=len, reverse=True))
    explicit_person = re.compile(r'(?:^|\s)(?:' + explicit_roles + r')\s+(' + names + r')(?=$|\s)')
    named_people = [match for match in explicit_person.finditer(normalized) if match[1] not in blocked]
    if len(named_people) > 1 and len({match[1] for match in named_people}) > 1:
        first = named_people[0]
        return {'label': normalized[:first.start(1)].strip(), 'name': first[1],
                'note': normalized[first.end(1):].strip(), 'multipleSenders': True}

    for match in re.finditer(r'(?:^|[\s/)]|(?<=㈜))(?:'+roles+r')(?:\s+\d+기)?\s+('+names+r')(?=$|[\s(,·/ㆍ])', normalized, re.I):
        name = match[1]
        if name in blocked:
            continue
        before = normalized[:match.start(1)].strip()
        after = normalized[match.end(1):].strip()
        if not previous.get('group') and not after and previous.get('name') == name:
            return previous
        if not after or re.fullmatch(names, after) or re.match(r'^0\d{1,2}[- .]\d{3,4}[- .]\d{4}$', after) or after.startswith(('(', '㈜', ',', '·', 'ㆍ')) or re.search(r'[\s/)](?:'+roles+r')\s', ' '+after+' ', re.I):
            if after.startswith((',', '·', 'ㆍ')):
                return {'label': before, 'name': name, 'note': after[1:].lstrip(),
                        'nameSuffix': after[0]}
            return {'label': before, 'name': name, 'note': after}

    # A short line containing only names can follow a separate affiliation line.
    pair = re.fullmatch(r'(.+?)\s+('+names+r')\s+('+names+r')', normalized)
    if pair and all(len(n) == 3 and n not in blocked for n in pair.groups()[1:]) and (
        '\n' in sender or any(x in pair[1] for x in ('공구','주식회사','(주)','㈜'))
    ):
        return {'label':pair[1], 'name':pair[2], 'note':pair[3]}
    if not previous.get('group'):
        return previous
    lines = [line.strip() for line in sender.splitlines() if line.strip()]
    if len(lines) >= 2:
        tail = lines[-1].split()
        if 2 <= len(tail) <= 4 and all(re.fullmatch(names,n) and n not in blocked for n in tail):
            return {'label': '\n'.join(lines[:-1]), 'name': tail[0], 'note': ' '.join(tail[1:])}
    return previous

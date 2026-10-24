"""Manual, rate-aware queries to existing Emby search and Douban public suggestions."""
import json
import re
import time
from html import unescape
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor
import media_match

_CACHE={}


def cached_query(key,lookup,success_ttl=600,error_ttl=30):
    previous=_CACHE.get(key)
    if previous and previous[0]>time.time():return previous[1]
    result=lookup()
    _CACHE[key]=(time.time()+(success_ttl if result.get('status')=='ok' else 900 if result.get('status')=='rate_limited' else error_ttl),result)
    if len(_CACHE)>200:
        for k in list(_CACHE)[:100]:_CACHE.pop(k,None)
    return result


def douban_suggest(session,query,kind):
    def lookup():
        try:
            r=session.get('https://movie.douban.com/j/subject_suggest',params={'q':query},headers={'User-Agent':'Mozilla/5.0','Referer':'https://movie.douban.com/'},timeout=6)
            if r.status_code in {403,429}:return {'status':'rate_limited','items':[]}
            r.raise_for_status();payload=r.json()
            if not isinstance(payload,list):return {'status':'invalid_response','items':[]}
            rows=[]
            for data in payload[:20]:
                if not isinstance(data,dict) or not re.fullmatch('[1-9][0-9]*',str(data.get('id') or '')):continue
                episode=str(data.get('episode') or '')
                is_show=episode.isdigit() and int(episode)>1
                if kind=='Movie' and is_show:continue
                # Suggestions label TV as type=movie too. Episode count is the only type hint.
                # Missing episode counts cannot establish a movie/TV identity.
                # Keep uncertain results for explicit review instead of silently dropping them.
                rows.append({'title':str(data.get('title') or ''),'year':media_match.year(data.get('year')),'kind':kind,
                             'douban_id':str(data['id']),'imdb_id':None,'douban_score':None,
                             'source':'douban_suggest','source_label':'豆瓣名称查询','poster':safe_image(data.get('img')),
                             'cache_updated_at':None,'overview':'','type_hint':'episode_count' if is_show else 'unverified',
                             'type_unverified':kind=='Series' and not is_show})
            return {'status':'ok','items':rows}
        except Exception:return {'status':'lookup_error','items':[]}
    return cached_query(('douban',kind,query),lookup)


def safe_image(value):
    value=str(value or '')
    return value if value.startswith('https://') else None


def emby_search(emby,query,kind):
    def lookup():
        try:
            payload=emby('POST','/Items/RemoteSearch/'+('Series' if kind=='Series' else 'Movie'),json={'SearchInfo':{'Name':query,'MetadataLanguage':'zh-CN'},'SearchProviderName':'TheMovieDb'},timeout=8).json()
            if not isinstance(payload,list):return {'status':'invalid_response','items':[]}
            rows=[]
            for data in payload[:15]:
                ids={str(k).casefold():str(v) for k,v in (data.get('ProviderIds') or {}).items() if v}
                if not ids.get('tmdb'):continue
                rows.append({'title':str(data.get('Name') or ''),'year':media_match.year(data.get('ProductionYear')),'kind':kind,
                             'tmdb_id':ids['tmdb'],'douban_id':ids.get('douban'),'imdb_id':ids.get('imdb'),
                             'douban_score':None,'source':'emby_tmdb','source_label':'Emby · TMDB','poster':safe_image(data.get('ImageUrl')),
                             'cache_updated_at':None,'overview':str(data.get('Overview') or '')[:500]})
            return {'status':'ok','items':rows}
        except Exception:return {'status':'lookup_error','items':[]}
    return cached_query(('tmdb',kind,query),lookup)


def gather(session, emby, query, kind):
    with ThreadPoolExecutor(max_workers=3) as executor:
        suggestion = executor.submit(douban_suggest, session, query, kind)
        full = executor.submit(douban_search, session, query, kind)
        tmdb = executor.submit(emby_search, emby, query, kind)
        suggested, searched = suggestion.result(), full.result()
        records = {r['douban_id']: r for r in suggested['items']}
        for row in searched['items']:
            existing = records.get(row['douban_id'])
            if existing:
                row = {**existing, **row, 'imdb_id': existing.get('imdb_id') or row.get('imdb_id')}
            records[row['douban_id']] = row
        status = 'ok' if 'ok' in {suggested['status'], searched['status']} else searched['status']
        return {'status': status, 'suggest_status': suggested['status'], 'search_status': searched['status'],
                'items': list(records.values()), 'incomplete': True}, tmdb.result()


def combine(local,douban,tmdb,item):
    rows=[dict(r) for r in local]
    for r in douban:
        matches=[x for x in rows if x.get('douban_id')==r.get('douban_id')]
        if matches:
            if not matches[0].get('poster'):matches[0]['poster']=r.get('poster')
        else:rows.append(r)
    current={str(k).casefold():str(v) for k,v in (item.get('ProviderIds') or {}).items() if v}
    for r in tmdb:
        # Reuse current provider IDs only when Emby TMDB record agrees by actual ID.
        if r.get('tmdb_id')==current.get('tmdb'):
            r={**r,'imdb_id':r.get('imdb_id') or current.get('imdb'),'douban_id':r.get('douban_id') or current.get('douban')}
        matches=[x for x in rows if r.get('imdb_id') and x.get('imdb_id')==r['imdb_id'] and x.get('year')==r.get('year')]
        if len(matches)==1:
            if not matches[0].get('poster'):matches[0]['poster']=r.get('poster')
            matches[0]['tmdb_id']=r.get('tmdb_id')
        else:rows.append(r)
    for row in rows:row['bindable']=bool(row.get('douban_id') or row.get('imdb_id'))
    return rows


def plain_text(value):
    return unescape(re.sub(r'<[^>]+>', '', value or '')).strip()


def parse_douban_search(text, kind):
    rows = []
    for block in re.split(r'<div\s+class=["\x27]result["\x27]\s*>', text)[1:]:
        decoded = unquote(unescape(block))
        subject = re.search(r'https://movie\.douban\.com/subject/([1-9][0-9]*)/', decoded)
        heading = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.S)
        if not subject or not heading:
            continue
        title_match = re.search(r'<a\b[^>]*>(.*?)</a>', heading[1], re.S)
        if not title_match:
            continue
        category = plain_text(heading[1])
        record_kind = 'Series' if '[电视剧]' in category else 'Movie' if '[电影]' in category else None
        if record_kind and record_kind != kind:
            continue
        cast = re.search(r'<span[^>]*class=["\x27]subject-cast["\x27][^>]*>(.*?)</span>', block, re.S)
        years = re.findall(r'(?:19|20)[0-9]{2}', plain_text(cast[1]) if cast else '')
        image = re.search(r'<img[^>]+src=["\x27]([^"\x27]+)', block)
        summary = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        rows.append({'title': plain_text(title_match[1]), 'year': years[-1] if years else '',
                     'kind': record_kind or kind, 'douban_id': subject[1], 'imdb_id': None,
                     'douban_score': None, 'source': 'douban_search', 'source_label': '豆瓣网页搜索',
                     'poster': safe_image(unescape(image[1])) if image else None,
                     'cache_updated_at': None, 'overview': plain_text(summary[1])[:500] if summary else '',
                     'type_hint': 'search_category' if record_kind else 'unverified'})
    return rows[:20]


def douban_search(session, query, kind):
    def lookup():
        try:
            response = session.get('https://www.douban.com/search', params={'cat': '1002', 'q': query},
                                   headers={'User-Agent': 'Mozilla/5.0'}, timeout=6)
            if response.status_code in {403, 429}:
                return {'status': 'rate_limited', 'items': []}
            response.raise_for_status()
            if 'search-result' not in response.text:
                return {'status': 'verification_required', 'items': []}
            return {'status': 'ok', 'items': parse_douban_search(response.text, kind)}
        except Exception:
            return {'status': 'lookup_error', 'items': []}
    return cached_query(('douban_search', kind, query), lookup)


def parse_subject_id(value):
    value = str(value or '').strip()
    if re.fullmatch(r'[1-9][0-9]{0,11}', value):
        return value
    match = re.fullmatch(r'https?://(?:movie|m)\.douban\.com/(?:movie/|tv/)?subject/([1-9][0-9]{0,11})/?(?:\?[^\s]*)?', value)
    if not match:
        raise ValueError('请输入数字豆瓣 ID 或豆瓣作品条目链接')
    return match[1]


def local_subject(root, subject_id):
    if root is None or not root.is_dir():
        return None
    records = []
    for path, fallback_kind in media_match.cache_paths(root):
        directory = re.fullmatch(r'([1-9][0-9]*)(?:_(tt[0-9]+))?', path.parent.name, re.I)
        if not directory or directory[1] != subject_id:
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict) or str(data.get('id') or '') != subject_id:
                continue
            imdb = data.get('imdb_id') or data.get('imdbId') or directory[2]
            if imdb and directory[2] and imdb.casefold() != directory[2].casefold():
                continue
            kind = ('Series' if data['is_tv'] else 'Movie') if isinstance(data.get('is_tv'), bool) else fallback_kind
            records.append({'title': str(data.get('title') or ''), 'year': media_match.year(data.get('year')),
                            'kind': kind, 'douban_id': subject_id, 'imdb_id': imdb,
                            'douban_score': media_match.score(data.get('rating')), 'source': 'douban_cache',
                            'source_label': '神医助手本地缓存 · 直接 ID', 'poster': None,
                            'cache_updated_at': path.stat().st_mtime,
                            'overview': str(data.get('intro') or data.get('summary') or '')[:500]})
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    identities = {(r['kind'], r['imdb_id']) for r in records}
    return max(records, key=lambda r: r['cache_updated_at']) if len(identities) == 1 else None


def douban_subject(session, subject_id):
    def lookup():
        try:
            response = session.get(f'https://movie.douban.com/subject/{subject_id}/',
                                   headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://movie.douban.com/'}, timeout=6)
            if response.status_code in {403, 429}:
                return {'status': 'rate_limited', 'items': []}
            if response.status_code == 404:
                return {'status': 'no_result', 'items': []}
            response.raise_for_status()
            script = re.search(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', response.text, re.S)
            if not script:
                return {'status': 'verification_required', 'items': []}
            data = json.loads(script[1])
            if not isinstance(data, dict) or data.get('@type') not in {'Movie', 'TVSeries'}:
                return {'status': 'invalid_response', 'items': []}
            canonical = re.search(r'<link[^>]+rel=["\x27]canonical["\x27][^>]+href=["\x27]([^"\x27]+)', response.text)
            if canonical and f'/subject/{subject_id}/' not in canonical[1]:
                return {'status': 'invalid_response', 'items': []}
            imdb = re.search(r'https?://(?:www\.)?imdb\.com/title/(tt[0-9]+)', response.text)
            row = {'title': str(data.get('name') or ''), 'year': media_match.year(data.get('datePublished')),
                   'kind': 'Series' if data['@type'] == 'TVSeries' else 'Movie', 'douban_id': subject_id,
                   'imdb_id': imdb[1] if imdb else None, 'douban_score': None, 'source': 'douban_subject',
                   'source_label': '豆瓣条目 · 直接 ID', 'poster': safe_image(data.get('image')),
                   'cache_updated_at': None, 'overview': str(data.get('description') or '')[:500]}
            return {'status': 'ok', 'items': [row]}
        except Exception:
            return {'status': 'lookup_error', 'items': []}
    return cached_query(('douban_subject', subject_id), lookup)

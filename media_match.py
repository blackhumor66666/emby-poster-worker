"""Search candidate identities without inferring cross-provider IDs from titles."""
import json
import re
from pathlib import Path
import rating_policy


def year(value):
    match = re.search(r'(?:19|20)[0-9]{2}', str(value or ''))
    return match.group(0) if match else ''


def score(value):
    value = value.get('value') if isinstance(value, dict) else value
    number = rating_policy.decimal_score(value)
    return f'{number:.1f}' if number is not None else None


def cache_paths(root):
    seen = set()
    for base in (root, root / 'cache'):
        for category, filename, kind in [('douban-tv','series.json','Series'),('douban-movies','all.json','Movie'),('','series.json','Series'),('','all.json','Movie')]:
            for path in (base / category).glob(f'*/{filename}'):
                if path not in seen:
                    seen.add(path)
                    yield path, kind


def search_local(root, query, kind, production_year, limit=30):
    if root is None or not root.is_dir() or kind not in {'Movie','Series'}:
        return []
    needle = rating_policy.normalized_title(query)
    if len(needle) < 2:
        return []
    records = {}
    for path, record_kind in cache_paths(root):
        match = re.fullmatch(r'([1-9][0-9]*)(?:_(tt[0-9]+))?', path.parent.name, re.I)
        if not match:
            continue
        try:
            payload = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(payload, dict) or str(payload.get('id')) != match[1]:
                continue
            payload_kind = ('Series' if payload['is_tv'] else 'Movie') if isinstance(payload.get('is_tv'), bool) else record_kind
            if payload_kind != kind:
                continue
            embedded = str(payload.get('imdb_id') or payload.get('imdbId') or '')
            if embedded and match[2] and embedded.casefold() != match[2].casefold():
                continue
            title = str(payload.get('title') or '')
            if needle not in rating_policy.normalized_title(title):
                continue
            imdb = match[2] or (embedded if re.fullmatch(r'tt[0-9]+',embedded,re.I) else None)
            row = {'title':title,'year':year(payload.get('year')),'kind':kind,'douban_id':match[1],
                   'imdb_id':imdb,'douban_score':score(payload.get('rating')),'source':'douban_cache',
                   'source_label':'神医助手本地缓存','poster':None,'cache_updated_at':path.stat().st_mtime,
                   'overview':str(payload.get('intro') or payload.get('summary') or '')[:500]}
            key = (row['douban_id'],row['imdb_id'])
            if key not in records or records[key]['cache_updated_at'] < row['cache_updated_at']:
                records[key] = row
        except (OSError,ValueError,TypeError):
            continue
    return sort_candidates(list(records.values()),query,production_year)[:limit]


def sort_candidates(rows, query, production_year):
    needle = rating_policy.normalized_title(query)
    target = year(production_year)
    return sorted(rows,key=lambda r:(rating_policy.normalized_title(r['title']) != needle,
                                    abs(int(r['year'])-int(target)) if r.get('year') and target else (10000 if target else 0),
                                    not bool(r.get('douban_id') and r.get('imdb_id')),
                                    r['title'],r.get('imdb_id') or ''))


def merge_omdb(local, payload, kind, query, production_year, limit=30):
    rows = [dict(row) for row in local]
    by_imdb = {}
    for row in rows:
        if row.get('imdb_id'):
            by_imdb.setdefault(row['imdb_id'].casefold(),[]).append(row)
    records = (payload or {}).get('Search') or []
    if not isinstance(records,list):
        records = []
    for record in records[:20]:
        if not isinstance(record,dict) or record.get('Type') != ('series' if kind == 'Series' else 'movie'):
            continue
        imdb = str(record.get('imdbID') or '')
        if not re.fullmatch(r'tt[0-9]+',imdb,re.I):
            continue
        poster = str(record.get('Poster') or '')
        poster = poster if poster.startswith('https://') else None
        matches = by_imdb.get(imdb.casefold(),[])
        # A shared series IMDb id can correspond to multiple Douban seasons.
        # Do not arbitrarily merge into the first one.
        if len({r['douban_id'] for r in matches}) == 1:
            matches[0]['source_label'] = '本地豆瓣缓存 + IMDb' if matches[0].get('douban_id') else 'TMDB + IMDb'
            matches[0]['source'] = 'douban_cache+omdb' if matches[0].get('douban_id') else 'emby_tmdb+omdb'
            matches[0]['poster'] = poster
            continue
        if any(r.get('source') == 'omdb' and r.get('imdb_id') == imdb for r in rows):
            continue
        rows.append({'title':str(record.get('Title') or ''),'year':year(record.get('Year')),'kind':kind,
                     'douban_id':None,'imdb_id':imdb,'douban_score':None,'source':'omdb',
                     'source_label':'OMDb IMDb','poster':poster,'cache_updated_at':None,'overview':''})
    return sort_candidates(rows,query,production_year)[:limit]


def effective_ids(item, override):
    ids = {str(k).casefold():str(v) for k,v in (item.get('ProviderIds') or {}).items() if v}
    for source in ('douban','imdb'):
        key = source+'_id'
        if key in override:
            if override[key]:
                ids[source] = str(override[key])
            else:
                ids.pop(source,None)
    return {'douban':ids.get('douban'),'imdb':ids.get('imdb')}


def id_gaps(item, override):
    ids = effective_ids(item,override)
    douban = bool(ids['douban'] and re.fullmatch(r'[1-9][0-9]*',ids['douban']))
    imdb = bool(ids['imdb'] and re.fullmatch(r'tt[0-9]+',ids['imdb'],re.I))
    return [] if douban and imdb else ['missing_both_ids' if not douban and not imdb else 'missing_douban_id' if not douban else 'missing_imdb_id']


def planned_ids(item, override, candidate):
    current = effective_ids(item,override)
    incoming = {'douban':candidate.get('douban_id'),'imdb':candidate.get('imdb_id')}
    # Keep a missing counterpart only when a known, unchanged provider id
    # proves continuity. Otherwise explicitly clear the old counterpart.
    continuity = any(incoming[k] and current[k] == incoming[k] for k in incoming)
    return {k:incoming[k] or (current[k] if continuity else None) for k in incoming}

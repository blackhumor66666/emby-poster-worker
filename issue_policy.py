"""Classify library diagnostics by actionable work, waiting and reference data."""
import hashlib
import json
from datetime import datetime,timezone

CONFLICTS={'identity_mismatch','media_type_mismatch','imdb_id_mismatch','douban_id_mismatch','cache_conflict'}
TRANSIENT={'lookup_error','http_error','network_error','rate_limited','circuit_open','temporarily_unavailable','temporarily_missing'}
LABELS={'identity_conflict':'作品或季别错配','missing_identity':'作品尚未匹配','poster_score_stale':'封面评分未同步',
        'poster_missing':'封面尚未生成','provider_config':'评分源配置需处理','provider_wait':'等待评分源恢复',
        'new_item':'新入库，等待评分','cache_wait':'等待豆瓣缓存','no_score':'暂无可用评分','cache_old':'豆瓣缓存较旧',
        'cache_blocked':'缓存尚未就绪','cache_wait':'等待上游缓存','optional_douban':'封面已更新，可补充豆瓣','background_pending':'已处理，后台更新中','background_failed':'后台封面任务失败','season_artwork':'季海报未配置','metadata_review':'媒体信息需核对','partial_identity':'可补充作品 ID','manual_score':'使用人工固定评分'}


def timestamp(value):
    if isinstance(value,(int,float)):return float(value)
    if not value:return None
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError,TypeError,OverflowError):return None



def classify(item,state,result,gaps,raw_codes,now,grace_hours=48,decisions=None,work=None,handled=False):
    scores=state.get('ratings') or {}
    details=state.get('rating_details') or {}
    created=timestamp(item.get('DateCreated'))
    # Do not infer a new arrival from a metadata update or a recent redraw.
    waiting_new=created is not None and 0<=now-created<grace_hours*3600
    issues=[]
    def add(code,bucket,description,action=None):
        signature=hashlib.sha256(json.dumps({'code':code,'ids':item.get('ProviderIds') or {},'binding':state.get('rating_identity'),'reason':result.get('status'),'cache_title':result.get('cache_title'),'cache_year':result.get('cache_year'),'scores':scores if code=='poster_score_stale' else None},sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
        decision=(decisions or {}).get(code) or {}
        hidden=decision.get('fingerprint')==signature and (decision.get('mode')=='dismiss' or float(decision.get('until') or 0)>now)
        issues.append({'code':code,'label':LABELS[code],'bucket':'ignored' if hidden else bucket,'normal_bucket':bucket,
                       'description':description,'action':action,'fingerprint':signature})
    is_season=item.get('Type')=='Season'
    if result.get('status') in CONFLICTS:
        add('identity_conflict','info' if is_season else 'action','请在 Emby 的季元数据中核对独立豆瓣 ID；整剧 ID 不能替代其他季。' if is_season else '缓存对应的作品、季别或外部 ID 有冲突。重新匹配正确作品后更新封面。',None if is_season else 'match')
    has_artwork = bool((item.get('ImageTags') or {}).get('Primary') or item.get('_has_original'))
    if 'douban_stale' in raw_codes:
        add('poster_score_stale',('waiting' if has_artwork else 'info') if is_season else 'action','可信评分来源已经改变，可以重新读取评分并更新封面。','refresh')
    if is_season and 'poster_missing' in raw_codes and not (item.get('ImageTags') or {}).get('Primary') and not item.get('_has_original'):
        add('season_artwork','info','Emby 的这一季没有独立海报，暂不生成评分季海报；不计入需要操作。')
    elif 'poster_missing' in raw_codes:
        add('poster_missing','waiting' if waiting_new or is_season else 'action','新入库后将在扫描中生成封面。' if waiting_new else '尝试生成封面；没有原图时需先补海报。','refresh')
    for source in ('IMDb','RT'):
        status=(details.get(source) or {}).get('status')
        if status=='invalid_api_key':
            if not any(i['code']=='provider_config' for i in issues):add('provider_config','action','评分源凭据无效。在数据来源中检查和更新 Key。','sources')
        elif status in TRANSIENT:
            if not any(i['code']=='provider_wait' for i in issues):add('provider_wait','waiting','上游暂不可用，系统按现有间隔重试。已有可信评分继续保留。')
    missing_douban=not scores.get('豆瓣')
    has_douban_id=bool(next((v for k,v in (item.get('ProviderIds') or {}).items() if str(k).casefold()=='douban'), None)) or bool(result.get('douban_id'))
    if waiting_new and (missing_douban or not scores.get('IMDb')) and not any(i['code']=='identity_conflict' for i in issues):
        add('new_item','waiting',f'入库未满 {grace_hours} 小时，先等待元数据和评分缓存构建；超过等待期仍缺失再处理。')
    elif is_season and missing_douban:
        add('no_score','info','这一季暂无经核实的独立评分。季表由神医助手生成，也可在 Emby 季元数据中填写独立豆瓣 ID。')
    elif gaps and not has_douban_id and missing_douban and result.get('status') not in CONFLICTS:
        add('missing_identity','action','搜索并确认对应作品，补齐能核实的 ID；未找到可稍后处理或标为已知缺失。','match')
    elif missing_douban and result.get('status') not in CONFLICTS:
        if result.get('status') in {'cache_unavailable','missing_cache_file','invalid_json','negative_placeholder'}:
            add('cache_wait','waiting','作品 ID 已有，等待神医助手生成缓存；可重新读取本地评分。反复匹配不会生成缓存。','refresh')
        elif result.get('status') == 'ok':add('cache_wait','waiting','可信豆瓣评分已读取，等待封面任务应用。','refresh')
        elif result.get('status') == 'json_without_rating':add('no_score','info','豆瓣缓存已生成，缓存尚无可用评分；保持为空，后续按来源更新。')
        else:add('no_score','info','当前来源未返回评分，保持为空。这不证明平台没有评分。')
    elif gaps and not is_season:
        add('partial_identity','info','已有可用评分，补充 ID 属于可选维护，不计入待修复。','match')
    if result.get('cache_age_days',0) and result['cache_age_days']>90:
        add('cache_old','info','继续展示最后已知评分。Worker 重读缓存不会更新豆瓣分数；必要时在神医助手中单条更新。')
    if 'media_issue' in raw_codes:
        add('metadata_review','info','名称和媒体流信息有差异，需要人工核对；不能据此判断资源损坏。')
    if any((details.get(s) or {}).get('source')=='rating_overrides' for s in ('豆瓣','IMDb','RT')):
        add('manual_score','info','人工固定分优先。确认自动来源正确后，可逐项切回自动评分。','details')
    # Submitted work is not a request for another human click. Keep identity
    # conflicts visible; move only work already handed to the background queue.
    work = work or {}
    automated = {'poster_missing', 'poster_score_stale', 'cache_wait'}
    if handled:
        automated.add('missing_identity')
    unresolved = [i for i in issues if i['code'] in automated and i['bucket'] in {'action', 'waiting'}]
    if work.get('action') == 'render' and work.get('status') == 'failed':
        if unresolved:
            issues = [i for i in issues if i not in unresolved]
            add('background_failed','action','已保存的匹配仍保留。'+str(work.get('error') or '封面更新失败，请检查后重试。'),'refresh')
    elif (handled or work.get('status') in {'queued','running','succeeded'}) and work.get('action','render') == 'render':
        if unresolved or work.get('status') in {'queued','running'}:
            issues = [i for i in issues if i not in unresolved]
            message = ('作品匹配已保存；豆瓣缓存已就绪，后台将自动应用到封面。' if result.get('status') == 'ok'
                       else '作品匹配已保存，等待神医助手缓存；缓存到达后自动更新封面，无需再次匹配。')
            if work.get('status') in {'queued','running'}:
                message = '已提交后台队列，正在排队或更新封面；可以继续处理下一部。'
            active = work.get('status') in {'queued','running'}
            binding_at = timestamp(item.get('_binding_at')) or work.get('created_at')
            waited = max(0, now - float(binding_at)) if binding_at else 0
            if not active and result.get('status') in {'missing_cache_file','negative_placeholder','cache_unavailable','invalid_json'}:
                if not has_douban_id:
                    add('optional_douban','info','已保存当前作品关联，封面任务已完成；尚未确认豆瓣 ID，不存在正在执行的豆瓣任务。可按需补充豆瓣条目。','match')
                elif waited >= 6*3600 or result.get('status') in {'cache_unavailable','invalid_json'}:
                    add('cache_blocked','info','作品关联已保存，封面任务已结束；上游豆瓣缓存仍不可用，不是 Worker 排队。请在神医助手检查该条目的缓存；缓存到达后仍会自动应用。','refresh')
                else:
                    add('cache_wait','waiting','封面任务已结束，正在等待神医助手生成豆瓣缓存（不是封面任务排队）。Worker 每30秒检查本地缓存；超过6小时仍未就绪将转入参考。','refresh')
            else:
                add('background_pending','waiting',message)
    priority={'action':0,'waiting':1,'info':2,'ignored':3}
    issues.sort(key=lambda i:priority[i['bucket']])
    return {'status':issues[0]['bucket'] if issues else 'healthy','issues':issues,
            'new_arrival':waiting_new,'date_created':item.get('DateCreated'),'grace_hours':grace_hours,
            'counts':{key:sum(i['bucket']==key for i in issues) for key in priority}}


DOUBAN_GAP_REASONS = {
    'missing_identity': '豆瓣 ID 未确认',
    'missing_cache': '上游缓存未生成',
    'unrated_cache': '缓存没有有效评分',
    'ready_to_apply': '已有评分，待封面应用',
    'identity_conflict': '作品身份或类型冲突',
    'season_mapping': '缺少独立季映射',
    'source_unavailable': '缓存不可用或读取异常',
    'unavailable': '暂无可用评分',
}


def douban_gap(item, state, result, ids, triage, override=None):
    """Coverage is independent of triage urgency. An ignored unrelated issue
    must not hide an unscored source, and expired fingerprints are not ignored.
    """
    import rating_policy
    if rating_policy.decimal_score((state.get('ratings') or {}).get('豆瓣')) is not None:
        return None
    status = result.get('status')
    manual = rating_policy.decimal_score((override or {}).get('douban'))
    if status in CONFLICTS:
        reason = 'identity_conflict'
        description = '缓存对应的作品、版本或类型未通过身份核验，不能应用该评分。'
        action = 'match'
    elif manual is not None or result.get('value'):
        reason = 'ready_to_apply'
        description = '已有可用人工评分或可信缓存，但尚未应用到当前封面。'
        action = 'refresh'
    elif status in {'missing_season_mapping','unverified_season_parent','unverified_season_mapping'}:
        reason = 'season_mapping'
        description = '这一季没有经过核实的独立条目映射；整剧评分不会直接替代其他季。'
        action = 'details'
    elif status == 'json_without_rating':
        reason = 'unrated_cache'
        description = '缓存文件存在，但未包含有效豆瓣分。缓存未更新不等于豆瓣网站没有评分；请在神医助手检查或刷新该条目。'
        action = 'details'
    elif status in {'cache_unavailable','invalid_json','invalid_cache_path'}:
        reason = 'source_unavailable'
        description = '本地缓存不可用或内容异常，请核对挂载和上游缓存。'
        action = 'details'
    elif not ids.get('douban') and not result.get('douban_id'):
        reason = 'missing_identity'
        description = '尚未确认可核实的豆瓣条目 ID；可匹配作品或直接输入豆瓣 ID。'
        action = 'match'
    elif status in {'missing_cache_file','negative_placeholder'}:
        reason = 'missing_cache'
        description = '豆瓣 ID 已保存，但本地还没有对应的可用缓存。重绘不会生成神医助手缓存；缓存到达后会自动读取。'
        action = 'details'
    else:
        reason = 'unavailable'
        description = '当前读取记录没有可用豆瓣分，保持为空；请在详情查看具体来源状态。'
        action = 'details'
    relevant = {'missing_identity','identity_conflict','cache_wait','cache_blocked','no_score',
                'optional_douban','background_pending','background_failed','poster_score_stale'}
    issues = triage.get('issues') or []
    source_issues = [i for i in issues if i.get('code') in relevant]
    ignored = bool(source_issues) and all(i.get('bucket') == 'ignored' for i in source_issues)
    return {'reason':reason, 'label':DOUBAN_GAP_REASONS[reason], 'description':description,
            'action':action, 'ignored':ignored, 'scope':'seasons' if item.get('Type')=='Season' else 'works',
            'cache_status':status, 'cache_updated_at':result.get('cache_updated_at')}


def douban_gap_counts(rows):
    result = {'works':0, 'seasons':0, 'ignored_works':0, 'ignored_seasons':0,
              'reasons':{'works':{}, 'seasons':{}}}
    for row in rows:
        gap = row.get('douban_gap')
        if not gap:
            continue
        scope = gap['scope']
        if gap['ignored']:
            result['ignored_'+scope] += 1
        else:
            result[scope] += 1
            reasons = result['reasons'][scope]
            reasons[gap['reason']] = reasons.get(gap['reason'], 0) + 1
    return result

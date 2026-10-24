import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import media_match
import rating_review

class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.patches=[patch.object(app,'CACHE_DIR',self.root),patch.object(app,'DOUBAN_CACHE_DIR',self.root/'douban'),patch.object(app,'RATING_OVERRIDES_FILE',self.root/'overrides.json'),patch.object(app,'_RATING_OVERRIDES_MTIME',-1),patch.object(app,'_RATING_OVERRIDES_CACHE',{}),patch.object(app,'OMDB_API_KEY','test'),patch.object(app,'_scan_is_busy',return_value=False),patch.object(app,'_start_item_render',return_value=True),patch.object(app.search_sources,'gather',return_value=({'status':'ok','items':[]},{'status':'ok','items':[]}))]
        for p in self.patches:p.start()
        app.MATCH_CANDIDATES.clear()
        self.item={'Id':'item1','Name':'Show','Type':'Series','ProductionYear':2020,'ProviderIds':{}}
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        app.MATCH_CANDIDATES.clear();self.temp.cleanup()
    def cache(self,did='123',imdb='tt123',title='Show',year=2020,score='8.1'):
        folder=app.DOUBAN_CACHE_DIR/'douban-tv'/(did+('_'+imdb if imdb else ''));folder.mkdir(parents=True,exist_ok=True)
        (folder/'series.json').write_text(json.dumps({'id':did,'title':title,'year':year,'is_tv':True,'rating':{'value':score}}))
    def search(self):
        with patch.object(app,'_omdb_cached_request',return_value={'Response':'False'}):
            return app._match_search(self.item,{'query':'Show','kind':'Series','year':'2020'})['items']
    def preview(self,candidate):
        payload={'Response':'True','Type':'series','imdbID':candidate.get('imdb_id'),'imdbRating':'7.2','Ratings':[]}
        with patch.object(app,'_omdb_cached_request',return_value=payload):
            return app._match_preview(self.item,{'token':candidate['token']})
    def test_confirm_defaults_to_write_back_to_emby(self):
        self.cache(); candidate=self.search()[0]; self.preview(candidate)
        with patch.object(app,'_sync_match_to_emby',return_value=self.item) as sync:
            result=app._match_confirm(self.item,{'token':candidate['token'],'confirmed':True})
        sync.assert_called_once_with(self.item,{'douban':'123','imdb':'tt123'})
        self.assertTrue(result['sync_emby'])

    def test_single_candidate_has_both_ids(self):
        self.cache();candidate=self.search()[0]
        self.assertEqual((candidate['douban_id'],candidate['imdb_id']),('123','tt123'))
        preview=self.preview(candidate);self.assertEqual(preview['automatic_ratings'],{'豆瓣':'8.1','IMDb':'7.2'})
        with patch.object(app,'_sync_match_to_emby') as sync:
            result=app._match_confirm(self.item,{'token':candidate['token'],'confirmed':True,'sync_emby':False})
        sync.assert_not_called();self.assertTrue(result['queued'])
        self.assertEqual(media_match.effective_ids(self.item,app._rating_override(self.item)),{'douban':'123','imdb':'tt123'})
    def test_confirmation_requires_preview(self):
        self.cache();c=self.search()[0]
        with self.assertRaises(ValueError):app._match_confirm(self.item,{'token':c['token'],'confirmed':True,'sync_emby':False})
    def test_token_cannot_be_used_on_another_resource(self):
        self.cache();c=self.search()[0]
        with self.assertRaises(ValueError):app._selected_candidate({**self.item,'Id':'other'},c['token'])
    def test_stale_ids_reject_confirmation(self):
        self.cache();c=self.search()[0];self.preview(c)
        with self.assertRaises(ValueError):app._match_confirm({**self.item,'ProviderIds':{'Imdb':'tt456'}},{'token':c['token'],'confirmed':True,'sync_emby':False})
    def test_expired_token_rejected(self):
        self.cache();c=self.search()[0];app.MATCH_CANDIDATES[c['token']]['expires_at']=0
        with self.assertRaises(ValueError):self.preview(c)
    def test_fixed_scores_kept_by_default(self):
        app._update_rating_override('item1',{'douban':'7.0','imdb':'7.3','reason':'verified'})
        self.cache();c=self.search()[0];self.preview(c)
        result=app._match_confirm(self.item,{'token':c['token'],'confirmed':True,'sync_emby':False})
        self.assertEqual(result['override']['douban'],'7.0');self.assertEqual(result['override']['imdb'],'7.3')
    def test_only_selected_fixed_score_cleared(self):
        app._update_rating_override('item1',{'douban':'7.0','imdb':'7.3'})
        self.cache();c=self.search()[0];self.preview(c)
        result=app._match_confirm(self.item,{'token':c['token'],'confirmed':True,'sync_emby':False,'score_modes':{'douban':'automatic'}})
        self.assertNotIn('douban',result['override']);self.assertEqual(result['override']['imdb'],'7.3')
    def test_manual_score_save_preserves_binding_and_other_modes(self):
        self.cache();c=self.search()[0];self.preview(c);app._match_confirm(self.item,{'token':c['token'],'confirmed':True,'sync_emby':False})
        app._update_rating_override('item1',{'imdb':'7.4','score_modes':{'imdb':'manual'}})
        result=app._update_rating_override('item1',{'douban':'8.2','score_modes':{'douban':'manual'}})
        self.assertEqual(result['imdb'],'7.4');self.assertEqual(result['douban_id'],'123');self.assertIn('match',result)
    def test_manual_mode_without_value_invalid(self):
        with self.assertRaises(ValueError):app._update_rating_override('item1',{'score_modes':{'douban':'manual'}})
    def test_changing_one_id_clears_unverified_counterpart(self):
        item={**self.item,'ProviderIds':{'Douban':'321','Imdb':'tt321'}}
        ids=media_match.planned_ids(item,{}, {'douban_id':None,'imdb_id':'tt999'})
        self.assertEqual(ids,{'douban':None,'imdb':'tt999'})
        ids=media_match.planned_ids(item,{}, {'douban_id':None,'imdb_id':'tt321'})
        self.assertEqual(ids['douban'],'321')
    def test_null_binding_does_not_revive_old_ids(self):
        item={**self.item,'ProviderIds':{'Douban':'321','Imdb':'tt321'}}
        override={'match':{'title':'New'},'douban_id':None,'imdb_id':'tt999'}
        with patch.object(app,'_rating_override',return_value=override):
            self.assertEqual(app._douban_cache_result(item)['status'],'cache_unavailable')
        self.assertEqual(media_match.effective_ids(item,override),{'douban':None,'imdb':'tt999'})
    def test_null_imdb_not_reused_for_external_scores(self):
        item={**self.item,'ProviderIds':{'Imdb':'tt321'}}
        with patch.object(app,'_rating_override',return_value={'match':{'title':'New'},'imdb_id':None}):
            self.assertEqual(app._find_omdb_id_info(item),(None,'unmatched_provider_id'))
    def test_same_name_is_not_enough_to_join_sources(self):
        self.cache();local=media_match.search_local(app.DOUBAN_CACHE_DIR,'Show','Series',2020)
        rows=media_match.merge_omdb(local,{'Search':[{'Title':'Show','Year':'2020','Type':'series','imdbID':'tt999'}]},'Series','Show',2020)
        self.assertEqual(len(rows),2);self.assertIsNone(next(r for r in rows if r['imdb_id']=='tt999')['douban_id'])
    def test_multiple_seasons_share_imdb_not_arbitrarily_joined(self):
        self.cache();self.cache('456','tt123','Show Season 2',2021)
        rows=media_match.merge_omdb(media_match.search_local(app.DOUBAN_CACHE_DIR,'Show','Series',2020),{'Search':[{'Title':'Show','Year':'2020','Type':'series','imdbID':'tt123'}]},'Series','Show',2020)
        self.assertEqual(len(rows),3)
    def test_bad_directory_embedded_id_is_not_candidate(self):
        self.cache();p=app.DOUBAN_CACHE_DIR/'douban-tv'/'123_tt123'/'series.json';d=json.loads(p.read_text());d['id']='456';p.write_text(json.dumps(d))
        self.assertEqual(self.search(),[])
    def test_episode_candidate_filtered(self):
        rows=media_match.merge_omdb([],{'Search':[{'Title':'Show','Year':'2020','Type':'episode','imdbID':'tt123'}]},'Series','Show',2020)
        self.assertEqual(rows,[])
    def test_preview_imdb_type_mismatch_not_committable(self):
        self.cache();c=self.search()[0]
        with patch.object(app,'_omdb_cached_request',return_value={'Response':'True','Type':'movie','imdbID':'tt123'}):
            with self.assertRaises(ValueError):app._match_preview(self.item,{'token':c['token']})
        self.assertNotIn('previewed_at',app.MATCH_CANDIDATES[c['token']])
    def test_preview_type_conflict_blocks_cache_score(self):
        self.cache(title='Other',year=2005)
        with patch.object(app,'_omdb_cached_request',return_value={'Response':'False'}):
            candidates=app._match_search(self.item,{'query':'Other','kind':'Series','year':''})['items']
        with self.assertRaises(ValueError):self.preview(candidates[0])
    def test_effective_binding_removes_missing_id_diagnostic(self):
        ids={'match':{'title':'Show'},'douban_id':'123','imdb_id':'tt123'}
        self.assertEqual(media_match.id_gaps(self.item,ids),[])
    def test_report_uses_effective_binding(self):
        ids={'match':{'title':'Show'},'douban_id':'123','imdb_id':'tt123'}
        report=rating_review.build_report([self.item],lambda _: {'ratings':{'豆瓣':'8.1'}},lambda _: {'status':'ok'},None,'today',overrides=lambda _:ids)
        self.assertNotIn('missing_external_id',report['counts'])
    def test_emby_sync_preserves_complete_metadata(self):
        full={**self.item,'Overview':'keep','People':[{'Name':'Actor'}],'LockedFields':['Name'],'CommunityRating':8.0}
        refreshed={**full,'ProviderIds':{'Douban':'123','Imdb':'tt123'}}
        calls=[]
        def emby(method,path,**kwargs):
            calls.append((method,path,kwargs))
            if path=='/Users':return Mock(json=lambda:[{'Id':'u','Policy':{'IsAdministrator':True}}])
            if method=='GET':return Mock(json=lambda:full)
            return Mock()
        with patch.object(app,'emby',side_effect=emby),patch.object(app,'_get_item_by_id',return_value=refreshed):
            app._sync_match_to_emby(self.item,{'douban':'123','imdb':'tt123'})
        post=next(c for c in calls if c[0]=='POST')[2]['json']
        self.assertEqual(post['Overview'],'keep');self.assertEqual(post['People'],full['People']);self.assertEqual(post['LockedFields'],['Name'])
    def test_emby_sync_failure_leaves_worker_override_unchanged(self):
        self.cache();c=self.search()[0];self.preview(c)
        with patch.object(app,'_sync_match_to_emby',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):app._match_confirm(self.item,{'token':c['token'],'confirmed':True,'sync_emby':True})
        self.assertEqual(app._rating_override(self.item),{})
    def test_missing_score_is_not_missing_id(self):
        item={**self.item,'ProviderIds':{'Douban':'123','Imdb':'tt123'}}
        with patch.object(app,'_douban_cache_result',return_value={'status':'missing_cache_file'}),patch.object(app,'_safe_item_state',return_value={'ratings':{}}):
            self.assertEqual(app._match_queue_flags(item),['ids_without_score'])
    def test_interactive_omdb_no_retry(self):
        with patch.object(app,'_omdb_wait_for_slot'),patch.object(app,'_omdb_circuit_status',return_value=None),patch.object(app.SESSION,'get',return_value=Mock(status_code=429,json=lambda:{'Response':'False'})) as request:
            result=app._omdb_cached_request('match-request',{'s':'Show'},interactive=True)
        self.assertEqual(request.call_count,1);self.assertEqual(request.call_args.kwargs['timeout'],6)

if __name__=='__main__':unittest.main()


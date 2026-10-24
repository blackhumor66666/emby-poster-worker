from pathlib import Path
import sys
import json
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from collections import Counter
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import douban_source
import rating_policy
import rating_review


class AccuracyTests(unittest.TestCase):
    def test_scores_are_not_extracted_from_arbitrary_text(self):
        for v in [True, False, 'IMDb 8.7', '-8.7', 'nan', '8.7oops', '101%', float('inf')]:
            self.assertIsNone(app._format_imdb(v))
        self.assertEqual(app._format_imdb('8.7/10'), '8.7')
        self.assertIsNone(app._format_rt('-10%'))
        self.assertEqual(app._format_rt(0), '0%')

    def test_dedicated_rt_endpoint_low_percentages(self):
        for v in [0, 1, 7, 10, 100]:
            self.assertEqual(app._format_mdblist_rating('tomatoes', v), f'{v}%')
        self.assertIsNone(app._format_mdblist_rating('tomatoes', 8.6))
        self.assertEqual(app._format_mdblist_rating('tomatoes', '8.6%'), '9%')
        self.assertIsNone(app._format_mdblist_rating('audience', 95))

    def test_omdb_independent_imdb_field(self):
        with patch.object(app, '_omdb_cached_request', return_value={'Response':'True','imdbID':'tt1234','imdbRating':'8.4','Ratings':[]}):
            values,status=app._omdb_rating_values('tt1234')
        self.assertEqual(values, {'IMDb':'8.4'})
        self.assertEqual(status,'ok')

    def test_omdb_id_mismatch_is_rejected(self):
        with patch.object(app, '_omdb_cached_request', return_value={'Response':'True','imdbID':'tt9999','imdbRating':'8.4'}):
            values,status=app._omdb_rating_values('tt1234')
        self.assertEqual(values,{})
        self.assertEqual(status,'identity_mismatch')

    def test_http_success_without_score_is_not_rating_success(self):
        with patch.object(app,'RATING_EXTERNAL_PROVIDER_ORDER',('omdb',)), patch.object(app,'_omdb_rating_values',return_value=({},'ok')), patch.object(app,'_load_json_file',return_value={}):
            values,details=app._external_rating_values({'Type':'Movie'},'tt1234','provider_id',{'IMDb','RT'})
        self.assertEqual(values,{})
        self.assertEqual(details['RT']['status'],'no_rating')
        self.assertEqual(details['IMDb']['status'],'no_rating')

    def test_episode_score_is_not_tv_series_score(self):
        with patch.object(app,'RATING_EXTERNAL_PROVIDER_ORDER',('omdb',)), patch.object(app,'_omdb_rating_values',return_value=({'IMDb':'9.9'},'ok')), patch.object(app,'_load_json_file',return_value={'Type':'episode'}):
            values,details=app._external_rating_values({'Type':'Series'},'tt1234','provider_id',{'IMDb'})
        self.assertEqual(values,{})
        self.assertEqual(details['IMDb']['status'],'identity_mismatch')

    def test_requested_source_not_blocked_by_old_provenance(self):
        item={'Type':'Movie','ProviderIds':{'Imdb':'tt1234'}}
        with patch.object(app,'_rating_override',return_value={}), patch.object(app,'_external_rating_values',return_value=({'RT':'70%'},{'RT':{'status':'ok','source':'mdblist.provider_id'}})) as external:
            d=app.rating_details(item,{'RT'},{'RT':'80%'},{'RT':{'status':'ok','source':'emby.critic_rating'}})
        self.assertEqual(d['values']['RT'],'70%')
        external.assert_called_once()

    def test_transient_failure_retains_last_known_not_fake_refresh(self):
        old={'IMDb':'8.4'};detail={'IMDb':{'status':'ok','source':'mdblist.provider_id','last_success_at':123}}
        with patch.object(app,'_rating_override',return_value={}), patch.object(app,'_find_omdb_id_info',return_value=('tt1234','provider_id')), patch.object(app,'_external_rating_values',return_value=({}, {'IMDb':{'status':'rate_limited','source':'mdblist.provider_id'}})):
            d=app.rating_details({'Type':'Movie'}, {'IMDb'}, old, detail)
        self.assertEqual(d['values']['IMDb'],'8.4')
        self.assertEqual(d['details']['IMDb']['status'],'temporarily_unavailable')
        self.assertEqual(d['details']['IMDb']['last_success_at'],123)

    def test_definitive_no_rating_clears_old_score(self):
        with patch.object(app,'_rating_override',return_value={}), patch.object(app,'_find_omdb_id_info',return_value=('tt1234','provider_id')), patch.object(app,'_external_rating_values',return_value=({}, {'IMDb':{'status':'no_rating','source':'omdb.provider_id'}})):
            d=app.rating_details({}, {'IMDb'}, {'IMDb':'8.4'}, {'IMDb':{'status':'ok','source':'omdb.provider_id'}})
        self.assertNotIn('IMDb',d['values'])

    def test_stats_count_actual_values(self):
        stats=Counter();app.update_rating_stats(stats,{'RT':{'status':'ok'},'IMDb':{'status':'ok'}},{'IMDb':'8.4'})
        self.assertEqual(stats['RT:no_rating'],1)
        self.assertEqual(stats['IMDb:ok'],1)

    def tv(self,root,title='Old Show',year=2013,payload_title='Old Show',payload_year=2013):
        path=Path(root)/'douban-tv'/'12345_tt1234'/'series.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'id':'12345','is_tv':True,'year':str(payload_year),'title':payload_title,'rating':{'value':8.3}}),encoding='utf8')
        return {'Id':'1','Type':'Series','Name':title,'ProductionYear':year,'ProviderIds':{'Douban':'12345','Imdb':'tt1234'}}

    def test_series_json_is_supported_and_timestamp_exposed(self):
        with tempfile.TemporaryDirectory() as root:
            item=self.tv(root);r=douban_source.resolve(Path(root),item)
            self.assertEqual(r['value'],'8.3');self.assertEqual(r['status'],'ok');self.assertIn('cache_updated_at',r)

    def test_different_title_and_large_year_gap_blocks_wrong_season(self):
        with tempfile.TemporaryDirectory() as root:
            item=self.tv(root,title='歌手',year=2013,payload_title='歌手·当打之年',payload_year=2020)
            self.assertEqual(douban_source.resolve(Path(root),item)['status'],'identity_mismatch')

    def test_one_year_distribution_difference_is_review_not_block(self):
        with tempfile.TemporaryDirectory() as root:
            item=self.tv(root,year=2013,payload_year=2012)
            r=douban_source.resolve(Path(root),item)
            self.assertEqual(r['status'],'ok');self.assertIn('year_difference',r['identity_warnings'])

    def test_series_not_mapped_to_movie_cache(self):
        item={'Type':'Series'}
        self.assertTrue(rating_policy.cache_identity_review(item,{'is_tv':False})['blocked'])

    def test_binding_id_beats_emby_id_and_fixed_override_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            item=self.tv(root);item['ProviderIds']['Douban']='99999'
            with patch.object(app,'DOUBAN_CACHE_DIR',Path(root)), patch.object(app,'_rating_override',return_value={'douban_id':'12345'}):
                self.assertEqual(app._douban_cache_result(item)['status'],'ok')
            with patch.object(app,'_rating_override',return_value={'douban':'7.0','reason':'user confirmed'}):
                self.assertEqual(app.rating_details(item,{'豆瓣'})['values']['豆瓣'],'7.0')

    def test_offline_candidates_are_not_automatic_mutations(self):
        with tempfile.TemporaryDirectory() as root:
            item=self.tv(root); item['ProviderIds']={}
            report=rating_review.build_report([item],lambda _: {},lambda _: {'status':'missing_ids'},Path(root),'test')
            self.assertEqual(report['automatic_id_changes'],0)
            self.assertEqual(report['counts']['offline_id_candidate'],1)
            self.assertEqual(report['findings'][0]['suggestions'][0]['douban_id'],'12345')
            self.assertEqual(item['ProviderIds'],{})

    def test_empty_refresh_set_does_not_query_providers(self):
        with patch.object(app,'_external_rating_values') as external:
            d=app.rating_details({},set(),{'IMDb':'7.1'},{'IMDb':{'status':'ok','source':'mdblist.provider_id'}})
        external.assert_not_called(); self.assertEqual(d['values']['IMDb'],'7.1')

    def test_series_media_query_is_scoped_and_cache_invalidates_v1(self):
        with tempfile.TemporaryDirectory() as root, patch.object(app,'CACHE_DIR',Path(root)):
            response=Mock(json=lambda:{'Items':[{'SeriesId':'1','MediaSources':[],'MediaStreams':[]}],'TotalRecordCount':1})
            with patch.object(app,'emby',return_value=response) as request:
                data=app.enrich_series({'Id':'1','Type':'Series'})
            self.assertEqual(request.call_args.kwargs['params']['ParentId'],'1')
            self.assertNotIn('ParentIds',request.call_args.kwargs['params'])
            self.assertEqual(data['_SeriesEpisodeCount'],1)

    def test_unscoped_series_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, patch.object(app,'CACHE_DIR',Path(root)):
            response=Mock(json=lambda:{'Items':[{'SeriesId':'other','MediaStreams':[{'Type':'Video','Width':3840}]}],'TotalRecordCount':1})
            with patch.object(app,'emby',return_value=response):
                data=app.enrich_series({'Id':'1','Type':'Series'})
            self.assertNotIn('MediaStreams',data)
            self.assertFalse((Path(root)/'series-media'/'1.json').exists())

    def test_expired_mdblist_cache_refreshes_even_with_value(self):
        with tempfile.TemporaryDirectory() as root, patch.object(app,'CACHE_DIR',Path(root)):
            app._mdblist_write_values('movie','tt1234','imdb',{'IMDb':'7.4'},'ok')
            file=app._mdblist_cache_file('movie','tt1234');d=json.loads(file.read_text());d['checked_at']['imdb']=1;file.write_text(json.dumps(d))
            with patch.object(app,'_mdblist_fetch_batch',return_value={'status':'rate_limited'}) as lookup:
                app._mdblist_cached_values({'Type':'Movie'},'tt1234',{'IMDb'})
            lookup.assert_called_once()

    def test_douban_conflict_does_not_keep_old_valid_score(self):
        with patch.object(app,'_douban_cache_result',return_value={'status':'identity_mismatch'}),patch.object(app,'DOUBAN_REQUIRE_VERIFIED_CACHE',True):
            self.assertTrue(app.community_rating_changed({}, {'豆瓣':'9.0'}, {'豆瓣':{'source':'douban.local_cache'}}))

    def test_old_identity_missing_new_optional_none_is_compatible(self):
        old = {'type':'Series','douban':'123','imdb':'tt123','override_imdb':None}
        self.assertTrue(app.rating_identity_matches(old, {**old,'override_douban':None}))
        self.assertFalse(app.rating_identity_matches(old, {**old,'override_douban':'456'}))
        self.assertFalse(app.rating_identity_matches({}, {**old,'override_douban':None}))

    def test_stale_media_fallback_keeps_original_timestamp(self):
        with tempfile.TemporaryDirectory() as root, patch.object(app,'CACHE_DIR',Path(root)):
            item = {'Id':'1','Type':'Series'}
            app._write_series_media_cache(item,[],[],5)
            file = Path(root)/'series-media'/'1.json'
            payload = json.loads(file.read_text());payload['fetched_at']=1
            file.write_text(json.dumps(payload));before=file.read_bytes()
            with patch.object(app,'emby',side_effect=app.requests.ConnectionError('offline')):
                self.assertEqual(app.enrich_series(item)['_SeriesEpisodeCount'],5)
            self.assertEqual(file.read_bytes(),before)

    def test_season_uses_mapped_subject_and_not_series_score(self):
        with tempfile.TemporaryDirectory() as root:
            parent = self.tv(root)
            folder=Path(root)/'douban-tv'/'12345_tt1234'
            (folder/'seasons.json').write_text(json.dumps({'2':'67890'}))
            season_dir=folder.parent/'67890';season_dir.mkdir()
            (season_dir/'series.json').write_text(json.dumps({'id':'67890','title':'Test Season 2','year':2024,'is_tv':True,'rating':{'value':8.1}}))
            season={'Id':'s2','Type':'Season','Name':'Season 2','SeriesId':parent['Id'],'IndexNumber':2,'ProviderIds':{}}
            with patch.object(app,'DOUBAN_CACHE_DIR',Path(root)),patch.object(app,'_rating_override',return_value={}),patch.object(app,'_get_item_by_id',return_value=parent):
                result=app._douban_cache_result(season)
                self.assertEqual(result['value'],'8.1');self.assertEqual(result['douban_id'],'67890')
                season['IndexNumber']=3
                self.assertEqual(app._douban_cache_result(season)['status'],'missing_season_mapping')
                season['IndexNumber']=1
                self.assertEqual(app._douban_cache_result(season)['douban_id'],'12345')

    def test_season_does_not_inherit_series_imdb_rt(self):
        item={'Type':'Season','ProviderIds':{'Imdb':'tt1234'},'CriticRating':90}
        with patch.object(app,'_rating_override',return_value={}),patch.object(app,'_external_rating_values') as external,patch.object(app,'CRITIC_RATING_IS_RT',True):
            result=app.rating_details(item,{'IMDb','RT'})
        external.assert_not_called()
        self.assertNotIn('IMDb',result['values']);self.assertNotIn('RT',result['values'])

    def test_season_media_rejects_wrong_season_same_series(self):
        with tempfile.TemporaryDirectory() as root,patch.object(app,'CACHE_DIR',Path(root)):
            response=Mock(json=lambda:{'Items':[{'SeriesId':'p','SeasonId':'s1'}],'TotalRecordCount':1})
            with patch.object(app,'emby',return_value=response):
                result=app.enrich_series({'Type':'Season','Id':'s2'})
            self.assertNotIn('_SeriesEpisodeCount',result)

    def test_transient_provider_failure_not_hidden_by_empty_fallback(self):
        with patch.object(app,'RATING_EXTERNAL_PROVIDER_ORDER',['mdblist','omdb']),patch.object(app,'_mdblist_cached_values',return_value=({}, {'imdb':'rate_limited'})),patch.object(app,'_omdb_rating_values',return_value=({},'ok')):
            values, details=app._external_rating_values({'Type':'Movie'},'tt1234','provider_id',{'IMDb'})
        self.assertEqual(details['IMDb']['status'],'rate_limited')

    def test_corrected_imdb_binding_is_also_used_for_douban_identity(self):
        with tempfile.TemporaryDirectory() as root:
            item=self.tv(root);item['ProviderIds']['Imdb']='tt9999'
            with patch.object(app,'DOUBAN_CACHE_DIR',Path(root)),patch.object(app,'_rating_override',return_value={'imdb_id':'tt1234'}):
                self.assertEqual(app._douban_cache_result(item)['status'],'ok')

    def test_known_wrong_season_blocks_other_automatic_scores_too(self):
        item={'Type':'Series','ProviderIds':{'Imdb':'tt1234'},'CriticRating':90}
        with patch.object(app,'_douban_cache_result',return_value={'status':'identity_mismatch'}),patch.object(app,'_rating_override',return_value={}),patch.object(app,'_external_rating_values') as external,patch.object(app,'CRITIC_RATING_IS_RT',True):
            d=app.rating_details(item,{'IMDb','RT'},{'IMDb':'7.2','RT':'90%'},{'IMDb':{'status':'ok'},'RT':{'status':'ok'}})
        external.assert_not_called();self.assertEqual(d['values'],{})
        self.assertEqual(d['details']['IMDb']['status'],'identity_mismatch')
        with patch.object(app,'_douban_cache_result',return_value={'status':'identity_mismatch'}),patch.object(app,'_rating_override',return_value={'imdb':'8.5','rt':'80%'}):
            d=app.rating_details(item,{'IMDb','RT'})
        self.assertEqual(d['values'],{'IMDb':'8.5','RT':'80%'})

if __name__=='__main__':unittest.main()


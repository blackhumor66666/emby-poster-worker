import json,sys,time,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app,issue_policy,douban_source

class DoubanCoverageTests(unittest.TestCase):
 def gap(self,status='missing_cache_file',score=None,ids=None,issues=None,kind='Movie',override=None,value=None,triage_status='info'):
  return issue_policy.douban_gap({'Id':'1','Type':kind},{'ratings':{'豆瓣':score}if score is not None else{}}, {'status':status,'value':value},ids if ids is not None else{'douban':'123'}, {'status':triage_status,'issues':issues or[]},override)
 def test_reference_cached_without_rating_remains_visible(self):
  gap=self.gap(status='json_without_rating');self.assertEqual(gap['reason'],'unrated_cache');self.assertFalse(gap['ignored']);self.assertEqual(gap['action'],'details')
 def test_no_cache_is_separate_from_cache_without_score(self):
  self.assertEqual(self.gap()['reason'],'missing_cache');self.assertEqual(self.gap(status='json_without_rating')['reason'],'unrated_cache')
 def test_valid_applied_score_is_not_counted_as_missing(self):
  for score in ['8.1',8.1,'8.1/10']:
   self.assertIsNone(self.gap(score=score))
 def test_zero_invalid_scores_are_not_mistaken_for_coverage(self):
  for score in ['0',0,'--','NaN','12']:
   self.assertIsNotNone(self.gap(score=score))
 def test_no_douban_id_is_missing_identity_even_after_render(self):
  self.assertEqual(self.gap(ids={'douban':None,'imdb':'tt123'})['reason'],'missing_identity')
 def test_manual_or_trusted_local_score_waiting_for_render_is_separate(self):
  self.assertEqual(self.gap(status='ok',value='8.1')['reason'],'ready_to_apply');self.assertEqual(self.gap(override={'douban':'7.5'})['reason'],'ready_to_apply')
 def test_conflict_never_applies_untrusted_cached_value(self):
  gap=self.gap(status='identity_mismatch',value='8.1');self.assertEqual(gap['reason'],'identity_conflict')
 def test_season_mapping_counted_separately(self):
  gap=self.gap(status='missing_season_mapping',kind='Season');self.assertEqual(gap['reason'],'season_mapping');self.assertEqual(gap['scope'],'seasons')
 def test_actual_source_ignored_is_excluded_from_default_count(self):
  gap=self.gap(issues=[{'code':'cache_blocked','bucket':'ignored'}]);self.assertTrue(gap['ignored'])
 def test_ignored_unrelated_media_issue_does_not_hide_score_gap(self):
  gap=self.gap(issues=[{'code':'metadata_review','bucket':'ignored'}],triage_status='ignored');self.assertFalse(gap['ignored'])
 def test_stale_ignore_decision_does_not_hide_changed_identity(self):
  item={'Id':'1','Type':'Movie','ProviderIds':{}}
  first=issue_policy.classify(item,{}, {'status':'missing_ids'},['missing_both_ids'],[],time.time())
  issue=first['issues'][0];decision={'missing_identity':{'mode':'dismiss','fingerprint':issue['fingerprint']}}
  changed={**item,'ProviderIds':{'Imdb':'tt999'}}
  triage=issue_policy.classify(changed,{}, {'status':'missing_cache_file'},['missing_douban_id'],[],time.time(),decisions=decision)
  gap=issue_policy.douban_gap(changed,{}, {'status':'missing_cache_file'},{'douban':None},triage)
  self.assertFalse(gap['ignored'])
 def test_aggregates_partition_scopes_and_ignore_independently_of_urgency(self):
  rows=[{'douban_gap':self.gap(status='json_without_rating')},{'douban_gap':self.gap(status='missing_cache_file')},{'douban_gap':self.gap(kind='Season',status='missing_season_mapping')},{'douban_gap':self.gap(issues=[{'code':'no_score','bucket':'ignored'}])},{'douban_gap':None}]
  counts=issue_policy.douban_gap_counts(rows);self.assertEqual(counts['works'],2);self.assertEqual(counts['seasons'],1);self.assertEqual(counts['ignored_works'],1);self.assertEqual(sum(counts['reasons']['works'].values()),2)
 def test_empty_cache_exposes_timestamp_and_zero_votes_without_fabricating_rating(self):
  with tempfile.TemporaryDirectory()as directory:
   root=Path(directory);path=root/'douban-tv'/'123'/'series.json';path.parent.mkdir(parents=True);path.write_text(json.dumps({'id':'123','title':'Show','year':'2026','is_tv':True,'rating':{'value':0,'count':0}}))
   result=douban_source.resolve(root,{'Id':'1','Name':'Show','ProductionYear':2026,'Type':'Series','ProviderIds':{'Douban':'123'}})
   self.assertEqual(result['status'],'json_without_rating');self.assertEqual(result['rating_count'],0);self.assertEqual(result['rating_value'],0);self.assertGreater(result['cache_updated_at'],0);self.assertNotIn('value',result)
 def test_unclassified_missing_row_is_not_silently_dropped(self):
  item={'Id':'x','Type':'Movie','Name':'Example','ProviderIds':{'Douban':'123'}}
  healthy={'status':'healthy','issues':[],'counts':{'action':0,'waiting':0,'info':0,'ignored':0}}
  with patch.object(app,'get_items_cached',return_value=[item]),patch.object(app,'_effective_item_type',side_effect=lambda x:x),patch.object(app,'_safe_item_state',return_value={}),patch.object(app,'_rating_override',return_value={}),patch.object(app,'_item_triage',return_value=healthy),patch.object(app,'_douban_cache_result',return_value={'status':'missing_cache_file'}),patch.object(app,'_library_definitions',return_value=[]),patch.object(app,'_task_state',return_value=None):
   payload=app._build_matching_queue_payload();self.assertEqual(len(payload['items']),1);self.assertEqual(payload['douban_gaps']['works'],1)

if __name__=='__main__':unittest.main()

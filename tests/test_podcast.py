import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('podcast',Path(__file__).parents[1]/'scripts/podcast.py')
p=importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch=patch.multiple(p,CACHE=Path(self.tmp.name)/'cache',OUT=Path(self.tmp.name)/'out')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.config=dict(model='eleven_flash_v2_5',max_credits=100,credit_ceiling_per_character=1)
        self.voices=dict(male='m',female='f')
        self.line=dict(speaker='male',de='Hallo!',en='Hello!')
        self.accounts=[dict(name='A',key='one',remaining=100),dict(name='B',key='two',remaining=100)]

    def test_quota_failover_and_cache(self):
        def api(key,route,body=None):
            if route=='/user/subscription':
                return dict(character_limit=100,character_count=0)
            if key=='one':
                raise p.ProviderError(401,'quota_exceeded')
            return dict(audio_base64='YXVkaW8=')
        with patch.object(p,'request',side_effect=api) as call:
            first=p.synthesize(self.line,self.config,self.voices,self.accounts)
            count=call.call_count
            self.assertEqual(first,p.synthesize(self.line,self.config,self.voices,self.accounts))
            self.assertEqual(call.call_count,count)
        self.assertEqual(self.accounts[0]['remaining'],0)

    def test_ambiguous_failure_blocks_rebilling(self):
        def api(key,route,body=None):
            if body is None:
                return dict(character_limit=100,character_count=0)
            raise RuntimeError('network failure')
        with patch.object(p,'request',side_effect=api):
            with self.assertRaises(RuntimeError):
                p.synthesize(self.line,self.config,self.voices,self.accounts)
        with patch.object(p,'request') as call:
            with self.assertRaises(RuntimeError):
                p.synthesize(self.line,self.config,self.voices,self.accounts)
            call.assert_not_called()

    def test_policy_blocks_do_not_rotate(self):
        def api(key,route,body=None):
            if body is None:
                return dict(character_limit=100,character_count=0)
            raise p.ProviderError(401,'detected_unusual_activity')
        with patch.object(p,'request',side_effect=api) as call:
            with self.assertRaises(p.ProviderError):
                p.synthesize(self.line,self.config,self.voices,self.accounts)
            self.assertEqual(call.call_count,2)

    def test_plan_budget_and_turn_packing(self):
        with self.assertRaises(RuntimeError):
            p.plan([self.line],self.config,self.voices,[dict(name='A',remaining=3),dict(name='B',remaining=3)])
        self.config['max_credits']=5
        with self.assertRaises(RuntimeError):
            p.plan([self.line],self.config,self.voices)

    def test_duplicate_text_charged_only_once(self):
        result=p.plan([self.line,self.line],self.config,self.voices)
        self.assertEqual(result['credit_upper_estimate'],6)

    def test_extra_keys_and_duplicate_secrets(self):
        with patch.dict(os.environ,{'ALVIN':'same','JOHN_SNOW':'same','ELEVENLABS_KEYS_JSON':json.dumps({'NEXT':'other'})},clear=True):
            self.assertEqual(len(p.keys()),2)

    def test_sentence_boundaries_keep_every_sample(self):
        text='Hallo! Wie geht es dir? Gut.'
        alignment=dict(characters=list(text),
                       character_start_times_seconds=[i/10 for i in range(len(text))],
                       character_end_times_seconds=[(i+1)/10 for i in range(len(text))])
        spans=p.sentence_intervals(text,alignment,3.0)
        self.assertEqual(len(spans),3)
        self.assertEqual(spans[0][0],0)
        self.assertEqual(spans[-1][1],3.0)
        self.assertTrue(all(left[1]==right[0] for left,right in zip(spans,spans[1:])))
        with self.assertRaises(RuntimeError):
            p.sentence_intervals(text,None,3.0)

    def test_existing_turn_is_reused_before_sentence_requests(self):
        line=dict(speaker='male',de='Hallo! Wie geht es dir?',en='Hello! How are you?')
        self.assertEqual([u['de'] for u in p.generation_units(line,self.config,self.voices)],['Hallo!','Wie geht es dir?'])
        body,voice=p.payload(line,self.config,self.voices)
        p.atomic(p.CACHE/f'{p.cache_id(body,voice)}.json',{})
        self.assertEqual(p.generation_units(line,self.config,self.voices),[line])

    def test_subtitle_escaping(self):
        self.assertNotIn('{',p.escape(r'{\\an8}Hello'))
        self.assertEqual(p.ass_time(61.25),'0:01:01.25')


if __name__=='__main__':
    unittest.main()

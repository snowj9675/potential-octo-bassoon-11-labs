import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import dialogue_v3 as v3


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.episode=v3.load_script(Path(__file__).parents[1]/'input/v3-learning-german.json')

    def test_one_request_keeps_both_speakers_and_tags(self):
        body=v3.body_for(self.episode,dict(male='m',female='f'))
        self.assertEqual(body['model_id'],'eleven_v3')
        self.assertEqual({x['voice_id'] for x in body['inputs']},{'m','f'})
        self.assertIn('[curious]',body['inputs'][0]['text'])
        self.assertNotIn('voice_settings',body)

    def test_short_translated_phrases_follow_provider_words(self):
        for tagged in (False,True):
            text=' '.join(t['text'] if tagged else v3.untag(t['text']) for t in self.episode['turns'])
            result=dict(alignment=dict(characters=list(text),character_start_times_seconds=[i*.04 for i in range(len(text))],character_end_times_seconds=[(i+1)*.04 for i in range(len(text))]))
            cues=v3.subtitle_cues(self.episode,result,len(text)*.04)
            self.assertEqual(len(cues),17)
            self.assertTrue(all(len(c['de'].split())<=5 and len(c['en'].split())<=6 for c in cues))
            self.assertTrue(all('[' not in c['de'] for c in cues))
            self.assertTrue(all(a['end']<=b['start'] for a,b in zip(cues,cues[1:])))

    def test_misaligned_audio_is_not_silently_guessed(self):
        with self.assertRaises(RuntimeError):
            v3.subtitle_cues(self.episode,dict(alignment=dict(characters=['X'],character_start_times_seconds=[0],character_end_times_seconds=[1])),2)

    def test_caption_mismatch_fails_before_api(self):
        data=copy.deepcopy(self.episode)
        data['turns'][0]['chunks'][0]['de']='Falscher Text.'
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'script.json';p.write_text(json.dumps(data))
            with patch.object(v3.core,'request') as api:
                with self.assertRaises(ValueError):v3.load_script(p)
                api.assert_not_called()

    def test_budget_and_render_only_protect_credits(self):
        body=v3.body_for(self.episode,dict(male='m',female='f'))
        with tempfile.TemporaryDirectory() as tmp, patch.object(v3.core,'CACHE',Path(tmp)/'cache'),patch.object(v3.core,'OUT',Path(tmp)/'out'),patch.object(v3.core,'request') as api:
            with self.assertRaises(RuntimeError):v3.credit_plan(body,dict(credit_ceiling_per_character=1,max_credits=10))
            with self.assertRaises(RuntimeError):v3.generate(body,dict(credit_ceiling_per_character=1),None,'render')
            api.assert_not_called()

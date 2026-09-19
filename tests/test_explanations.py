import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError
import explanations as e

ANSWER={'translation':'An ordinary, unremarkable life.', 'parts':[
    {'japanese':'普通の人生','reading':'ふつうのじんせい','meaning':'An ordinary life.'}],
    'structure':'A noun phrase centered on 人生.'}


class ExplanationTests(unittest.TestCase):
    def test_validation_rejects_unrenderable_answers(self):
        self.assertEqual(e.validate(json.dumps(ANSWER)),ANSWER)
        for value in [{}, {**ANSWER,'parts':[]}, {**ANSWER,'extra':True},
                      {**ANSWER,'parts':[dict(ANSWER['parts'][0],reading='futsuu')]},
                      {**ANSWER,'translation':'x'*2001}]:
            with self.assertRaises(ValidationError):e.validate(json.dumps(value))
        with self.assertRaises(ValidationError):e.validate('```json\n'+json.dumps(ANSWER)+'\n```')

    def test_astra_success_is_saved_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);key='a'*64;(root/key).mkdir()
            with patch.object(e,'astra',return_value=ANSWER),patch.object(e,'gemma') as fallback:
                e.run_job(key,'普通の人生',root)
            fallback.assert_not_called()
            result=e.get(key,root)
            self.assertEqual(result['model'],'gpt-6-astra')
            self.assertEqual(result['answer'],ANSWER)

    def test_failure_uses_gemma_and_persists_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);key='b'*64;(root/key).mkdir()
            with patch.object(e,'astra',side_effect=TimeoutError('Timed out')),patch.object(e,'gemma',return_value=ANSWER):
                e.run_job(key,'普通の人生',root)
            result=e.get(key,root)
            self.assertEqual(result['model'],'gemma4:12b')
            self.assertEqual(result['fallback_reason'],'Timed out')
            self.assertEqual(result['answer'],ANSWER)

    def test_both_fail_never_publish_an_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);key='c'*64;(root/key).mkdir()
            with patch.object(e,'astra',side_effect=ValueError('Invalid JSON')),patch.object(e,'gemma',side_effect=ValueError('Invalid reading')):
                e.run_job(key,'普通の人生',root)
            self.assertEqual(e.get(key,root)['status'],'failed')
            self.assertFalse((root/key/'result.json').exists())

    def test_interrupted_work_and_invalid_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);key='d'*64;(root/key).mkdir()
            e.write_json(root/key/'status.json',{'status':'running'})
            self.assertEqual(e.get(key,root)['status'],'failed')
            with self.assertRaises(ValueError):e.get('../result',root)

    def test_cached_result_does_not_start_another_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(e.POOL,'submit') as submit:
                key=e.start('普通の人生',root)
                self.assertEqual(e.start('普通の人生',root),key)
                submit.assert_called_once()
                e.ACTIVE.discard(key)
                e.write_json(root/key/'result.json',{'status':'ready','answer':ANSWER})
                e.start('普通の人生',root)
                submit.assert_called_once()

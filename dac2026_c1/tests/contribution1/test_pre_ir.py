import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from assumption_modeling.pre_ir import analyze_before_ir
from assumption_modeling.dag_validator import validate_dag
from assumption_modeling.requirements import extract_requirements,validate_requirements
from IRgeneration.ir_generate import gen_petrinet_ir as frontend


class PreIRTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.spec=self.root/'spec.md';self.spec.write_text('clear clears count. load loads data_in.\n')
    def analyze(self,answers=None):return analyze_before_ir(self.spec,self.root/'pre',answers)
    def test_unresolved_dag_retains_open_refinement_exit(self):
        result=self.analyze();self.assertEqual(result['status'],'needs_clarification')
        dag=json.loads(Path(result['state']['paths']['condition_dag']).read_text())['condition_dag']
        self.assertFalse(dag['nodes'][0]['domain_closed']);self.assertIn('other',dag['nodes'][0]['value_domain'])
    def test_answer_resume_and_spec_hash_validation(self):
        first=self.analyze();answer=self.root/'answers.json'
        doc=dict(spec_hash=first['state']['spec_hash'],answers=[dict(question_id='DEC_COLLISION_POLICY',value='clear_first',source='user')]);answer.write_text(json.dumps(doc))
        result=self.analyze(answer);self.assertEqual(result['status'],'ready');self.assertEqual(result['contract']['constraints'][0]['higher_operation'],'clear')
        self.assertNotEqual(first['directory'],result['directory']);self.assertTrue(Path(first['directory']).is_dir())
        self.spec.write_text(self.spec.read_text()+'\nchanged')
        with self.assertRaisesRegex(ValueError,'different specification'):self.analyze(answer)
    def test_explicit_priority_needs_no_answer(self):
        self.spec.write_text(self.spec.read_text()+'clear has priority over load.\n')
        self.assertEqual(self.analyze()['status'],'ready')
    def test_explicit_freedom_is_not_missing_requirement(self):
        self.spec.write_text(self.spec.read_text()+'Priority between clear and load is implementation-defined.\n')
        result=self.analyze();self.assertEqual(result['status'],'ready');self.assertEqual(len(result['contract']['implementation_freedoms']),1)
    def test_conflicting_answer_is_preserved(self):
        self.spec.write_text(self.spec.read_text()+'clear has priority over load.\n');first=self.analyze();answer=self.root/'answers.json'
        answer.write_text(json.dumps(dict(spec_hash=first['state']['spec_hash'],answers=[dict(question_id='DEC_COLLISION_POLICY',value='load_first',source='user')])))
        self.assertEqual(self.analyze(answer)['status'],'evidence_conflict')
    def test_other_requires_typed_refinement(self):
        first=self.analyze();answer=self.root/'answers.json'
        answer.write_text(json.dumps(dict(spec_hash=first['state']['spec_hash'],answers=[dict(question_id='DEC_COLLISION_POLICY',value='other',source='user',behavior='hold')])))
        self.assertEqual(self.analyze(answer)['status'],'needs_clarification')
    def test_cyclic_dangling_and_incomplete_dag_rejected(self):
        node=dict(id='D',kind='decision',domain_closed=False,value_domain=['x','other'],requirement_ids=[])
        for edges in ([dict(source='D',target='D',value='x')],[dict(source='D',target='missing',value='x')]):
            self.assertEqual(validate_dag(dict(nodes=[node],roots=['D'],edges=edges))['status'],'invalid')
        closed=dict(node,domain_closed=True)
        self.assertEqual(validate_dag(dict(nodes=[closed],roots=['D'],edges=[]))['status'],'invalid')
    def test_quotes_and_character_ranges_checked(self):
        text='时钟说明\nclear sets count.\n';reqs=extract_requirements(text)
        self.assertEqual(validate_requirements(reqs,text),[])
        for req in reqs:self.assertEqual(text[req['source']['char_start']:req['source']['char_end']],req['text'])
        reqs[0]['text']='invented quote';self.assertTrue(validate_requirements(reqs,text))
    def test_contract_survives_request_retry_and_saved_prompt(self):
        contract=dict(status='ready',constraints=[dict(id='CON_TEST')])
        response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='module: {}'))],id='x',model='frozen',usage=None)
        with patch.object(frontend,'OpenAI'),patch.object(frontend.llm_stream,'create',return_value=response) as create,patch.object(frontend,'_frontend_contract_issues',side_effect=[['issue'],[]]):
            frontend.call_llm('key','url','model','spec',0,100,generation_contract=contract)
        self.assertEqual(create.call_count,2)
        for call in create.call_args_list:self.assertIn('CON_TEST',json.dumps(call.kwargs['messages']))
        with self.assertRaises(ValueError):frontend.build_messages('spec',dict(status='needs_clarification'))
    def test_yaml_repair_retains_contract(self):
        response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='module: {}'))])
        with patch.object(frontend.llm_stream,'create',return_value=response) as call:
            frontend.repair_yaml(None,'m','bad','err',0,100,dict(constraints=['CON_TEST']))
        self.assertIn('CON_TEST',json.dumps(call.call_args.kwargs['messages']))

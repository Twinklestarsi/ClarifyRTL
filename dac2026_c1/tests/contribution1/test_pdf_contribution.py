"""Acceptance tests for PDF sections 4.1–4.5, including nontrivial counterexamples."""
import copy
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from Compiler.rtl_compiler.compiler import compile_ir
from Compiler.rtl_compiler.parser import parse_ir
from assumption_modeling.analysis import start_analysis,finish_analysis
from assumption_modeling.binding import bind_proposals,merge_requirements
from assumption_modeling.candidates import scan_choices
from assumption_modeling.export_training import export_graph
from assumption_modeling.graph import build_graph
from assumption_modeling.requirements import extract_requirements,validate_requirements,frozen_llm_proposals
from assumption_modeling.schema import digest,envelope,write_json,file_hash
from assumption_modeling.support import review_assumptions
from .test_semantics import FIXTURE,HAS_Z3
HAS_NUMPY=importlib.util.find_spec('numpy') is not None


class ContributionPDFTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.model=parse_ir(FIXTURE)
    def structured(self,text):
        return dict(id='LLM_HOLD',text=text,source=dict(line_start=1,line_end=1),trigger='enable',
            expected_behavior='count retains its previous value',temporal_relation='next_active_clock_edge',
            related_signals=['enable','count'],explicit_freedom=[],scope='normal_operation',
            claims=[dict(kind='hold',trigger='enable',target='count',scope='normal_operation')])
    def test_structured_source_fields_and_example_scope(self):
        text="When load, count receives data_in on the next active clock edge.\nWhen stall, count holds its value.\n## Example\nclear has priority over load.\n"
        requirements=extract_requirements(text)
        self.assertEqual(validate_requirements(requirements,text),[])
        self.assertEqual(requirements[0]['trigger'],'load');self.assertEqual(requirements[0]['expected_behavior']['expression'],'data_in')
        self.assertEqual(requirements[1]['claims'][0]['kind'],'hold')
        self.assertFalse(requirements[-1]['claims'])
        for req in requirements:
            self.assertEqual(text[req['source']['char_start']:req['source']['char_end']],req['text'])
    def test_frozen_extraction_repairs_schema_without_promoting_interpretation(self):
        text='At an enabled edge retain the counter value.';req=self.structured(text)
        def response(body):return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))],id='frozen',model='locked-model',usage=None)
        invalid=copy.deepcopy(req);invalid['text']='fabricated quotation'
        with patch('IRgeneration.llm_stream.create',side_effect=[response(dict(requirements=[invalid],candidates=[])),response(dict(requirements=[req],candidates=[]))]) as create:
            result=frozen_llm_proposals(text,{},api_key='test',base_url='test',model='locked-model',client=object(),signals=['enable','count'])
        self.assertEqual(create.call_count,2);self.assertEqual(result['attempts'],2)
        self.assertEqual(result['proposals']['requirements'][0]['interpretation_status'],'proposed')
        self.assertTrue(all(call.kwargs['temperature']==0 for call in create.call_args_list))
    def test_invalid_proposals_cannot_modify_unrelated_ir(self):
        reqs=extract_requirements('Enable increments count.');rules=scan_choices(self.model,reqs)
        proposals=[dict(id='bad',kind='state_update',target='count',transition='enable_op',action_index=0,
            alternatives=[dict(value='bad',patch=dict(type='assignment_expression',expression='unknown_signal'))])]
        assumptions,validation=bind_proposals(self.model,rules,proposals,reqs)
        self.assertEqual(validation[0]['status'],'rejected');self.assertEqual(len(assumptions),len(rules))
        self.assertEqual(self.model.transition_map()['enable_op'].actions[0].expression,"count + 4'd1")
    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_llm_candidates_enter_graph_and_executable_witness_path(self):
        text='At an enabled edge retain the counter value.';req=self.structured(text);spec=self.root/'spec.md';spec.write_text(text)
        candidate=dict(id='hold-proposal',kind='state_update',target='count',transition='enable_op',action_index=0,
            current_interpretation='increments count',requirement_ids=['LLM_HOLD'],alternatives=[dict(kind='behavior_change',value='decrement instead',
            patch=dict(type='assignment_expression',expression="count - 4'd1"))])
        proposed=dict(proposals=dict(requirements=[dict(req,interpretation_status='proposed',origin='frozen_llm')],candidates=[candidate]),model='frozen',interpretation_state='proposed')
        rtl=self.root/'rtl.v';mp=self.root/'map.json';compile_ir(FIXTURE,rtl,source_map_path=mp)
        with patch('assumption_modeling.analysis.frozen_llm_proposals',return_value=proposed):
            state=start_analysis(ir_path=FIXTURE,rtl_path=rtl,source_map_path=mp,spec_path=spec,output_dir=self.root/'analysis',llm_options={'api_key':'unused'})
        bound=next(a for a in state['assumptions'] if a['proposal_origin']=='frozen_llm_bound_to_ir')
        self.assertEqual(bound['evidence_status'],'needs_source_review')
        self.assertTrue(any(r['assumption_id']==bound['id'] and r['status']=='sat' for r in state['local']))
        graph=export_graph(state['g1'])
        self.assertTrue(any(e['source']==bound['id'] and e['relation']=='specified-by' and e['verification_state']=='proposed' for e in graph['edges']))
        self.assertFalse(any(n['type']=='Evidence' and n.get('attributes',{}).get('level')=='source_conformance' for n in graph['nodes']))
    def test_verified_source_relationships_survive_input_export(self):
        reqs=extract_requirements('load has priority over clear.');choices,checks,events=review_assumptions(self.model,scan_choices(self.model,reqs),reqs)
        graph=build_graph(self.model,reqs,choices,{},dict(envelope(ir_path=FIXTURE),snapshot_id='post_rtl_pre_validation'))
        exported=export_graph(graph)
        self.assertTrue(any(e['relation']=='specified-by' for e in exported['edges']))
        self.assertEqual(next(a for a in choices if a['kind']=='priority')['evidence_status'],'supported')
    def test_representation_ablation_removes_nodes_and_incident_edges(self):
        reqs=extract_requirements('load has priority over clear.');choices=scan_choices(self.model,reqs)
        graph=build_graph(self.model,reqs,choices,{},dict(envelope(ir_path=FIXTURE),snapshot_id='post_rtl_pre_validation'))
        exported=export_graph(graph,representations=('Spec','RTL'))
        self.assertFalse(any(n['type']=='IR' for n in exported['nodes']));self.assertIsNone(exported['ir_hash'])
        known={n['id'] for n in exported['nodes']};self.assertTrue(all(e['source'] in known and e['target'] in known for e in exported['edges']))
    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_rtl_branch_impact_is_not_raw_guard_impact(self):
        from assumption_modeling.alternatives import typed_patch
        from assumption_modeling.witness import bounded_impact
        from assumption_modeling.observations import impact_labels
        mapping={};from Compiler.rtl_compiler.backend import generate_verilog
        generate_verilog(self.model,source_map=mapping)
        choices=scan_choices(self.model,[]);a=next(a for a in choices if a['kind']=='priority');b,_=typed_patch(self.model,a,a['alternatives'][0])
        graph=build_graph(self.model,[],choices,mapping,envelope(ir_path=FIXTURE))
        report=bounded_impact(self.model,b,a['id'],'B',horizon=1)
        labels=impact_labels(graph,self.model,report)
        self.assertEqual(labels['IR:guard:clear_op']['value'],0)
        regions=[n for n in graph['nodes'] if n['type']=='RTL' and n['subtype']=='effective_branch']
        self.assertTrue(any(labels[n['id']]['mask'] and labels[n['id']]['value']==1 for n in regions))
        self.assertTrue(any(label['source']=='bounded_ir_supported_rtl_localization' for label in labels.values()))
    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_handshake_hold_semantics_checks_guard_context(self):
        import yaml
        from experiments.contribution1.families import family_document
        from assumption_modeling.support import check_behavior_claim
        doc,_=family_document('ready_valid');path=self.root/'buffer.yaml';path.write_text(yaml.safe_dump(doc))
        model=parse_ir(path);claim=dict(kind='hold',target='payload',trigger='out_valid && !out_ready && !flush',scope='normal_operation')
        self.assertEqual(check_behavior_claim(model,claim)['status'],'conformant')
        model.transition_map()['capture_payload'].guard='in_valid'
        self.assertEqual(check_behavior_claim(model,claim)['status'],'violated')
    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_actual_revision_cost_is_separate_from_edit_count(self):
        if not shutil.which('verilator'):self.skipTest('Verilator unavailable')
        from assumption_modeling.revision_cost import measure_revision
        a=next(a for a in scan_choices(self.model,[]) if a['kind']=='priority')
        result=measure_revision(self.model,a,a['alternatives'][0],self.root/'measurement',horizon=1,repeats=1)
        self.assertEqual(result['status'],'measured');self.assertGreater(result['value'],0)
        self.assertEqual(result['scope']['unit'],'seconds');self.assertEqual(result['semantic_edit_count'],1)
        self.assertEqual(result['source'],'executed_revision_and_verification')
    def test_split_reserves_independent_groups_for_four_sets(self):
        from experiments.contribution1.split_dataset import split_rows
        rows=[]
        for i in range(6):
            case=self.root/str(i)
            # Different graph shapes, not mere renamings.
            write_json(case/'input/graph.json',dict(nodes=[dict(id=str(j),type='IR',subtype='state',attributes={'width':i+1}) for j in range(i+1)],edges=[]))
            rows.append(dict(case_id=str(i),case_path=str(case),base_design_id=str(i),template_id=str(i),lineage_group=str(i),input_hash=str(i)))
        split=split_rows(rows);self.assertEqual(split['status'],'ready');self.assertTrue(all(split['splits'].values()))
    @unittest.skipUnless(HAS_NUMPY,'optional NumPy')
    def test_all_task_predictions_and_calibration_are_exported(self):
        from assumption_modeling.graph import with_evidence
        from assumption_modeling.features import HashEncoder,encode_graph,FEATURE_VERSION
        from assumption_modeling.model import MultiTaskModel
        from assumption_modeling.predict import predict_graph
        choices=scan_choices(self.model,[]);graph=build_graph(self.model,[],choices,{},dict(envelope(ir_path=FIXTURE),snapshot_id='post_local_witness'))
        encoder=HashEncoder(8);features=encode_graph(export_graph(graph),[dict(id=a['id'],kind=a['kind'],alternatives=a['alternatives']) for a in choices],encoder)
        model=MultiTaskModel(features['feature_dim'],8,hidden_dim=4,layers=1)
        extra=dict(snapshot='post_local_witness',horizon=2,feature_schema=FEATURE_VERSION,encoder=encoder.manifest,
            task_label_counts={t:1 for t in ('status','mismatch','impact','observable','cost')},cost_scope=dict(unit='seconds',policy_id='test_policy'),data_hash='D',split_hash='S')
        checkpoint=self.root/'best.npz';model.save(checkpoint,extra=extra)
        calibrator=self.root/'calibrator.json';write_json(calibrator,dict(checkpoint_hash=file_hash(checkpoint),input_manifest_hash='D',split_hash='S',temperatures={'status':2,'mismatch':2,'impact':2,'observable':2}))
        rows=predict_graph(graph,choices,checkpoint,2,calibrator_path=calibrator)
        self.assertTrue(rows)
        for row in rows:
            self.assertAlmostEqual(sum(row['predicted_status_distribution']),1)
            self.assertIsNotNone(row['predicted_revision_cost']);self.assertGreaterEqual(row['predicted_revision_cost']['value'],0)
            self.assertTrue(0<=row['predicted_observable_difference_probability']<=1);self.assertEqual(row['calibration'],'temperature_scaled')
        extra['task_label_counts']['cost']=0;model.save(checkpoint,extra=extra);calibrator.unlink()
        self.assertTrue(all(r['predicted_revision_cost'] is None for r in predict_graph(graph,choices,checkpoint,2)))
        extra['task_label_counts']['cost']=1
        extra['task_kind_label_counts']={task:{'reset_value':1} for task in extra['task_label_counts']}
        model.save(checkpoint,extra=extra)
        kinds={a['id']:a['kind'] for a in choices}
        rows=predict_graph(graph,choices,checkpoint,2)
        supported=[r for r in rows if kinds[r['assumption_id']]=='reset_value']
        unsupported=[r for r in rows if kinds[r['assumption_id']]!='reset_value']
        self.assertTrue(supported and unsupported)
        self.assertTrue(all(r['predicted_mismatch_probability'] is not None for r in supported))
        self.assertTrue(all(r['predicted_status_distribution'] is None and r['predicted_mismatch_probability'] is None
            and r['predicted_impact_nodes']==[] and r['predicted_revision_cost'] is None for r in unsupported))

    @unittest.skipUnless(HAS_NUMPY,'optional NumPy')
    def test_local_semantic_encoder_is_frozen_and_versioned(self):
        import sys
        import numpy as np
        from assumption_modeling.features import LocalSemanticEncoder
        class Parameter:
            def requires_grad_(self,value):self.requires_grad=value
        class FrozenModel:
            def __init__(self,*args,**kwargs):self.kwargs=kwargs;self.weight=Parameter();self.max_seq_length=128
            def parameters(self):return [self.weight]
            def eval(self):self.evaluating=True
            def get_sentence_embedding_dimension(self):return 4
            def encode(self,*args,**kwargs):return np.array([1.,0.,0.,0.])
        local=self.root/'encoder';local.mkdir();(local/'config.json').write_text('{}')
        with patch.dict(sys.modules,{'sentence_transformers':SimpleNamespace(SentenceTransformer=FrozenModel)}),patch('importlib.metadata.version',return_value='test-runtime'):
            encoder=LocalSemanticEncoder(local)
        self.assertFalse(encoder.model.weight.requires_grad);self.assertTrue(encoder.model.evaluating)
        self.assertTrue(encoder.model.kwargs['local_files_only']);self.assertFalse(encoder.model.kwargs['trust_remote_code'])
        self.assertEqual(encoder.manifest['kind'],'local_semantic_encoder');self.assertTrue(encoder.manifest['version'])
    def test_review_attestation_expires_when_labels_change(self):
        from experiments.contribution1.review_dataset import import_reviews,valid_review
        from experiments.contribution1.common import write_manifest,read_json
        case=self.root/'case';write_json(case/'metadata.json',dict(human_reviewed=False));write_json(case/'oracle/labels.json',dict(queries=[]))
        manifest=self.root/'manifest.jsonl';write_manifest(manifest,[dict(case_id='case',case_path='case',input_hash='INPUT')])
        review=self.root/'review.json';write_json(review,dict(reviews=[dict(case_id='case',input_hash='INPUT',labels_hash=file_hash(case/'oracle/labels.json'),decision='approved',reviewer='unit-test-fixture')]))
        import_reviews(manifest,review);metadata=read_json(case/'metadata.json')
        self.assertTrue(valid_review(case,metadata,'INPUT'))
        write_json(case/'oracle/labels.json',dict(queries=[{'changed':True}]))
        self.assertFalse(valid_review(case,metadata,'INPUT'))
    @unittest.skipUnless(HAS_NUMPY,'optional NumPy')
    def test_calibration_and_evaluation_tools_preserve_scope(self):
        from assumption_modeling.model import MultiTaskModel
        from experiments.contribution1.calibrate import calibrate
        from experiments.contribution1.evaluate import evaluate
        from .test_training import sample
        features,labels=sample();other=copy.deepcopy(labels);other['queries'][0]['status']['value']=0;other['queries'][0]['mismatch']['value']=0;other['queries'][0]['alternatives'][0]['observable']['value']=0
        samples=[dict(case_id='one',group='one',features=features,labels=labels),dict(case_id='two',group='two',features=features,labels=other)]
        root=self.root/'run';seed=root/'seed_17';seed.mkdir(parents=True)
        manifest=self.root/'data.jsonl';manifest.write_text('test fixture');splits=self.root/'split.json';splits.write_text('{}')
        checkpoint=seed/'best.npz';MultiTaskModel(3,2,hidden_dim=4,layers=1).save(checkpoint)
        write_json(root/'config.json',dict(manifest=str(manifest),split_manifest=str(splits)))
        write_json(root/'run_manifest.json',dict(status='trained',input_manifest_hash=file_hash(manifest),split_hash=file_hash(splits),seeds=[dict(seed=17,checkpoint=str(checkpoint))]))
        with patch('experiments.contribution1.calibrate.load_split',return_value=samples):calibrate(root)
        self.assertTrue((seed/'calibrator.json').is_file())
        with patch('experiments.contribution1.evaluate.load_split',return_value=samples):summary=evaluate(root,'test',bootstrap=5)
        self.assertEqual(summary['seeds'][0]['metrics']['status']['count'],2)
        self.assertTrue((seed/'test_predictions.json').is_file())
        self.assertTrue((root/'test_summary.json').is_file())
    def test_available_properties_are_not_future_property_leakage(self):
        from assumption_modeling.properties import attach_properties
        choices=scan_choices(self.model,[]);graph=build_graph(self.model,[],choices,{},dict(envelope(ir_path=FIXTURE),snapshot_id='post_rtl_pre_validation'))
        doc=dict(sva_properties=[dict(name='actual_update',category='state_update',scope=dict(related_states=['count']))])
        available=attach_properties(graph,doc,visibility='G0')
        self.assertTrue(any(e['relation']=='checked-by' for e in export_graph(available)['edges']))
        future=attach_properties(graph,doc,visibility='G2')
        self.assertFalse(any(n['type']=='Property' for n in export_graph(future)['nodes']))
    def test_analysis_configuration_failure_does_not_trigger_ir_repair(self):
        from iteration import pipeline
        with patch('sys.argv',['main.py','--ir',str(FIXTURE),'--generation-dir',str(self.root/'pipeline')]):args=pipeline._arguments()
        with patch('assumption_modeling.analysis.start_analysis',side_effect=ValueError('invalid checkpoint schema')),patch.object(pipeline,'repair_hw_ir') as repair,patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError,'HW-IR repair is not applicable'):pipeline.run_pipeline(args)
        repair.assert_not_called()

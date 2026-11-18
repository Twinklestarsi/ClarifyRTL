import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from Compiler.rtl_compiler.parser import parse_ir
from assumption_modeling.analysis import start_analysis,finish_analysis
from assumption_modeling.export_training import export_graph
from assumption_modeling.graph import build_graph
from assumption_modeling.impact import structural_cone
from assumption_modeling.schema import envelope
from iteration import pipeline
from .test_semantics import FIXTURE,HAS_Z3


class GraphPipelineTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def args(self,*argv):
        with patch('sys.argv',['main.py',*argv]):return pipeline._arguments()
    def run_ir(self,*extra):
        args=self.args('--ir',str(FIXTURE),'--generation-dir',str(self.root),'--no-verilator','--no-sva','--no-hw-ir-repair',*extra)
        with patch('builtins.print'):return pipeline.run_pipeline(args)
    def test_no_verilator_early_return_keeps_all_snapshots(self):
        path,manifest=self.run_ir();self.assertTrue(path.is_file());a=manifest['contribution1']['attempts'][0];directory=Path(a['directory'])
        for name in ('graph_G0','graph_G1','graph_G2','predictions','source_map'):self.assertTrue((directory/f'{name}.json').is_file())
        predictions=json.loads((directory/'predictions.json').read_text())['predictions']
        self.assertTrue(all(p['predicted_status_distribution'] is None for p in predictions))
        self.assertEqual(json.loads((directory/'graph_G2.json').read_text())['validation']['status'],'skipped')
    def test_new_run_does_not_overwrite_previous_attempt(self):
        _,first=self.run_ir();old=Path(first['contribution1']['attempts'][0]['directory'])/'graph_G1.json';text=old.read_text()
        _,second=self.run_ir();self.assertNotEqual(first['contribution1']['run_id'],second['contribution1']['run_id']);self.assertEqual(old.read_text(),text)
    def test_pre_ir_unresolved_never_calls_generation(self):
        spec=self.root/'spec.md';spec.write_text('clear clears. load loads.\n')
        args=self.args(str(spec),'--pre-ir-analysis','--generation-dir',str(self.root/'out'))
        with patch.object(pipeline,'generate_ir') as generate:
            _,manifest=pipeline.run_pipeline(args)
        self.assertEqual(manifest['status'],'needs_clarification');generate.assert_not_called()
    def test_lint_failure_still_saves_analysis(self):
        args=self.args('--ir',str(FIXTURE),'--generation-dir',str(self.root),'--no-sva','--no-hw-ir-repair')
        report=dict(status='failed',mode='lint',compile_returncode=1,simulation_returncode=None)
        with patch.object(pipeline,'run_verilator',return_value=(None,report)),patch('builtins.print'):
            with self.assertRaises(Exception):pipeline.run_pipeline(args)
        directories=list((self.root/'contribution1/runs').glob('*/attempts/attempt_0'))
        self.assertEqual(len(directories),1);self.assertTrue((directories[0]/'graph_G2.json').is_file());self.assertEqual(json.loads((self.root/'pipeline_manifest.json').read_text())['status'],'failed')
    def test_export_drops_future_and_gold_fields(self):
        _,manifest=self.run_ir();directory=Path(manifest['contribution1']['attempts'][0]['directory'])
        g=json.loads((directory/'graph_G1.json').read_text());g['nodes'][0]['attributes']['gold_status']='implementation_error';g['hidden_spec']='secret'
        exported=export_graph(g);self.assertNotIn('secret',json.dumps(exported));self.assertNotIn('gold_status',json.dumps(exported))
        g['snapshot_id']='post_validation'
        with self.assertRaises(ValueError):export_graph(g)
    def test_state_output_alias_and_feedback_cone(self):
        model=parse_ir(FIXTURE);g=build_graph(model,[],[],{},envelope(ir_path=FIXTURE))
        count=[n for n in g['nodes'] if n['id']=='IR:state:count'];self.assertEqual(len(count),1);self.assertIn('output',count[0]['attributes']['roles']);self.assertEqual(count[0]['attributes']['reset_value'],"4'd0")
        cone=structural_cone(g,['IR:state:count']);self.assertIn('IR:action:enable_op:0',cone['nodes']);self.assertIsNone(cone['semantic_impact'])
    def test_partial_dependencies_do_not_prove_no_impact(self):
        model=parse_ir(FIXTURE);model.transitions[0].guard='$signed(data_in)'
        g=build_graph(model,[],[],{},envelope(ir_path=FIXTURE));self.assertFalse(g['coverage']['dependency_complete'])
        cone=structural_cone(g,['IR:state:count']);self.assertFalse(cone['absence_proves_no_impact'])
    def test_source_map_hash_mismatch_is_rejected(self):
        _,manifest=self.run_ir();directory=Path(manifest['contribution1']['attempts'][0]['directory']);mp=directory/'source_map.json';data=json.loads(mp.read_text());data['ir_hash']='stale';mp.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'hashes'):
            start_analysis(ir_path=self.root/'ir.yaml',rtl_path=self.root/'generated_rtl.v',source_map_path=mp,spec_path=None,output_dir=self.root/'bad')
    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_reachable_witness_replays_in_rtl(self):
        import shutil
        if not shutil.which('verilator'):self.skipTest('Verilator unavailable')
        from assumption_modeling.candidates import scan_choices
        from assumption_modeling.alternatives import typed_patch
        from assumption_modeling.witness import bounded_impact
        from assumption_modeling.replay import replay
        model=parse_ir(FIXTURE);a=next(a for a in scan_choices(model,[]) if a['kind']=='priority');b,_=typed_patch(model,a,a['alternatives'][0])
        result=bounded_impact(model,b,a['id'],'B',horizon=2);query=next(q for q in result['queries'] if q['status']=='sat')
        result=replay(model,b,query['witness'],self.root/'replay')
        self.assertEqual(result['status'],'verified');self.assertEqual(result['report']['mode'],'simulation')

import importlib.util
import tempfile
import unittest
from pathlib import Path
import yaml
from assumption_modeling.expressions import parse, evaluate, Unsupported
from experiments.contribution1.rtllm_models import SELECTION, build_model
from Compiler.rtl_compiler.parser import parse_ir


@unittest.skipUnless(importlib.util.find_spec('z3'), 'optional Z3')
class RTLLMSemanticsTests(unittest.TestCase):
    def test_self_determined_concat_and_select_and_shift_widths(self):
        import z3
        widths={'a':4,'b':4,'shift':16};values={'a':z3.BitVecVal(15,4),'b':z3.BitVecVal(1,4),'shift':z3.BitVecVal(256,16)}
        def result(text,width=None):return z3.simplify(evaluate(parse(text),values,widths,width)).as_long()
        self.assertEqual(result('{a,b}',16),241)
        self.assertEqual(result('{a + b,b}',16),1)  # 4-bit addition inside concat truncates.
        self.assertEqual(result('~a[0]'),0)
        self.assertEqual(result('a[3:1]',8),7)
        self.assertEqual(result("a << 4'd1",8),30)
        self.assertEqual(result("a >> 4'd1"),7)
        self.assertEqual(result('a << shift'),0)
        self.assertEqual(result("a + b << 4'd1"),0)
        with self.assertRaises(Unsupported):result('a[4:0]')

    def test_all_twenty_models_have_supported_scalar_semantics(self):
        from assumption_modeling.transition_system import TransitionSystem
        self.assertEqual(len(SELECTION),20)
        with tempfile.TemporaryDirectory() as td:
            for name,_,_ in SELECTION:
                with self.subTest(name=name):
                    p=Path(td)/'ir.yaml';p.write_text(yaml.safe_dump(build_model(name).document()))
                    system=TransitionSystem(parse_ir(p))
                    self.assertEqual(system.observation_domain,'active_clock_samples')

    def test_async_reset_scope_and_reachable_witness(self):
        from assumption_modeling.candidates import scan_choices
        from assumption_modeling.alternatives import typed_patch
        from assumption_modeling.witness import bounded_impact
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'ir.yaml';p.write_text(yaml.safe_dump(build_model('ring_counter').document()))
            model=parse_ir(p);a=next(a for a in scan_choices(model,[]) if a['kind']=='reset_value')
            other,_=typed_patch(model,a,a['alternatives'][0])
            report=bounded_impact(model,other,a['id'],'B',horizon=2)
            query=next(q for q in report['queries'] if q['observable']=='state:state')
            self.assertEqual(query['status'],'sat')
            self.assertEqual(query['scope']['observation_domain'],'active_clock_samples')
            self.assertFalse(query['scope']['inter_edge_reset_pulses'])

if __name__=='__main__':unittest.main()

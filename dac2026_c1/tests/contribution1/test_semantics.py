import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path

from Compiler.rtl_compiler.parser import parse_ir
from Compiler.rtl_compiler.backend import generate_verilog
from Compiler.rtl_compiler.compiler import compile_ir
from assumption_modeling.candidates import scan_choices
from assumption_modeling.alternatives import typed_patch
from assumption_modeling.conformance import check_conformance
from assumption_modeling.expressions import parse,Unsupported
from assumption_modeling.schema import file_hash
from assumption_modeling.witness import local_difference,bounded_impact

FIXTURE=Path(__file__).resolve().parents[2]/'experiments/contribution1_demo/case_001/input/ir.yaml'
HAS_Z3=importlib.util.find_spec('z3') is not None


class SemanticTests(unittest.TestCase):
    def setUp(self):self.model=parse_ir(FIXTURE)
    def priority(self):
        a=next(a for a in scan_choices(self.model,[]) if a['kind']=='priority')
        b,patch=typed_patch(self.model,a,a['alternatives'][0]);return a,b,patch

    def test_emission_map_is_exact_and_preserves_output(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.v';mp=Path(td)/'map.json'
            compile_ir(FIXTURE,out,source_map_path=mp)
            self.assertEqual(out.read_text(),generate_verilog(self.model))
            mapping=json.loads(mp.read_text());self.assertEqual(mapping['rtl_hash'],file_hash(out));self.assertEqual(mapping['ir_hash'],file_hash(FIXTURE))
            lines=out.read_text().splitlines();roles=set()
            for r in mapping['regions']:
                self.assertLessEqual(r['line_end'],len(lines));self.assertGreaterEqual(r['line_start'],1);roles.add(r['role'])
                if r['role']=='reset_override':self.assertIn("count <= 4'd0",lines[r['line_start']-1])
                if r['role']=='action':self.assertIn('=',lines[r['line_start']-1])
            self.assertTrue({'default_hold','reset_override','effective_branch','action','state_commit','port_declaration'}<=roles)

    def test_patch_preserves_other_pairwise_priorities(self):
        from Compiler.rtl_compiler.lowering import ordered_for_target
        a,b,manifest=self.priority()
        self.assertEqual(ordered_for_target(b,b.processes[0],'count'),['clear_op','load_op','enable_op'])
        self.assertEqual(self.model.processes[0].transitions,['load_op','clear_op','enable_op'])
        self.assertEqual(manifest['semantic_edit_count'],1)

    def test_masked_guard_is_unknown_not_false(self):
        self.model.transition_map()['load_op'].guard='load && !clear'
        a=next(a for a in scan_choices(self.model,[]) if a['kind']=='priority')
        self.assertEqual(a['binding_state'],'binding_unknown')
        with self.assertRaises(Unsupported):typed_patch(self.model,a,a['alternatives'][0])

    def test_environment_alternative_never_silently_constrains(self):
        a,_,_=self.priority()
        with self.assertRaisesRegex(Unsupported,'contract evidence'):typed_patch(self.model,a,a['alternatives'][1])

    def test_contract_detects_reversed_normalized_priority(self):
        contract=dict(constraints=[dict(id='C1',kind='priority',higher_operation='clear',lower_operation='load',evidence_ids=['E'])])
        self.assertEqual(check_conformance(self.model,contract)['status'],'violated')
        _,b,_=self.priority();self.assertEqual(check_conformance(b,contract)['status'],'conformant')

    def test_parser_rejects_code_and_unsupported_syntax(self):
        for expression in ["__import__('os')",'a[b:c:d]','{2{a}}',"4'sd2",'a >>> b',"4'bx"]:
            with self.subTest(expression=expression):
                with self.assertRaises(Unsupported):parse(expression)
        # Parameterized/dynamic bit and part selects are parsed for dependency
        # extraction; width evaluation may still reject them later.
        self.assertEqual(parse('a[b:0]').symbols(), {'a', 'b'})
        self.assertEqual(parse('(a && !b) ? x + 4\'d1 : x').symbols(),{'a','b','x'})

    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_full_functions_include_higher_priority_and_reset(self):
        import z3
        from assumption_modeling.transition_system import TransitionSystem
        _,b,_=self.priority();system=TransitionSystem(b)
        state={'count':z3.BitVecVal(7,4)};inputs={n:z3.BitVecVal(0,system.widths[n]) for n in system.inputs}
        inputs.update(clear=z3.BitVecVal(1,1),load=z3.BitVecVal(1,1),data_in=z3.BitVecVal(5,4))
        out,_=system.step(state,inputs);self.assertEqual(z3.simplify(out['count']).as_long(),0)
        inputs['reset']=z3.BitVecVal(1,1);out,_=system.step(state,inputs);self.assertEqual(z3.simplify(out['count']).as_long(),0)

    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_local_and_bounded_are_independent(self):
        a,b,_=self.priority()
        local=local_difference(self.model,b,a['id'],a['alternatives'][0]['id'])
        self.assertEqual(local['status'],'sat');self.assertEqual(local['scope']['initial_state'],'abstract')
        bounded=bounded_impact(self.model,b,a['id'],'B',horizon=2)
        q=next(q for q in bounded['queries'] if q['observable']=='state:count')
        self.assertEqual(q['status'],'sat');self.assertIsNotNone(q['witness']['reset_prefix'])
        self.assertEqual(next(q for q in bounded['queries'] if q['observable']=='guard:clear_op')['status'],'unsat')

    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_invalid_environment_not_vacuous_equivalence(self):
        a,b,_=self.priority()
        env=[dict(expression="1'b0",evidence_ids=['E'])]
        self.assertEqual(local_difference(self.model,b,a['id'],'B',environment=env)['status'],'invalid_environment')
        self.assertEqual(bounded_impact(self.model,b,a['id'],'B',horizon=2,environment=env)['status'],'invalid_environment')

    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_unsupported_semantics_retains_unknown(self):
        a,b,_=self.priority();self.model.states[0].datatype='signed'
        self.assertEqual(local_difference(self.model,b,a['id'],'B')['status'],'unknown')

    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_unsigned_context_and_truncation(self):
        import z3
        from assumption_modeling.expressions import evaluate,resize
        x=z3.BitVecVal(15,4)
        result=evaluate(parse("x + 4'd1"),{'x':x},{'x':4},4)
        self.assertEqual(z3.simplify(result).as_long(),0)
        result=evaluate(parse("x + 4'd1"),{'x':x},{'x':4},5)
        self.assertEqual(z3.simplify(result).as_long(),16)
        result=evaluate(parse("x < 4'd1"),{'x':x},{'x':4})
        self.assertEqual(z3.simplify(result).as_long(),0)
        with self.assertRaises(Unsupported):evaluate(parse('x + 1'),{'x':x},{'x':4})

    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_reset_variants_do_not_force_equal_post_reset_state(self):
        a=next(a for a in scan_choices(self.model,[]) if a['kind']=='reset_value');b,_=typed_patch(self.model,a,a['alternatives'][0])
        result=bounded_impact(self.model,b,a['id'],'B',horizon=1)
        q=next(q for q in result['queries'] if q['observable']=='state:count')
        self.assertEqual(q['status'],'sat');self.assertEqual(q['witness']['initial_A']['count'],0);self.assertEqual(q['witness']['initial_B']['count'],1)

    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_uninitialized_reachability_is_unknown(self):
        self.model.clock_domains[0].reset=None
        a,b,_=self.priority()
        self.assertEqual(bounded_impact(self.model,b,a['id'],'B',horizon=1)['status'],'unknown')

    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_invalid_payload_changes_are_not_observable(self):
        from Compiler.rtl_compiler.model import Port
        self.model.ports.append(Port('valid','output',1))
        self.model.derived_signals.append(dict(name='valid',width=1,expression="1'b0"))
        a,b,_=self.priority()
        protocol=dict(evidence_ids=['USER_PROTOCOL'],channels=[dict(valid='valid',payload=['count'])])
        result=bounded_impact(self.model,b,a['id'],'B',horizon=2,output_protocol=protocol)
        queries={q['observable']:q for q in result['queries']}
        self.assertEqual(queries['raw_output:count']['status'],'sat')
        self.assertEqual(queries['output:valid']['status'],'unsat')

    @unittest.skipUnless(HAS_Z3,'optional Z3')
    def test_valid_control_difference_is_observable(self):
        from Compiler.rtl_compiler.model import Port
        self.model.ports.append(Port('valid','output',1))
        self.model.derived_signals.append(dict(name='valid',width=1,expression="count == 4'd0"))
        a,b,_=self.priority()
        protocol=dict(evidence_ids=['USER_PROTOCOL'],channels=[dict(valid='valid',payload=['count'])])
        result=bounded_impact(self.model,b,a['id'],'B',horizon=2,output_protocol=protocol)
        self.assertEqual(next(q for q in result['queries'] if q['observable']=='output:valid')['status'],'sat')

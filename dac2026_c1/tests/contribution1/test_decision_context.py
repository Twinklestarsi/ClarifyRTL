import copy
import unittest
import numpy as np

from assumption_modeling.decision_context import add_context, CONTEXT_VERSION, QUERY_NAMES, ALT_NAMES, NODE_NAMES
from assumption_modeling.features import HashEncoder, encode_graph
from assumption_modeling.schema import digest
from assumption_modeling.model import MultiTaskModel
from tests.contribution1.test_training import sample


class ContextTests(unittest.TestCase):
    def graph(self):
        nodes=[dict(id='A',type='Assumption',subtype='reset_value',text="4'd0",attributes={'binding_state':'verified'}),
               dict(id='S',type='IR',subtype='state',text='count',attributes={'width':4,'roles':['state','output']}),
               dict(id='Q',type='Requirement',subtype=None,text='Reset value of count is 0.',attributes={
                   'interpretation_status':'canonical_verified','claims':[dict(kind='reset_value',target='count',value=0)]})]
        g=dict(snapshot_id='post_local_witness',nodes=nodes,edges=[dict(source='A',target='S',relation='binds-to')])
        g['input_hash']=digest(g);return g

    def test_visible_clauses_match_target_and_keep_missing_intent_unknown(self):
        g=self.graph();queries=[dict(id='A',kind='reset_value',alternatives=[dict(id='B',value="4'd1")])]
        f=encode_graph(g,queries,HashEncoder(8));a=add_context(f,g,queries)
        values=dict(zip(QUERY_NAMES,a['decision_context']['A']['context']))
        self.assertEqual(values['source_exact_match'],1)
        g['nodes'][2]['attributes']['claims'][0]['target']='unrelated_register'
        b=add_context(f,g,queries);values=dict(zip(QUERY_NAMES,b['decision_context']['A']['context']))
        self.assertEqual(values['source_present'],0)
        self.assertEqual(values['source_syntactic_difference'],0)
        self.assertEqual(len(a['decision_context']['A']['alternatives']['B']['nodes'][0]),len(NODE_NAMES))

    def test_future_snapshot_is_rejected(self):
        g=self.graph();g['snapshot_id']='post_bounded_witness'
        with self.assertRaises(ValueError):add_context({},g,[])

    def test_context_and_log_cost_gradients_match_numeric_derivatives(self):
        f,l=sample();rng=np.random.default_rng(11)
        f['decision_context_schema']=CONTEXT_VERSION
        f['decision_context']={'A':dict(context=rng.normal(size=len(QUERY_NAMES)).tolist(), alternatives={
            'B':dict(context=rng.normal(size=len(ALT_NAMES)).tolist(),nodes=rng.normal(size=(3,len(NODE_NAMES))).tolist())})}
        m=MultiTaskModel(3,2,hidden_dim=4,layers=1,decision_context=True,cost_loss='log_seconds',impact_positive_weight=2.)
        losses,grads=m.loss_and_grad(f,l)
        for key,index in [('status_context_W',(3,2)),('mismatch_context_W',(1,)),('impact_node_W',(2,)),
                          ('impact_context_W',(2,)),('observable_context_W',(3,)),('cost_context_W',(4,)),('cost_W',(0,))]:
            old=m.params[key][index];eps=1e-5
            m.params[key][index]=old+eps;plus=sum(m.loss_and_grad(f,l)[0].values())
            m.params[key][index]=old-eps;minus=sum(m.loss_and_grad(f,l)[0].values())
            m.params[key][index]=old
            self.assertAlmostEqual((plus-minus)/(2*eps),grads[key][index],places=5,msg=key)
        bad=copy.deepcopy(l);bad['queries'][0]['alternatives'][0]['cost']['value']=0
        with self.assertRaises(ValueError):m.loss_and_grad(f,bad)


if __name__=='__main__':unittest.main()

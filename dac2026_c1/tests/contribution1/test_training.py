import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from assumption_modeling.schema import write_json,digest

HAS_NUMPY=importlib.util.find_spec('numpy') is not None


def sample():
    features=dict(x=[[.1,.2,.3],[.4,-.3,.2],[-.2,.4,.1]],types=[1,2,3],edges=[[0,1,0],[1,0,7],[1,2,6],[2,1,13]],node_ids=['A','S','R'],queries=[dict(id='A',node=0,alternatives=[dict(id='B',embedding=[.2,-.4])])],feature_dim=3,alternative_dim=2)
    labels=dict(queries=[dict(assumption_id='A',status=dict(value=2,mask=True),mismatch=dict(value=1,mask=True),alternatives=[dict(alternative_id='B',impact={'A':dict(value=None,mask=False),'S':dict(value=1,mask=True),'R':dict(value=0,mask=True)},observable=dict(value=1,mask=True),cost=dict(value=2,mask=True))])])
    return features,labels


@unittest.skipUnless(HAS_NUMPY,'optional NumPy')
class ModelTests(unittest.TestCase):
    def test_gradients_match_finite_differences(self):
        import numpy as np
        from assumption_modeling.model import MultiTaskModel
        features,labels=sample();model=MultiTaskModel(3,2,hidden_dim=4,layers=1,seed=17)
        loss,grads=model.loss_and_grad(features,labels)
        for key,index in [('type_W',(1,0,0)),('self_0',(0,1)),('rel_0',(0,1,2)),('query_h',(1,1)),('query_b',(0,1)),('impact_W',(3,)),('status_W',(1,2)),('cost_W',(1,))]:
            value=model.params[key][index];epsilon=1e-5
            model.params[key][index]=value+epsilon;plus=sum(model.loss_and_grad(features,labels)[0].values())
            model.params[key][index]=value-epsilon;minus=sum(model.loss_and_grad(features,labels)[0].values())
            model.params[key][index]=value
            self.assertAlmostEqual((plus-minus)/(2*epsilon),grads[key][index],places=5,msg=key)
    def test_no_valid_supervision_skips_optimizer(self):
        import numpy as np
        from assumption_modeling.model import MultiTaskModel,AdamW
        features,labels=sample();labels['queries']=[];model=MultiTaskModel(3,2,hidden_dim=4,layers=1);optimizer=AdamW(model.params)
        original={k:v.copy() for k,v in model.params.items()};loss,grads=model.loss_and_grad(features,labels)
        self.assertEqual(loss,{});self.assertIsNone(grads);self.assertFalse(optimizer.step(model.params,grads));self.assertEqual(optimizer.t,0)
        for key in model.params:np.testing.assert_array_equal(original[key],model.params[key])
    def test_resolved_targets_excluded_from_loss(self):
        from assumption_modeling.model import MultiTaskModel
        features,labels=sample();query=labels['queries'][0]
        query['status']['resolved_by_evidence']=True;query['mismatch']['mask']=False
        query['alternatives'][0]['observable']['mask']=False;query['alternatives'][0]['cost']['mask']=False
        for target in query['alternatives'][0]['impact'].values():target['resolved_by_evidence']=True
        loss,grads=MultiTaskModel(3,2,hidden_dim=4,layers=1).loss_and_grad(features,labels)
        self.assertEqual(loss,{});self.assertIsNone(grads)
    def test_small_clean_sample_overfits(self):
        from assumption_modeling.model import MultiTaskModel,AdamW
        f,l=sample();model=MultiTaskModel(3,2,hidden_dim=8,layers=1);optimizer=AdamW(model.params,learning_rate=.02)
        initial=sum(model.loss_and_grad(f,l)[0].values())
        for _ in range(80):
            losses,grads=model.loss_and_grad(f,l);optimizer.step(model.params,grads)
        final=sum(model.loss_and_grad(f,l)[0].values());self.assertLess(final,initial*.15)
    def test_checkpoint_restores_optimizer_and_random_state(self):
        import numpy as np
        from assumption_modeling.model import MultiTaskModel,AdamW
        f,l=sample();model=MultiTaskModel(3,2,hidden_dim=4,layers=1,dropout=.2);optimizer=AdamW(model.params)
        _,grads=model.loss_and_grad(f,l,training=True);optimizer.step(model.params,grads)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'checkpoint.npz';model.save(path,optimizer,dict(epoch=2));loaded,extra=MultiTaskModel.load(path);op=AdamW(loaded.params);op.restore(path)
            self.assertEqual(extra['epoch'],2);self.assertEqual(op.t,optimizer.t)
            a,_=model.loss_and_grad(f,l,training=True);b,_=loaded.loss_and_grad(f,l,training=True);self.assertEqual(a,b)
            for key in model.params:np.testing.assert_array_equal(model.params[key],loaded.params[key])
    def test_unknown_node_labels_are_not_negative(self):
        import numpy as np
        from experiments.contribution1.losses import masked_losses
        f,l=sample();l['queries'][0]['status']['mask']=False;l['queries'][0]['mismatch']['mask']=False
        alt=l['queries'][0]['alternatives'][0];alt['observable']['mask']=False;alt['cost']['mask']=False
        out={'A':dict(alternatives={'B':dict(impact=np.array([100.,0.,0.]))})}
        losses,grads=masked_losses(out,l,f['node_ids']);self.assertAlmostEqual(losses['impact'],np.log(2));self.assertEqual(grads[('A','B','impact')][0],0)
    def test_impact_loss_balances_queries_instead_of_node_counts(self):
        import numpy as np
        from experiments.contribution1.losses import masked_losses
        f,l=sample();query=l['queries'][0];query['status']['mask']=False;query['mismatch']['mask']=False
        alt=query['alternatives'][0];alt['observable']['mask']=False;alt['cost']['mask']=False
        nodes=[str(i) for i in range(100)]
        alt['impact']={'0':dict(mask=True,value=1)}
        second=copy.deepcopy(alt);second['alternative_id']='C';second['impact']={node:dict(mask=True,value=0) for node in nodes};query['alternatives'].append(second)
        outputs={'A':dict(alternatives={'B':dict(impact=np.zeros(100)),'C':dict(impact=np.full(100,2.0))})}
        losses,_=masked_losses(outputs,l,nodes)
        self.assertAlmostEqual(losses['impact'],(np.log(2)+np.logaddexp(0,2))/2)

    def test_single_class_metrics_are_explicit(self):
        from experiments.contribution1.metrics import binary_metrics
        result=binary_metrics([0,0],[.1,.2]);self.assertIsNone(result['auroc']);self.assertIsNone(result['average_precision']);self.assertEqual(result['positive_rate'],0)


class IsolationTests(unittest.TestCase):
    def test_template_and_structural_duplicates_share_split(self):
        from experiments.contribution1.split_dataset import split_rows
        with tempfile.TemporaryDirectory() as td:
            rows=[]
            for i in range(8):
                case=Path(td)/str(i);write_json(case/'input/graph.json',dict(nodes=[dict(id=f'node_{i}',type='IR',subtype='state',attributes={'width':4})],edges=[]))
                rows.append(dict(case_id=str(i),case_path=str(case),base_design_id=str(i),template_id=str(i),lineage_group=str(i),input_hash=str(i)))
            result=split_rows(rows);self.assertEqual(result['independent_groups'],1);self.assertEqual(result['status'],'insufficient_independent_groups')
    @unittest.skipUnless(HAS_NUMPY,'optional NumPy')
    def test_stale_feature_cache_rejected(self):
        from experiments.contribution1.dataset import load_input
        with tempfile.TemporaryDirectory() as td:
            body=dict(snapshot_id='post_local_witness',nodes=[],edges=[]);body['input_hash']=digest(body)
            write_json(Path(td)/'input/graph.json',body);write_json(Path(td)/'input/features.json',dict(input_hash='stale'))
            with self.assertRaisesRegex(ValueError,'stale'):load_input(td)

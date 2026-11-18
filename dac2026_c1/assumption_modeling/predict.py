"""Shared inference contract for all Contribution 1 heads, with calibrated probabilities."""
from .schema import STATUSES


def empty_predictions(assumptions, horizon):
    return [dict(assumption_id=a['id'],alternative_id=b['id'],snapshot='post_local_witness',
        evidence_status=a.get('evidence_status','needs_source_review'),predicted_status_distribution=None,
        status_class_order=list(STATUSES),predicted_mismatch_probability=None,predicted_impact_nodes=[],
        predicted_observable_difference_probability=None,predicted_revision_cost=None,
        prediction_scope=dict(horizon=horizon,semantics='two_state'),model_version=None,tool_results=[])
        for a in assumptions for b in a['alternatives']]


def predict_graph(graph, assumptions, checkpoint, horizon, encoder_path=None, *, calibrator_path=None):
    import json
    import numpy as np
    from pathlib import Path
    from .model import MultiTaskModel
    from .features import HashEncoder,LocalSemanticEncoder,encode_graph,FEATURE_VERSION
    from .export_training import export_graph
    from .schema import digest,file_hash
    from experiments.contribution1.losses import sigmoid
    if not assumptions:return []
    model,extra=MultiTaskModel.load(checkpoint)
    if extra.get('snapshot')!=graph['snapshot_id'] or extra.get('horizon')!=horizon:raise ValueError('checkpoint snapshot/horizon does not match online analysis')
    if extra.get('feature_schema')!=FEATURE_VERSION:raise ValueError('checkpoint feature schema is incompatible')
    manifest=extra.get('encoder',{})
    if manifest.get('kind')=='hash_ngram_baseline':encoder=HashEncoder(manifest['dimension'])
    elif manifest.get('kind')=='local_semantic_encoder' and encoder_path:encoder=LocalSemanticEncoder(encoder_path)
    else:raise ValueError('checkpoint requires its frozen local feature encoder')
    if digest(encoder.manifest)!=digest(manifest):raise ValueError('feature encoder differs from training')
    representations=extra.get('representations',['Spec','IR','RTL'])
    exported=export_graph(graph,representations,include_local_witness=extra.get('include_local_witness',True))
    queries=[dict(id=a['id'],kind=a['kind'],alternatives=a['alternatives']) for a in assumptions]
    features=encode_graph(exported,queries,encoder)
    if model.config.get('decision_context'):
        from .decision_context import add_context
        features=add_context(features,exported,queries)
    outputs,_=model.forward(features)
    calibration=Path(calibrator_path) if calibrator_path else Path(checkpoint).parent/'calibrator.json'
    temperatures={};calibration_state='not_applied'
    if calibration.is_file():
        data=json.loads(calibration.read_text(encoding='utf-8'))
        if data.get('checkpoint_hash')!=file_hash(checkpoint):raise ValueError('calibrator checkpoint hash mismatch')
        if data.get('input_manifest_hash')!=extra.get('data_hash') or data.get('split_hash')!=extra.get('split_hash'):
            raise ValueError('calibrator training scope mismatch')
        temperatures=data['temperatures']
        if any(not np.isfinite(t) or t<=0 for t in temperatures.values()):raise ValueError('invalid calibration temperature')
        calibration_state='temperature_scaled'
    elif calibrator_path:raise ValueError('calibrator file not found')
    counts=extra.get('task_label_counts',{})
    kind_counts=extra.get('task_kind_label_counts')
    result=[]
    for a in assumptions:
        active=lambda task:counts.get(task,0)>0 and (kind_counts is None or kind_counts.get(task,{}).get(a['kind'],0)>0)
        out=outputs[a['id']];logits=out['status']/temperatures.get('status',1)
        distribution=np.exp(logits-logits.max());distribution/=distribution.sum()
        for b in a['alternatives']:
            change=out['alternatives'][b['id']]
            probs=sigmoid(change['impact']/temperatures.get('impact',1))
            cost_scope=extra.get('cost_scope')
            cost=None
            if active('cost') and cost_scope:
                predicted=float(np.expm1(min(30,float(np.logaddexp(0,change['cost'])))))
                cost=dict(value=predicted,**cost_scope)
            result.append(dict(assumption_id=a['id'],alternative_id=b['id'],snapshot=graph['snapshot_id'],
                evidence_status=a.get('evidence_status','needs_source_review'),status_class_order=list(STATUSES),
                predicted_status_distribution=distribution.tolist() if active('status') else None,
                predicted_mismatch_probability=float(sigmoid(out['mismatch']/temperatures.get('mismatch',1))) if active('mismatch') else None,
                predicted_impact_nodes=[dict(node_id=n,type=kind,probability=float(p)) for n,kind,p in zip(features['node_ids'],features['node_types'],probs)] if active('impact') else [],
                predicted_observable_difference_probability=float(sigmoid(change['observable']/temperatures.get('observable',1))) if active('observable') else None,
                predicted_revision_cost=cost,prediction_scope=dict(horizon=horizon,semantics='two_state',representations=representations,
                    observation_domain='active_clock_samples',inter_edge_reset_pulses=False),
                model_version=file_hash(checkpoint),encoder_kind=manifest['kind'],calibration=calibration_state,
                training_coverage=counts,smoke_only=extra.get('smoke_only',False),pilot_only=extra.get('pilot_only',False),
                choice_kind_training_coverage={task:kind_counts.get(task,{}).get(a['kind'],0) for task in counts} if kind_counts is not None else None,
                scientific_evaluation=extra.get('scientific_evaluation'),tool_results=[]))
    return result

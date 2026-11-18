"""Version-matched online evidence import. Labels and future tool results are not evidence inputs."""
import json
from pathlib import Path


def load_evidence(path, metadata, assumption_ids, requirement_ids):
    if not path:return []
    doc=json.loads(Path(path).read_text(encoding='utf-8'))
    if doc.get('spec_hash')!=metadata['spec_hash']:raise ValueError('evidence spec_hash mismatch')
    if doc.get('ir_hash') not in (None,metadata['ir_hash']):raise ValueError('evidence IR revision mismatch')
    events=doc.get('events',[])
    if not isinstance(events,list):raise ValueError('evidence events must be a list')
    seen=set()
    for item in events:
        if not isinstance(item,dict) or not item.get('id') or item['id'] in seen:raise ValueError('invalid/duplicate evidence ID')
        seen.add(item['id'])
        if item.get('assumption_id') not in assumption_ids:raise ValueError('evidence references unknown assumption')
        if item.get('requirement_id') and item['requirement_id'] not in requirement_ids:raise ValueError('evidence references unknown requirement')
        if item.get('level') not in ('user_answer','authoritative_document'):raise ValueError('import only source evidence, not future verification/oracle results')
        if not item.get('source') or not item.get('claim_kind') or not item.get('claim_subject'):raise ValueError('evidence requires source and explicit proposition')
        if item.get('status') not in ('confirmed','rejected','unknown'):raise ValueError('invalid source evidence status')
        item['visibility']='G0';item['origin']=item['level']
    return events

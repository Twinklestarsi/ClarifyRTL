"""Frozen semantic features plus explicit structure/evidence features, shared by train/inference."""
import json
import math
import re
from .schema import digest

FEATURE_VERSION='c1-features-3'
NODE_TYPES=('Requirement','Assumption','IR','RTL','Property','Evidence')
BASE_RELATIONS=('specified-by','implemented-by','reads','writes','depends-on','supports','contradicts','checked-by','relates-to','binds-to','contains')
RELATIONS=BASE_RELATIONS+tuple('reverse:'+r for r in BASE_RELATIONS)
TOOL_STATUSES=('sat','unsat','unknown','conformant','violated','allowed','confirmed','rejected','inconsistent')
STRUCTURAL_FEATURES=('log_in_degree','log_out_degree','log_width','log_reads','log_writes','binding_verified','implemented','source_reviewed','log_clock_domains')+tuple('status_'+s for s in TOOL_STATUSES)


class HashEncoder:
    """Explicit lexical baseline. Never advertised as a pretrained semantic encoder."""
    def __init__(self,dimension=64):
        if dimension<1:raise ValueError('embedding dimension must be positive')
        self.dimension=dimension
        self.manifest=dict(kind='hash_ngram_baseline',version='1',dimension=dimension,normalization='NFKC_lower',max_chars=2048,frozen=True)

    def encode(self,text):
        import unicodedata
        normalized=unicodedata.normalize('NFKC',text).lower()[:self.manifest['max_chars']]
        result=[0.0]*self.dimension
        for token in re.findall(r'\w+|[^\s]',normalized)+[normalized[i:i+3] for i in range(max(0,len(normalized)-2))]:
            h=int(digest(token)[:16],16);result[h%self.dimension]+=1 if (h//self.dimension)%2 else -1
        norm=math.sqrt(sum(v*v for v in result)) or 1
        return [v/norm for v in result]


class LocalSemanticEncoder:
    def __init__(self,path):
        from pathlib import Path
        if not path or not Path(path).is_dir():raise ValueError('Supply encoder_path pointing to an existing frozen local text/code model; hash is an explicit baseline only')
        from sentence_transformers import SentenceTransformer
        from importlib.metadata import version
        self.model=SentenceTransformer(str(path),device='cpu',local_files_only=True,trust_remote_code=False)
        for parameter in self.model.parameters():parameter.requires_grad_(False)
        self.model.eval();self.dimension=self.model.get_sentence_embedding_dimension()
        from functools import lru_cache
        self.encode = lru_cache(maxsize=65536)(self.encode)
        files={}
        import hashlib
        for file in sorted(Path(path).rglob('*')):
            if not file.is_file():continue
            hasher=hashlib.sha256()
            with file.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):hasher.update(chunk)
            files[str(file.relative_to(path))]=hasher.hexdigest()
        self.manifest=dict(kind='local_semantic_encoder',version=digest(files),dimension=self.dimension,frozen=True,
            max_tokens=self.model.max_seq_length,normalization='encoder_tokenizer_and_l2',
            libraries={name:version(name) for name in ('sentence-transformers','transformers','torch')})

    def encode(self,text):
        return self.model.encode(text,normalize_embeddings=True,show_progress_bar=False).tolist()


def make_encoder(config):
    name=config.get('encoder','local')
    if name=='hash':return HashEncoder(config.get('embedding_dim',64))
    if name=='local':return LocalSemanticEncoder(config.get('encoder_path'))
    raise ValueError('encoder must be local (frozen semantic model) or hash (explicit baseline)')


def encode_graph(graph,queries,encoder):
    nodes=graph['nodes'];index={n['id']:i for i,n in enumerate(nodes)}
    incoming=[0]*len(nodes);outgoing=[0]*len(nodes);reads=[0]*len(nodes);writes=[0]*len(nodes);edges=[]
    for edge in graph['edges']:
        src,dst=index[edge['source']],index[edge['target']]
        incoming[dst]+=1;outgoing[src]+=1
        reads[src]+=edge['relation']=='reads';writes[src]+=edge['relation']=='writes'
        if edge['relation'] in BASE_RELATIONS:
            relation=BASE_RELATIONS.index(edge['relation']);edges.extend(([src,dst,relation],[dst,src,relation+len(BASE_RELATIONS)]))
    features=[];types=[];truncations=0
    for i,node in enumerate(nodes):
        attrs=node.get('attributes',{});width=attrs.get('width');width=float(width) if isinstance(width,(float,int)) and width>0 else 0
        # Attributes are already white-listed by export_graph. No business/gold status is encoded.
        semantic_keys=('trigger','expected_behavior','temporal_relation','related_signals','explicit_freedom','scope','claims',
            'width','roles','clock_domain','reset_value','datatype','signed','edge','polarity','reset_type','order','alternatives',
            'claim_kind','claim_subject','requirement_claim','origin','interpretation_status')
        semantic={k:attrs[k] for k in semantic_keys if k in attrs}
        text=f"{node['type']} {node.get('subtype') or ''} {node['text']} "+json.dumps(semantic,sort_keys=True,ensure_ascii=False)
        struct=[math.log1p(incoming[i]),math.log1p(outgoing[i]),math.log1p(width),math.log1p(reads[i]),math.log1p(writes[i]),
            float(attrs.get('binding_state')=='verified'),float(attrs.get('implemented',node['type'] in ('IR','RTL'))),
            float(attrs.get('interpretation_status') in ('canonical_verified','reviewed')),float(bool(attrs.get('clock_domain')))]
        struct.extend(float(node['type']=='Evidence' and attrs.get('status')==status) for status in TOOL_STATUSES)
        features.append(encoder.encode(text)+struct);types.append(NODE_TYPES.index(node['type']))
        if isinstance(encoder,HashEncoder):truncations+=len(text)>encoder.manifest['max_chars']
    query_features=[]
    for query in queries:
        query_features.append(dict(id=query['id'],node=index[query['id']],alternatives=[dict(id=b['id'],
            embedding=encoder.encode(f"{query.get('kind','choice')} {b.get('kind','behavior_change')} {b.get('value','')}")) for b in query['alternatives']]))
    result=dict(schema_version=FEATURE_VERSION,input_hash=graph['input_hash'],encoder=encoder.manifest,
        node_ids=[n['id'] for n in nodes],node_types=[n['type'] for n in nodes],x=features,types=types,edges=edges,queries=query_features,
        feature_dim=encoder.dimension+len(STRUCTURAL_FEATURES),alternative_dim=encoder.dimension,
        structural_feature_names=list(STRUCTURAL_FEATURES),representations=graph.get('representations',['Spec','IR','RTL']),
        truncated_nodes=truncations if isinstance(encoder,HashEncoder) else None,include_local_witness=graph.get('include_local_witness',True))
    result['cache_key']=digest([graph['input_hash'],encoder.manifest,FEATURE_VERSION]);result['feature_hash']=digest(result)
    return result

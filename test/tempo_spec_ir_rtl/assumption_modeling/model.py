"""Small CPU relation-message-passing encoder with exact NumPy gradients.

Optional ML runtime: NumPy only. Frozen features are never optimized. This
backend also provides a no-message-passing MLP baseline with the same heads.
"""
import numpy as np
from .features import NODE_TYPES,RELATIONS
from experiments.contribution1.losses import masked_losses


class MultiTaskModel:
    def __init__(self,feature_dim,alternative_dim,hidden_dim=64,layers=2,seed=17,architecture='gnn',dropout=0.0):
        if architecture not in ('gnn','mlp'):raise ValueError('architecture must be gnn or mlp')
        if not 0 <= dropout < 1 or min(feature_dim, alternative_dim, hidden_dim) < 1 or layers < 0:
            raise ValueError('invalid model dimensions/layers/dropout')
        self.config=dict(feature_dim=feature_dim,alternative_dim=alternative_dim,hidden_dim=hidden_dim,
                         layers=layers,seed=seed,architecture=architecture,dropout=dropout)
        self.rng=np.random.default_rng(seed);self.params={};h=hidden_dim
        def parameter(name,shape,zero=False):
            self.params[name]=np.zeros(shape) if zero else self.rng.normal(0,1/np.sqrt(shape[-2] if len(shape)>1 else shape[-1]),shape)
        parameter('type_W',(len(NODE_TYPES),feature_dim,h));parameter('type_b',(len(NODE_TYPES),h),True)
        for layer in range(layers):
            parameter(f'self_{layer}',(h,h));parameter(f'bias_{layer}',(h,),True)
            if architecture=='gnn':parameter(f'rel_{layer}',(len(RELATIONS),h,h))
        parameter('status_W',(h,4));parameter('status_b',(4,),True)
        parameter('mismatch_W',(2*h,));parameter('mismatch_b',(1,),True)
        parameter('query_h',(h,h));parameter('query_b',(alternative_dim,h));parameter('query_bias',(h,),True)
        parameter('impact_W',(4*h,));parameter('impact_b',(1,),True)
        for name in ('observable','cost'):
            parameter(name+'_W',(2*h,));parameter(name+'_b',(1,),True)

    def forward(self,features,training=False):
        p=self.params;hsize=self.config['hidden_dim'];x=np.asarray(features['x'],dtype=float);types=np.asarray(features['types'])
        projected=np.zeros((len(x),hsize))
        for kind in range(len(NODE_TYPES)):
            selected=types==kind
            projected[selected]=x[selected]@p['type_W'][kind]+p['type_b'][kind]
        h0=np.tanh(projected);h=h0
        edges=np.asarray(features['edges'],dtype=int).reshape(-1,3);layer_cache=[]
        for layer in range(self.config['layers']):
            incoming=h@p[f'self_{layer}']+p[f'bias_{layer}']+h
            aggregates=[]
            if self.config['architecture']=='gnn':
                for relation in range(len(RELATIONS)):
                    selected=edges[edges[:,2]==relation];src,dst=selected[:,0],selected[:,1]
                    targets,indices,degree=np.unique(dst,return_inverse=True,return_counts=True)
                    average=np.zeros((len(targets),hsize));np.add.at(average,indices,h[src]);average/=degree[:,None]
                    if len(targets):incoming[targets]+=average@p[f'rel_{layer}'][relation]
                    aggregates.append((average,src,indices,degree,targets))
            centered=incoming-incoming.mean(axis=1,keepdims=True)
            inv=1/np.sqrt(np.mean(centered**2,axis=1,keepdims=True)+1e-5)
            normalized=centered*inv;activated=np.tanh(normalized)
            rate=self.config['dropout'] if training else 0
            mask=(self.rng.random(activated.shape)>=rate)/(1-rate) if rate else np.ones_like(activated)
            layer_cache.append((h,aggregates,normalized,inv,activated,mask));h=activated*mask
        pool=h.mean(axis=0);outputs={};query_cache={}
        for query in features['queries']:
            index=query['node'];ha=h[index];context=np.concatenate((ha,pool))
            out=dict(status=ha@p['status_W']+p['status_b'],mismatch=float(context@p['mismatch_W']+p['mismatch_b'][0]),alternatives={})
            alts={}
            for alternative in query['alternatives']:
                emb=np.asarray(alternative['embedding']);q=np.tanh(ha@p['query_h']+emb@p['query_b']+p['query_bias'])
                tiled=np.broadcast_to(q,h.shape)
                pair=np.concatenate((tiled,h,tiled*h,np.abs(tiled-h)),axis=1)
                change_context=np.concatenate((q,pool))
                altout=dict(impact=pair@p['impact_W']+p['impact_b'][0])
                for name in ('observable','cost'):altout[name]=float(change_context@p[name+'_W']+p[name+'_b'][0])
                out['alternatives'][alternative['id']]=altout
                alts[alternative['id']]=(emb,q,pair,change_context)
            outputs[query['id']]=out;query_cache[query['id']]=(index,context,alts)
        return outputs,(x,types,h0,layer_cache,h,query_cache)

    def loss_and_grad(self,features,labels,weights=None,training=False):
        outputs,cache=self.forward(features,training)
        losses,upstream=masked_losses(outputs,labels,features['node_ids'],weights)
        if not losses:return {},None
        p=self.params;grads={k:np.zeros_like(v) for k,v in p.items()}
        x,types,h0,layers,h,queries=cache;dh=np.zeros_like(h);width=h.shape[1]
        for identifier,(index,context,alts) in queries.items():
            ha=h[index];grad=upstream.get((identifier,None,'status'))
            if grad is not None:
                grads['status_W']+=np.outer(ha,grad);grads['status_b']+=grad;dh[index]+=grad@p['status_W'].T
            grad=upstream.get((identifier,None,'mismatch'))
            if grad is not None:
                grads['mismatch_W']+=context*grad;grads['mismatch_b']+=grad
                dcontext=p['mismatch_W']*grad;dh[index]+=dcontext[:width];dh+=dcontext[width:]/len(h)
            for aid,(emb,q,pair,change_context) in alts.items():
                dq=np.zeros(width);grad=upstream.get((identifier,aid,'impact'))
                if grad is not None:
                    grads['impact_W']+=pair.T@grad;grads['impact_b']+=grad.sum()
                    dpair=grad[:,None]*p['impact_W'][None,:]
                    dq+=dpair[:,:width].sum(axis=0);dh+=dpair[:,width:2*width]
                    dq+=(dpair[:,2*width:3*width]*h).sum(axis=0);dh+=dpair[:,2*width:3*width]*q
                    sign=np.sign(q-h);dq+=(dpair[:,3*width:]*sign).sum(axis=0);dh-=dpair[:,3*width:]*sign
                for name in ('observable','cost'):
                    grad=upstream.get((identifier,aid,name))
                    if grad is not None:
                        grads[name+'_W']+=change_context*grad;grads[name+'_b']+=grad
                        dc=p[name+'_W']*grad;dq+=dc[:width];dh+=dc[width:]/len(h)
                dz=dq*(1-q*q);grads['query_h']+=np.outer(ha,dz);grads['query_b']+=np.outer(emb,dz);grads['query_bias']+=dz
                dh[index]+=dz@p['query_h'].T
        for layer in reversed(range(len(layers))):
            previous,aggregates,norm,inv,activated,mask=layers[layer]
            dnorm=dh*mask*(1-activated**2)
            dz=inv*(dnorm-dnorm.mean(axis=1,keepdims=True)-norm*(dnorm*norm).mean(axis=1,keepdims=True))
            grads[f'self_{layer}']+=previous.T@dz;grads[f'bias_{layer}']+=dz.sum(axis=0)
            dh=dz@p[f'self_{layer}'].T+dz
            for relation,(average,src,indices,degree,targets) in enumerate(aggregates):
                if not len(targets):continue
                grads[f'rel_{layer}'][relation]+=average.T@dz[targets]
                daverage=dz[targets]@p[f'rel_{layer}'][relation].T
                np.add.at(dh,src,daverage[indices]/degree[indices,None])
        dz=dh*(1-h0*h0)
        for kind in range(len(NODE_TYPES)):
            selected=types==kind;grads['type_W'][kind]+=x[selected].T@dz[selected];grads['type_b'][kind]+=dz[selected].sum(axis=0)
        return losses,grads

    @property
    def trainable_parameters(self):return sum(v.size for v in self.params.values())

    def save(self,path,optimizer=None,extra=None):
        import json
        state={f'param:{k}':v for k,v in self.params.items()}
        state['metadata']=np.array(json.dumps(dict(config=self.config,extra=extra or {},rng=self.rng.bit_generator.state)))
        if optimizer:
            state.update({f'm:{k}':v for k,v in optimizer.m.items()});state.update({f'v:{k}':v for k,v in optimizer.v.items()})
            state['optimizer_step']=np.array(optimizer.t)
        np.savez_compressed(path,**state)

    @classmethod
    def load(cls,path):
        import json
        data=np.load(path,allow_pickle=False);meta=json.loads(str(data['metadata']));model=cls(**meta['config'])
        for key in model.params:model.params[key]=data['param:'+key].copy()
        model.rng.bit_generator.state=meta['rng']
        return model,meta.get('extra',{})


class AdamW:
    def __init__(self,params,learning_rate=.001,weight_decay=.0001):
        self.lr,self.decay,self.t=learning_rate,weight_decay,0
        self.m={k:np.zeros_like(v) for k,v in params.items()};self.v={k:np.zeros_like(v) for k,v in params.items()}

    def step(self,params,grads,clip=1.0):
        if grads is None:return False
        norm=np.sqrt(sum(np.sum(g*g) for g in grads.values()))
        if not np.isfinite(norm):raise ValueError('nonfinite training gradients')
        scale=min(1,clip/(norm+1e-12));self.t+=1
        for key in params:
            g=grads[key]*scale;self.m[key]=.9*self.m[key]+.1*g;self.v[key]=.999*self.v[key]+.001*g*g
            params[key]*=1-self.lr*self.decay
            params[key]-=self.lr*(self.m[key]/(1-.9**self.t))/(np.sqrt(self.v[key]/(1-.999**self.t))+1e-8)
        return True

    def restore(self,path):
        data=np.load(path,allow_pickle=False);self.t=int(data['optimizer_step'])
        for key in self.m:self.m[key]=data['m:'+key].copy();self.v[key]=data['v:'+key].copy()

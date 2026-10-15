import json, numpy as np, sys
D=json.load(open(sys.argv[1])); laps=D['laps']; step=D['grid_step']
names={376:'La Source',2404:'Les Combes',3036:'Bruxelles',3802:'Pouhon',4478:'Campus',4900:'Stavelot',6696:'Bus Stop'}
best=[l for l in laps if l['lap']==D['best']][0]
def A(l,k): return np.array(l['data'][k])
G=np.arange(len(best['data']['Speed']))*step
out=[]
for c,nm in names.items():
    i=int(c/step); a=max(0,i-int(350/step)); b=min(len(G)-1,i+int(250/step))
    row={'name':nm,'dist':c,'laps':[]}
    for l in laps:
        v,br,th,tm=A(l,'Speed'),A(l,'Brake'),A(l,'Throttle'),A(l,'time')
        seg=slice(a,b); j=a+np.argmin(v[seg])
        bk=np.flatnonzero(br[a:j]>0.1); bp=G[a+bk[0]] if len(bk) else None
        on=np.flatnonzero(th[j:b]>0.9); tp=G[j+on[0]] if len(on) else None
        row['laps'].append(dict(lap=l['lap'],entry=round(float(v[a+np.argmax(v[a:j])] if j>a else v[a]),1),
            brake_pt=None if bp is None else int(c-bp), peak_brake=round(float(br[a:j].max()*100),0) if j>a else 0,
            vmin=round(float(v[j]),1), apex=int(G[j]), full_thr=None if tp is None else int(tp-G[j]),
            exit=round(float(v[min(b,j+int(150/step))]),1), seg_time=round(float(tm[b]-tm[a]),3)))
    out.append(row)
# time delta of each lap vs best, by segment between corners
bt=A(best,'time')
for l in laps:
    d=A(l,'time')-bt
    l['delta_final']=float(d[-1])
json.dump(out,open(sys.argv[2],'w'))
for r in out:
    print('\n'+r['name'],r['dist'])
    for x in r['laps']: print('  ',x)

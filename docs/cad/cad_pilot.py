import json, math, os, sys

import os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = {'dims': json.load(open(os.path.join(ROOT, 'flange-standards-data/data/dimensions/asme-b16-5.json')))}
DATA['dims'] = [[0,0,r['size'],r['size_label'],r['dn'],r['rating'],r['od'],r['th'],r['rf'],r['rf_height'],r['bc'],r['bolts'],r['bolt_hole'],r.get('hub'),r.get('length'),r.get('hub_weld'),r.get('weight'),None] for r in DATA['dims']]

def row4():
    for r in DATA['dims']:
        if r[0] == 0 and r[1] == 0 and r[2] == '4' and r[5] == '150':
            return r
    raise SystemExit('row not found')

R = {'od':6,'th':7,'rf':8,'rfh':9,'bc':10,'bolts':11,'hole':12,'hub':13,'len':14,'hubw':15,'w':16}
PIPE_ID = {15:15.8,20:21.0,25:26.6,32:35.1,40:40.9,50:52.5,65:62.7,80:77.9,100:102.3,125:126.6,150:154.1,200:202.7,250:254.5,300:303.2,400:381.0,500:477.8,600:574.6}
PIPE_OD = {50:60.3,80:88.9,100:114.3,150:168.3,200:219.1,250:273,300:323.9,400:406.4,500:508,600:610}

def dims():
    r = row4()
    dn = r[4]; D = r[R['od']]; C = r[R['th']]; A = r[R['hub']]; H = r[R['len']]; dw = r[R['hubw']]
    RF = r[R['rf']]; f = r[R['rfh']]; K = r[R['bc']]; n = r[R['bolts']]; dh = r[R['hole']]
    B = PIPE_ID.get(dn, PIPE_OD.get(dn, dn*1.05)*0.92)
    return dict(D=D,C=C,A=A,H=H,dw=dw,B=B,RF=RF,f=f,K=K,n=n,dh=dh,dn=dn,std=r[R['w']])

def formula(d):
    D,C,A,H,dw,B,RF,f,K,n,dh = d['D'],d['C'],d['A'],d['H'],d['dw'],d['B'],d['RF'],d['f'],d['K'],d['n'],d['dh']
    L = H - C
    ring = math.pi/4*(D*D-B*B)*C
    hub = math.pi/12*L*(A*A + A*dw + dw*dw) - math.pi/4*B*B*L
    rf = math.pi/4*(RF*RF-B*B)*f
    holes = n*math.pi/4*dh*dh*C
    V = ring + hub + rf - holes
    return dict(ring=ring, hub=hub, rf=rf, holes=holes, V=V, W=V*7.85e-6)

def build_cad(d):
    import cadquery as cq
    D,C,A,H,dw,B,RF,f,K,n,dh = d['D'],d['C'],d['A'],d['H'],d['dw'],d['B'],d['RF'],d['f'],d['K'],d['n'],d['dh']
    L = H - C
    body = cq.Workplane('XY').circle(D/2).extrude(C)
    rf = cq.Workplane('XY').workplane(offset=-f).circle(RF/2).circle(B/2).extrude(f)
    hub = cq.Workplane('XY').circle(A/2).workplane(offset=L).circle(dw/2).loft().translate((0,0,C))
    solid = body.union(rf).union(hub)
    bore = cq.Workplane('XY').workplane(offset=-f).circle(B/2).extrude(H + f)
    solid = solid.cut(bore)
    holes = None
    for i in range(n):
        ang = 2*math.pi*i/n
        x = K/2*math.cos(ang); y = K/2*math.sin(ang)
        cyl = cq.Workplane('XY').center(x,y).circle(dh/2).extrude(C)
        holes = cyl if holes is None else holes.union(cyl)
    solid = solid.cut(holes)
    return solid

if __name__ == '__main__':
    d = dims()
    print('dims', d)
    fv = formula(d)
    print('formula', {k: round(v,3) for k,v in fv.items()})
    solid = build_cad(d)
    vol = solid.val().Volume()
    print('cad volume mm3', vol)
    print('error %', (vol - fv['V'])/fv['V']*100)
    out = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(out, exist_ok=True)
    try:
        from cadquery import exporters
        exporters.export(solid, os.path.join(out,'wn_4_150.step'))
        exporters.export(solid, os.path.join(out,'wn_4_150.stl'))
        print('exported step/stl')
    except Exception as e:
        print('export failed', e)

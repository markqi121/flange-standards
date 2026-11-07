import struct, math, sys

def read_stl(path):
    data = open(path,'rb').read()
    # binary STL
    if len(data) >= 84:
        n = struct.unpack('<I', data[80:84])[0]
        if 84 + 50*n == len(data):
            tris=[]
            off=84
            for _ in range(n):
                vals = struct.unpack('<12fH', data[off:off+50])
                off += 50
                # normal + 3 vertices
                tris.append((vals[3:6], vals[6:9], vals[9:12], vals[0:3]))
            return tris
    # ascii
    tris=[]; verts=[]; normal=(0,0,0)
    for line in data.decode('utf-8','ignore').splitlines():
        p=line.strip().split()
        if not p: continue
        if p[0]=='facet': normal=tuple(float(x) for x in p[2:5])
        elif p[0]=='vertex':
            verts.append(tuple(float(x) for x in p[1:4]))
            if len(verts)==3:
                tris.append((verts[0],verts[1],verts[2],normal)); verts=[]
    return tris

def norm(v):
    l=math.sqrt(sum(x*x for x in v)) or 1
    return tuple(x/l for x in v)
def sub(a,b): return tuple(a[i]-b[i] for i in range(3))
def cross(a,b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def dot(a,b): return sum(a[i]*b[i] for i in range(3))

def render(path, out_svg, W=900, H=700, front=False):
    tris=read_stl(path)
    # rotate: camera direction
    d=norm((1.1,1.35,-0.85 if front else 0.85))
    up=(0,0,1)
    right=norm(cross(up,d))
    up2=cross(d,right)
    pts=[]
    for a,b,c,n in tris:
        q=[]
        for p in (a,b,c):
            q.append((dot(p,right), dot(p,up2), dot(p,d)))
        pts.append((q,n))
    xs=[p[0] for tri,_ in pts for p in tri]; ys=[p[1] for tri,_ in pts for p in tri]
    minx,maxx=min(xs),max(xs); miny,maxy=min(ys),max(ys)
    pad=60
    scale=min((W-2*pad)/(maxx-minx),(H-2*pad)/(maxy-miny))
    cx=(minx+maxx)/2; cy=(miny+maxy)/2
    light=norm((0.5,0.7,1.2))
    polys=[]
    for tri,n in pts:
        # recompute normal in rotated frame for shading
        p0,p1,p2=tri
        rn=norm(cross((sub(p1,p0)),(sub(p2,p0))))
        # light in rotated frame
        lx=(dot(light,right),dot(light,up2),dot(light,d))
        diff=max(0,dot(rn,lx))
        shade=0.30+0.70*diff
        # steel blue-grey
        base=(144,160,176)
        col=tuple(min(255,int(c*shade+18)) for c in base)
        depth=(p0[2]+p1[2]+p2[2])/3
        poly=[((p[0]-cx)*scale+W/2, H/2-(p[1]-cy)*scale) for p in tri]
        polys.append((depth, poly, col, rn))
    polys.sort(key=lambda x:x[0])
    parts=['<?xml version="1.0" encoding="UTF-8"?>',
           f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
           '<g stroke-linejoin="round">']
    for depth, poly, col, rn in polys:
        # backface cull? keep all sorted; closed mesh: backfaces hidden
        # skip faces pointing away from camera (rn.z < 0) to reduce artifacts
        if rn[2] < -0.05:
            continue
        color='#%02x%02x%02x' % col
        pts_s=' '.join(f'{x:.2f},{y:.2f}' for x,y in poly)
        parts.append(f'<polygon points="{pts_s}" fill="{color}" stroke="{color}" stroke-width="0.6"/>')
    parts.append('</g></svg>')
    open(out_svg,'w').write('\n'.join(parts))
    print('wrote',out_svg,'tris',len(tris))

if __name__=='__main__':
    render(sys.argv[1] if len(sys.argv)>1 else 'wn_4_150.stl',
           sys.argv[2] if len(sys.argv)>2 else 'wn_4_150_3d.svg',
           front=(len(sys.argv)>3 and sys.argv[3]=='front'))

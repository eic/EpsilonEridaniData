#!/usr/bin/env python3
"""Preliminary calibration: do human decisions agree with the AI judges/panels?
Joins eval/decisions (human) with eval/judgments (AI order-stable consensus, using the latest
prompt version each pair was judged under).

Part of the eval pipeline (make_pairs -> judge -> label -> calibration/power). Keep the README's
"Labelling and analysis" section in sync when these scripts' flags or behaviour change."""
import collections, glob, json, itertools
import tcdata

def modal(ws): return collections.Counter(ws).most_common(1)[0][0] if ws else None

def _ver(r):
    pf=r['judge']['prompt_file']
    return 3 if 'v3' in pf else 2 if 'v2' in pf else 1

def judge_consensus(spec):
    rs=[json.load(open(f)) for f in glob.glob(str(tcdata.ROOT/'eval'/'judgments'/'*.json'))]
    rs=[r for r in rs if r['judge']['spec']==spec]
    # Per pair, use the LATEST prompt version this judge scored it under (v3 quad pairs and the
    # v2 deck/Hopf history both count; we never mix versions within a single pair's consensus).
    best=collections.defaultdict(int)
    for r in rs: best[r['pair_id']]=max(best[r['pair_id']],_ver(r))
    rs=[r for r in rs if _ver(r)==best[r['pair_id']]]
    byp=collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rs: byp[r['pair_id']][r['order']].append(r['winner_arm'])
    out={}
    for pid,po in byp.items():
        a,b=modal(po.get('ab',[])),modal(po.get('ba',[]))
        out[pid]= a if (a is not None and a==b) else 'UNSTABLE'
    return out

dec={}
for f in glob.glob(str(tcdata.ROOT/'eval'/'decisions'/'*.json')):
    d=json.load(open(f)); dec[d['pair_id']]=d['winner_arm']
print(f"human decisions: {len(dec)}  dist={dict(collections.Counter(dec.values()))}")

judges=['gpt-5.5','grok','opus','sonnet','deepseek']
C={j:judge_consensus(j) for j in judges}

print("\nper-judge agreement with human (pairs human labelled AND judge order-stable):")
for j in judges:
    shared=[p for p in dec if p in C[j] and C[j][p]!='UNSTABLE']
    agree=sum(1 for p in shared if dec[p]==C[j][p])
    print(f"  {j:<9}: {agree}/{len(shared)} agree" + (f"  ({100*agree/len(shared):.0f}%)" if shared else ""))

print("\npanel: both order-stable AND agree -> does human agree? (the headline rule)")
for x,y in [('grok','sonnet'),('grok','opus'),('gpt-5.5','sonnet'),('opus','sonnet')]:
    pids=[p for p in dec if p in C[x] and p in C[y] and C[x][p]!='UNSTABLE' and C[y][p]!='UNSTABLE' and C[x][p]==C[y][p]]
    ag=sum(1 for p in pids if dec[p]==C[x][p])
    print(f"  {x}+{y} consensus on {len(pids)} labelled pairs; human agrees {ag}/{len(pids)}" + (f" ({100*ag/len(pids):.0f}%)" if pids else ""))

print("\nall strong judges (grok,opus,sonnet,gpt-5.5) order-stable AND unanimous -> human:")
strong=['grok','opus','sonnet','gpt-5.5']
pids=[]
for p in dec:
    vs=[C[j].get(p) for j in strong]
    if all(v is not None and v!='UNSTABLE' for v in vs) and len(set(vs))==1: pids.append(p)
ag=sum(1 for p in pids if dec[p]==C['grok'][p])
print(f"  unanimous on {len(pids)} labelled pairs; human agrees {ag}/{len(pids)}")

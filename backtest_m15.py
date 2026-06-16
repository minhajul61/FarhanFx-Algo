"""FarhanFX — M15 Timeframe Backtest (all strategies + variants)"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import MetaTrader5 as mt5
from datetime import datetime, timezone

mt5.initialize(timeout=15000)
mt5.symbol_select('XAUUSDc', True)

# Same calendar window as H1 anchor
h1_ref = mt5.copy_rates_from_pos('XAUUSDc', mt5.TIMEFRAME_H1, 0, 5000)
start_dt = datetime.fromtimestamp(int(h1_ref[0]['time']), tz=timezone.utc)

rates    = mt5.copy_rates_from('XAUUSDc', mt5.TIMEFRAME_M15, start_dt, 40000)
d1_rates = mt5.copy_rates_from('XAUUSDc', mt5.TIMEFRAME_D1,  start_dt, 500)
h4_rates = mt5.copy_rates_from('XAUUSDc', mt5.TIMEFRAME_H4,  start_dt, 2000)
mt5.shutdown()

print(f"M15 bars: {len(rates)}  D1: {len(d1_rates)}  H4: {len(h4_rates)}")
print(f"Window: {start_dt.strftime('%Y-%m-%d')} -> now\n")

o=[float(r['open'])  for r in rates]; h=[float(r['high'])  for r in rates]
l=[float(r['low'])   for r in rates]; c=[float(r['close']) for r in rates]
t=[int(r['time'])    for r in rates]; N=len(c)
hrs=[datetime.fromtimestamp(ts,tz=timezone.utc).hour for ts in t]

d1_c=[float(r['close']) for r in d1_rates]; d1_t=[int(r['time']) for r in d1_rates]
h4_c=[float(r['close']) for r in h4_rates]; h4_t=[int(r['time']) for r in h4_rates]

# ─── Indicators ───────────────────────────────────────────────────────────────
def ema(p,n):
    n=min(n,len(p)-1) or 1; k=2/(n+1); e=[p[0]]
    for x in p[1:]: e.append(x*k+e[-1]*(1-k))
    return e

def atr_s(hi,lo,cl,n=14):
    tr=[hi[0]-lo[0]]
    for i in range(1,len(cl)):
        tr.append(max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1])))
    n=min(n,len(tr)); a=[sum(tr[:n])/n]
    for i in range(n,len(tr)): a.append((a[-1]*(n-1)+tr[i])/n)
    return [a[0]]*(n-1)+a

def rsi_s(cl,n=14):
    g=[0.0]; lo2=[0.0]
    for i in range(1,len(cl)):
        d=cl[i]-cl[i-1]; g.append(max(d,0)); lo2.append(max(-d,0))
    if len(g)<=n: return [50.0]*len(cl)
    ag=sum(g[1:n+1])/n; al=sum(lo2[1:n+1])/n; r=[50.0]*n
    for i in range(n,len(cl)):
        ag=(ag*(n-1)+g[i])/n; al=(al*(n-1)+lo2[i])/n
        r.append(100.0 if al==0 else 100-100/(1+ag/al))
    return r

def supertrend(hi,lo,cl,n=10,m=3.0):
    atr=atr_s(hi,lo,cl,n); up=[0.0]*len(cl); dn=[0.0]*len(cl); tr=[1]*len(cl)
    for i in range(1,len(cl)):
        mid=(hi[i]+lo[i])/2; bu=mid-m*atr[i]; bd=mid+m*atr[i]
        up[i]=bu if bu>up[i-1] or cl[i-1]<up[i-1] else up[i-1]
        dn[i]=bd if bd<dn[i-1] or cl[i-1]>dn[i-1] else dn[i-1]
        if tr[i-1]==-1 and cl[i]>dn[i]: tr[i]=1
        elif tr[i-1]==1 and cl[i]<up[i]: tr[i]=-1
        else: tr[i]=tr[i-1]
    return tr

# ─── Pre-compute ──────────────────────────────────────────────────────────────
print("Computing indicators...", flush=True)
atr  = atr_s(h,l,c,14); rsi  = rsi_s(c,14)
e5   = ema(c,5);   e8=ema(c,8)
e20  = ema(c,20);  e21=ema(c,21); e50=ema(c,50); e200=ema(c,min(200,N-1))
st   = supertrend(h,l,c,10,3.0)

d1e50=ema(d1_c,50); d1e200=ema(d1_c,min(200,len(d1_c)-1))
h4e50=ema(h4_c,50); h4e200=ema(h4_c,min(200,len(h4_c)-1))
d1b=[d1_c[i]>d1e50[i]>d1e200[i] for i in range(len(d1_c))]
d1s=[d1_c[i]<d1e50[i]<d1e200[i] for i in range(len(d1_c))]
h4b=[h4_c[i]>h4e50[i]>h4e200[i] for i in range(len(h4_c))]
h4s=[h4_c[i]<h4e50[i]<h4e200[i] for i in range(len(h4_c))]

def gtd(ts):
    for j in range(len(d1_t)-1,-1,-1):
        if d1_t[j]<=ts: return d1b[j],d1s[j]
    return False,False

def gth(ts):
    for j in range(len(h4_t)-1,-1,-1):
        if h4_t[j]<=ts: return h4b[j],h4s[j]
    return False,False

# ─── Simulator (150 bars max hold = ~37.5 hours = similar to H1 80 bars) ──────
ATR_MIN = 2.5  # M15 min ATR

def sim(ents, sl_m=1.5, tp_m=3.0, mx=150):
    trades=[]
    for (i,sig,sm,tm) in ents:
        sl_d=atr[i]*sm; tp_d=atr[i]*tm
        sl=c[i]-sl_d if sig==1 else c[i]+sl_d
        tp=c[i]+tp_d if sig==1 else c[i]-tp_d
        res=None
        for j in range(i+1,min(i+mx,N)):
            if sig==1:
                if l[j]<=sl: res=-sl_d; break
                if h[j]>=tp: res=+tp_d; break
            else:
                if h[j]>=sl: res=-sl_d; break
                if l[j]<=tp: res=+tp_d; break
        if res is None:
            pnl=c[min(i+mx,N-1)]-c[i] if sig==1 else c[i]-c[min(i+mx,N-1)]
            res=pnl
        trades.append(res)
    return trades

def show(label, trades):
    if len(trades)<5:
        print(f"    {label:<42} -- {len(trades)} trades (too few)")
        return 0,0
    wins=[x for x in trades if x>0]; loss=[x for x in trades if x<=0]
    wr=len(wins)/len(trades)*100
    pf=sum(wins)/abs(sum(loss)) if loss and sum(loss)!=0 else 999
    net=sum(trades)
    mk=">>>" if wr>=55 else (">> " if wr>=50 else "   ")
    print(f"  {mk} {label:<42} {len(trades):>4} | WR:{wr:>5.1f}% | PF:{pf:>4.2f} | Net:${net:>8.2f}")
    return wr, len(trades)

print("=== M15 BACKTEST — XAUUSDc ===\n")
print(f"     {'Strategy':<42} {'Trd':>4} | Win Rate  | PF    | Net PnL")
print("-"*75)

results = {}

# ─── 1. M2B/M2S ───────────────────────────────────────────────────────────────
ents=[]
for i in range(20,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(13<=hr<16)): continue
    rising=e20[i]>e20[i-8]>e20[i-16]; falling=e20[i]<e20[i-8]<e20[i-16]
    tb=min(l[max(0,i-10):i])<=e20[max(0,i-5)]+atr[i]*0.6
    ts2=max(h[max(0,i-10):i])>=e20[max(0,i-5)]-atr[i]*0.6
    sig=0
    if rising  and c[i]>e20[i] and c[i]>c[i-1] and tb  and c[i]>o[i] and 45<rsi[i]<62 and e20[i]>e50[i]: sig=1
    if falling and c[i]<e20[i] and c[i]<c[i-1] and ts2 and c[i]<o[i] and 38<rsi[i]<55 and e20[i]<e50[i]: sig=-1
    if not sig: continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    if sig==1 and (not db or not hb): continue
    if sig==-1 and (not dbs or not hbs): continue
    ents.append((i,sig,1.5,3.0))
wr,n=show("M2B/M2S (RSI+EMA stack+D1+H4)", sim(ents))
results["M2B/M2S"]=wr

# ─── 1b. M2B/M2S loose RSI ────────────────────────────────────────────────────
ents=[]
for i in range(20,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(13<=hr<16)): continue
    rising=e20[i]>e20[i-8]>e20[i-16]; falling=e20[i]<e20[i-8]<e20[i-16]
    tb=min(l[max(0,i-10):i])<=e20[max(0,i-5)]+atr[i]*0.6
    ts2=max(h[max(0,i-10):i])>=e20[max(0,i-5)]-atr[i]*0.6
    sig=0
    if rising  and c[i]>e20[i] and c[i]>c[i-1] and tb  and c[i]>o[i] and 40<rsi[i]<65: sig=1
    if falling and c[i]<e20[i] and c[i]<c[i-1] and ts2 and c[i]<o[i] and 35<rsi[i]<60: sig=-1
    if not sig: continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    if sig==1 and (not db or not hb): continue
    if sig==-1 and (not dbs or not hbs): continue
    ents.append((i,sig,1.5,3.0))
show("M2B/M2S (loose RSI 40-65)", sim(ents))

# ─── 1c. M2B/M2S EMA touch strict ────────────────────────────────────────────
ents=[]
for i in range(20,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(13<=hr<16)): continue
    rising=e20[i]>e20[i-8]>e20[i-16]; falling=e20[i]<e20[i-8]<e20[i-16]
    # stricter: price must be within 0.3 ATR of EMA20 (direct EMA touch)
    near_ema_b = abs(c[i]-e20[i])<atr[i]*0.4
    near_ema_s = abs(c[i]-e20[i])<atr[i]*0.4
    tb=min(l[max(0,i-10):i])<=e20[max(0,i-5)]+atr[i]*0.4
    ts2=max(h[max(0,i-10):i])>=e20[max(0,i-5)]-atr[i]*0.4
    sig=0
    if rising  and c[i]>e20[i] and c[i]>c[i-1] and tb  and c[i]>o[i] and 45<rsi[i]<65 and e20[i]>e50[i]: sig=1
    if falling and c[i]<e20[i] and c[i]<c[i-1] and ts2 and c[i]<o[i] and 35<rsi[i]<55 and e20[i]<e50[i]: sig=-1
    if not sig: continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    if sig==1 and (not db or not hb): continue
    if sig==-1 and (not dbs or not hbs): continue
    ents.append((i,sig,1.5,3.0))
show("M2B/M2S (strict EMA touch 0.4)", sim(ents))

# ─── 2. Pin Bar SR ────────────────────────────────────────────────────────────
ph=[]; pl=[]
for i in range(5,N-5):
    if all(h[i]>=h[j] for j in range(max(0,i-5),min(N,i+6)) if j!=i): ph.append((i,h[i]))
    if all(l[i]<=l[j] for j in range(max(0,i-5),min(N,i+6)) if j!=i): pl.append((i,l[i]))
def near(i,st,tol=0.7):
    if st==1:
        if abs(l[i]-e21[i])<atr[i]*tol: return True
        for (pi,pv) in reversed(pl):
            if 5<i-pi<80: return abs(l[i]-pv)<atr[i]*tol
    else:
        if abs(h[i]-e21[i])<atr[i]*tol: return True
        for (pi,pv) in reversed(ph):
            if 5<i-pi<80: return abs(h[i]-pv)<atr[i]*tol
    return False
ents=[]
for i in range(15,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    body=abs(c[i]-o[i]); dw=min(c[i],o[i])-l[i]; uw=h[i]-max(c[i],o[i])
    sig=0
    if dw>=body*2.2 and dw>uw*2.0 and body>atr[i]*0.04 and near(i,1) and db and hb: sig=1
    elif uw>=body*2.2 and uw>dw*2.0 and body>atr[i]*0.04 and near(i,-1) and dbs and hbs: sig=-1
    if sig: ents.append((i,sig,1.5,3.0))
wr,n=show("Pin Bar SR (EMA21/pivot D1+H4)", sim(ents))
results["Pin Bar SR"]=wr

# ─── 3. Engulfing Trend ───────────────────────────────────────────────────────
ents=[]
for i in range(15,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    body=abs(c[i]-o[i])
    be=c[i-1]<o[i-1] and c[i]>o[i] and c[i]>o[i-1] and o[i]<c[i-1] and body>atr[i]*0.3
    se=c[i-1]>o[i-1] and c[i]<o[i] and c[i]<o[i-1] and o[i]>c[i-1] and body>atr[i]*0.3
    at=abs(c[i]-e21[i])<atr[i]*0.7
    sig=0
    if be and at and c[i]>e50[i] and db and hb:   sig=1
    elif se and at and c[i]<e50[i] and dbs and hbs: sig=-1
    if sig: ents.append((i,sig,1.5,3.0))
wr,n=show("Engulfing Trend (EMA21+D1+H4)", sim(ents))
results["Engulfing Trend"]=wr

# ─── 4. PA Confluence ─────────────────────────────────────────────────────────
ents=[]
for i in range(210,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    body=abs(c[i]-o[i]); dw=min(c[i],o[i])-l[i]; uw=h[i]-max(c[i],o[i])
    bp=dw>=body*2.2 and dw>uw*1.5 and body>atr[i]*0.04
    sp=uw>=body*2.2 and uw>dw*1.5 and body>atr[i]*0.04
    bng=c[i-1]<o[i-1] and c[i]>o[i] and c[i]>o[i-1] and o[i]<c[i-1] and body>atr[i]*0.25
    sng=c[i-1]>o[i-1] and c[i]<o[i] and c[i]<o[i-1] and o[i]>c[i-1] and body>atr[i]*0.25
    ae=abs(c[i]-e21[i])<atr[i]*0.8
    tb=c[i]>e50[i] and e50[i]>e200[i]; ts=c[i]<e50[i] and e50[i]<e200[i]
    sig=0
    if (bp or bng) and ae and tb and db and hb:   sig=1
    elif (sp or sng) and ae and ts and dbs and hbs: sig=-1
    if sig: ents.append((i,sig,1.5,3.0))
wr,n=show("PA Confluence (Trend+Level+PA)", sim(ents))
results["PA Confluence"]=wr

# ─── 5. Trend Continuation ────────────────────────────────────────────────────
ents=[]
for i in range(210,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    n50=abs(c[i]-e50[i])<atr[i]*0.8
    sig=0
    if c[i]>e200[i] and st[i]==1 and n50 and c[i]>o[i] and 45<rsi[i]<65 and db and hb: sig=1
    elif c[i]<e200[i] and st[i]==-1 and n50 and c[i]<o[i] and 35<rsi[i]<55 and dbs and hbs: sig=-1
    if sig: ents.append((i,sig,1.5,3.0))
wr,n=show("Trend Continuation (EMA200+ST+EMA50)", sim(ents))
results["Trend Continuation"]=wr

# ─── 6. False Breakout / IB FBO ──────────────────────────────────────────────
ents=[]
for i in range(15,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    ib=h[i-1]<h[i-2] and l[i-1]>l[i-2]
    sig=0
    if ib and l[i]<l[i-2] and c[i]>l[i-2] and c[i]>o[i]: sig=1
    elif ib and h[i]>h[i-2] and c[i]<h[i-2] and c[i]<o[i]: sig=-1
    if sig: ents.append((i,sig,1.5,3.0))
show("False Breakout IB FBO (no trend filter)", sim(ents))

ents=[]
for i in range(15,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    ib=h[i-1]<h[i-2] and l[i-1]>l[i-2]
    sig=0
    if ib and l[i]<l[i-2] and c[i]>l[i-2] and c[i]>o[i] and db and hb: sig=1
    elif ib and h[i]>h[i-2] and c[i]<h[i-2] and c[i]<o[i] and dbs and hbs: sig=-1
    if sig: ents.append((i,sig,1.5,3.0))
show("False Breakout IB FBO + D1+H4 trend", sim(ents))

# ─── 7. Supertrend flip ──────────────────────────────────────────────────────
ents=[]
for i in range(12,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    sig=0
    if st[i-1]==-1 and st[i]==1 and db and hb: sig=1
    elif st[i-1]==1 and st[i]==-1 and dbs and hbs: sig=-1
    if sig: ents.append((i,sig,1.5,3.0))
show("Supertrend Flip (D1+H4)", sim(ents))

# ─── 8. EMA Cross with RSI ───────────────────────────────────────────────────
ents=[]
for i in range(55,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    sig=0
    if e20[i]>e50[i] and e20[i-1]<=e50[i-1] and 45<rsi[i]<65 and c[i]>e200[i] and db and hb: sig=1
    elif e20[i]<e50[i] and e20[i-1]>=e50[i-1] and 35<rsi[i]<55 and c[i]<e200[i] and dbs and hbs: sig=-1
    if sig: ents.append((i,sig,1.5,3.0))
show("EMA20/50 Cross + RSI + EMA200", sim(ents))

# ─── 9. 3-Bar Reversal ───────────────────────────────────────────────────────
ents=[]
for i in range(15,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    body=abs(c[i]-o[i]); no_ob=not (h[i-1]>h[i-2] and l[i-1]<l[i-2])
    sig=0
    if c[i-2]<o[i-2] and l[i-1]<l[i-2] and no_ob and c[i]>h[i-2] and body>atr[i]*0.2 and db and hb: sig=1
    elif c[i-2]>o[i-2] and h[i-1]>h[i-2] and no_ob and c[i]<l[i-2] and body>atr[i]*0.2 and dbs and hbs: sig=-1
    if sig: ents.append((i,sig,1.5,3.0))
show("3-Bar Reversal (D1+H4)", sim(ents))

# ─── 10. Scalper EMA 5/8 ─────────────────────────────────────────────────────
ents=[]
for i in range(10,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(12<=hr<16)): continue
    sig=0
    if e5[i]>e8[i] and e5[i-1]<=e8[i-1] and rsi[i]>55 and c[i]>e20[i]: sig=1
    elif e5[i]<e8[i] and e5[i-1]>=e8[i-1] and rsi[i]<45 and c[i]<e20[i]: sig=-1
    if sig: ents.append((i,sig,1.0,2.0))
show("Scalper EMA5/8 (SL1x TP2x)", sim(ents,sl_m=1.0,tp_m=2.0))

# ─── 11. M2B/M2S different TP/SL ─────────────────────────────────────────────
print()
ents_m2b=[]
for i in range(20,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(13<=hr<16)): continue
    rising=e20[i]>e20[i-8]>e20[i-16]; falling=e20[i]<e20[i-8]<e20[i-16]
    tb=min(l[max(0,i-10):i])<=e20[max(0,i-5)]+atr[i]*0.6
    ts2=max(h[max(0,i-10):i])>=e20[max(0,i-5)]-atr[i]*0.6
    sig=0
    if rising  and c[i]>e20[i] and c[i]>c[i-1] and tb  and c[i]>o[i] and 45<rsi[i]<62 and e20[i]>e50[i]: sig=1
    if falling and c[i]<e20[i] and c[i]<c[i-1] and ts2 and c[i]<o[i] and 38<rsi[i]<55 and e20[i]<e50[i]: sig=-1
    if not sig: continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    if sig==1 and (not db or not hb): continue
    if sig==-1 and (not dbs or not hbs): continue
    ents_m2b.append((i,sig,0,0))  # placeholder, override below

# Re-run with different SL/TP for same signals
ents_raw=[]
for i in range(20,N-1):
    if atr[i]<ATR_MIN: continue
    hr=hrs[i]
    if not ((7<=hr<11)or(13<=hr<16)): continue
    rising=e20[i]>e20[i-8]>e20[i-16]; falling=e20[i]<e20[i-8]<e20[i-16]
    tb=min(l[max(0,i-10):i])<=e20[max(0,i-5)]+atr[i]*0.6
    ts2=max(h[max(0,i-10):i])>=e20[max(0,i-5)]-atr[i]*0.6
    sig=0
    if rising  and c[i]>e20[i] and c[i]>c[i-1] and tb  and c[i]>o[i] and 45<rsi[i]<62 and e20[i]>e50[i]: sig=1
    if falling and c[i]<e20[i] and c[i]<c[i-1] and ts2 and c[i]<o[i] and 38<rsi[i]<55 and e20[i]<e50[i]: sig=-1
    if not sig: continue
    db,dbs=gtd(t[i]); hb,hbs=gth(t[i])
    if sig==1 and (not db or not hb): continue
    if sig==-1 and (not dbs or not hbs): continue
    ents_raw.append((i,sig,1.5,3.0))

show("M2B/M2S SL1.0 TP2.0", sim([(i,s,1.0,2.0) for (i,s,_,__) in ents_raw], sl_m=1.0, tp_m=2.0))
show("M2B/M2S SL1.5 TP4.0", sim([(i,s,1.5,4.0) for (i,s,_,__) in ents_raw], sl_m=1.5, tp_m=4.0))
show("M2B/M2S SL1.0 TP3.0", sim([(i,s,1.0,3.0) for (i,s,_,__) in ents_raw], sl_m=1.0, tp_m=3.0))

# ─── SUMMARY ─────────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("M15 STRATEGY SUMMARY:")
for s,wr in results.items():
    tag = "KEEP" if wr>=50 else "DROP"
    star = " ***" if wr>=55 else ""
    print(f"  {s:<35} WR:{wr:>5.1f}%  [{tag}]{star}")
print("\n[DONE]")

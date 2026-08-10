from __future__ import annotations
import html, json, os, re, smtplib, ssl, sys, time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path
import requests
from openai import OpenAI

ROOT = Path(__file__).resolve().parent
DOCS, ARCHIVE = ROOT / 'docs', ROOT / 'docs' / 'archive'
Q1_FILE = ROOT / 'config' / 'q1_journals.txt'
NCBI_BASE = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'
NCBI_EMAIL = os.getenv('NCBI_EMAIL', '').strip()
OPENAI_MODEL = os.getenv('OPENAI_MODEL', '').strip() or 'gpt-5-mini'
SITE_URL = os.getenv('SITE_URL', 'https://CARDIOLOGY-risk.github.io/ACS-weekly/').strip()

SEARCH_QUERY = r'''(
 "acute coronary syndrome"[Title/Abstract] OR "acute myocardial infarction"[Title/Abstract]
 OR STEMI[Title/Abstract] OR NSTEMI[Title/Abstract] OR "myocardial infarction"[Title/Abstract]
 OR "cardiac rehabilitation"[Title/Abstract] OR "cardiovascular rehabilitation"[Title/Abstract]
 OR "exercise-based rehabilitation"[Title/Abstract] OR "secondary prevention"[Title/Abstract]
 OR "lipid lowering"[Title/Abstract] OR "LDL-C"[Title/Abstract] OR "LDL cholesterol"[Title/Abstract]
 OR statin*[Title/Abstract] OR ezetimibe[Title/Abstract] OR PCSK9[Title/Abstract]
 OR inclisiran[Title/Abstract] OR obicetrapib[Title/Abstract] OR antiplatelet*[Title/Abstract]
 OR "smoking cessation"[Title/Abstract] OR "physical activity"[Title/Abstract]
 OR adherence[Title/Abstract] OR "lipoprotein(a)"[Title/Abstract]
 OR "residual cardiovascular risk"[Title/Abstract]
) AND humans[MeSH Terms]'''.strip()

@dataclass
class Article:
    pmid: str; title: str; journal: str; pubdate: str; doi: str; abstract: str
    publication_types: list[str]; authors: str; pubmed_url: str

def norm(s): return re.sub(r'[^a-z0-9]+', '', s.lower())

def load_q1():
    titles=[x.strip() for x in Q1_FILE.read_text(encoding='utf-8').splitlines() if x.strip() and not x.strip().startswith('#')]
    return {norm(x) for x in titles}, titles

def ncbi_get(endpoint, params):
    p=dict(params); p['tool']='ACS_secondary_prevention_weekly'
    if NCBI_EMAIL: p['email']=NCBI_EMAIL
    r=requests.get(f'{NCBI_BASE}/{endpoint}', params=p, timeout=45); r.raise_for_status(); time.sleep(.36); return r

def search_pubmed(start, end):
    term=f'({SEARCH_QUERY}) AND ("{start:%Y/%m/%d}"[Date - Publication] : "{end:%Y/%m/%d}"[Date - Publication])'
    r=ncbi_get('esearch.fcgi', {'db':'pubmed','term':term,'retmode':'json','retmax':250,'sort':'pub date'})
    return r.json().get('esearchresult',{}).get('idlist',[])

def txt(el): return ''.join(el.itertext()).strip() if el is not None else ''

def parse_pubmed(ids):
    if not ids: return []
    root=ET.fromstring(ncbi_get('efetch.fcgi', {'db':'pubmed','id':','.join(ids),'retmode':'xml'}).text)
    out=[]
    for item in root.findall('.//PubmedArticle'):
        m=item.find('MedlineCitation'); a=m.find('Article') if m is not None else None
        if m is None or a is None: continue
        pmid=txt(m.find('PMID')); title=txt(a.find('ArticleTitle')); journal=txt(a.find('Journal/Title'))
        abstract='\n'.join(filter(None,[txt(x) for x in a.findall('Abstract/AbstractText')]))
        ptypes=[txt(x) for x in a.findall('PublicationTypeList/PublicationType')]
        auth=[]
        for au in a.findall('AuthorList/Author')[:8]:
            coll=txt(au.find('CollectiveName'))
            name=coll or ' '.join(filter(None,[txt(au.find('LastName')),txt(au.find('Initials'))]))
            if name: auth.append(name)
        authors=', '.join(auth) + (', et al.' if len(a.findall('AuthorList/Author'))>8 else '')
        doi=''
        for aid in item.findall('PubmedData/ArticleIdList/ArticleId'):
            if aid.attrib.get('IdType')=='doi': doi=txt(aid); break
        ad=a.find('ArticleDate')
        if ad is not None:
            y,mn,d=txt(ad.find('Year')),txt(ad.find('Month')),txt(ad.find('Day'))
            pubdate='-'.join(filter(None,[y,mn.zfill(2) if mn.isdigit() else mn,d.zfill(2) if d.isdigit() else d]))
        else:
            pd=a.find('Journal/JournalIssue/PubDate')
            pubdate=' '.join(filter(None,[txt(pd.find('Year')) if pd is not None else '',txt(pd.find('Month')) if pd is not None else '',txt(pd.find('Day')) if pd is not None else '',txt(pd.find('MedlineDate')) if pd is not None else '']))
        out.append(Article(pmid,title,journal,pubdate,doi,abstract,ptypes,authors,f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/'))
    return out

def analyze(articles):
    empty={'top_papers':[],'sections':{'Acute Coronary Syndrome':[],'Cardiac Rehabilitation':[],'Secondary Cardiovascular Prevention':[]}}
    if not articles: return empty
    compact=[{'pmid':a.pmid,'title':a.title,'journal':a.journal,'publication_date':a.pubdate,'publication_types':a.publication_types,'abstract':a.abstract[:6500]} for a in articles]
    prompt=f'''You are a cardiology scientific literature editor. Assess the supplied PubMed papers for relevance to acute coronary syndrome, cardiac rehabilitation, or secondary cardiovascular prevention. Be conservative. Do not invent data. Return VALID JSON ONLY with exactly this structure:
{{"top_papers":["PMID"],"papers":[{{"pmid":"...","include":true,"category":"Acute Coronary Syndrome|Cardiac Rehabilitation|Secondary Cardiovascular Prevention","study_type":"...","objective":"...","key_results":"...","clinical_relevance":"...","why_it_matters":"...","rating":1,"must_read":false}}]}}
Rules: rating integer 1-5; MUST READ rare; exclude tangential papers; each included PMID belongs to one category; concise 1-3 sentence fields. ARTICLES: {json.dumps(compact,ensure_ascii=False)}'''
    raw=OpenAI().responses.create(model=OPENAI_MODEL,input=prompt).output_text.strip()
    raw=re.sub(r'^```(?:json)?\s*','',raw); raw=re.sub(r'\s*```$','',raw)
    data=json.loads(raw); by={a.pmid:a for a in articles}; secs=empty['sections']; scored={}
    for p in data.get('papers',[]):
        pmid=str(p.get('pmid','')); cat=p.get('category')
        if not p.get('include') or pmid not in by or cat not in secs: continue
        rec=asdict(by[pmid]); rec.update(p); rec['rating']=max(1,min(5,int(rec.get('rating',1)))); secs[cat].append(rec); scored[pmid]=rec
    tops=[scored[str(x)] for x in data.get('top_papers',[])[:3] if str(x) in scored]
    if not tops:
        tops=sorted([x for v in secs.values() for x in v],key=lambda x:(x.get('must_read',False),x.get('rating',0)),reverse=True)[:3]
    return {'top_papers':tops,'sections':secs}

def esc(x): return html.escape(str(x or ''))
def stars(n): return '★'*n+'☆'*(5-n)

def card(p):
    badge='<span class="must">★ MUST READ</span>' if p.get('must_read') else ''
    doi=f' · <a href="https://doi.org/{esc(p.get("doi"))}" target="_blank">DOI</a>' if p.get('doi') else ''
    return f'''<article class="paper"><div class="paper-top">{badge}<span class="rating">{stars(int(p.get('rating',1)))}</span></div><h3>{esc(p.get('title'))}</h3><p class="meta"><strong>{esc(p.get('journal'))}</strong> · {esc(p.get('pubdate'))} · {esc(p.get('study_type'))}</p><p class="authors">{esc(p.get('authors'))}</p><p><strong>Objective.</strong> {esc(p.get('objective'))}</p><p><strong>Key results.</strong> {esc(p.get('key_results'))}</p><p><strong>Clinical relevance.</strong> {esc(p.get('clinical_relevance'))}</p><p class="why"><strong>Why this paper matters.</strong> {esc(p.get('why_it_matters'))}</p><p><a href="{esc(p.get('pubmed_url'))}" target="_blank">PubMed · PMID {esc(p.get('pmid'))}</a>{doi}</p></article>'''

def render(report,start,end,total,q1n):
    top=''.join(f'<a class="top-card" href="{esc(p["pubmed_url"])}" target="_blank"><span>{"★ MUST READ" if p.get("must_read") else stars(int(p.get("rating",1)))}</span><strong>{esc(p["title"])}</strong><small>{esc(p["journal"])}</small></a>' for p in report['top_papers']) or '<p class="empty">No eligible priority papers identified this week.</p>'
    sections=''
    for name,papers in report['sections'].items(): sections+=f'<section><h2>{esc(name)}</h2>{"".join(card(p) for p in papers) or "<p class=\"empty\">No relevant new Q1 PubMed-indexed publications identified this week.</p>"}</section>'
    final=sum(len(v) for v in report['sections'].values())
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ACS & Secondary Prevention Weekly · {end:%d %b %Y}</title><style>:root{{--navy:#0b2d4d;--blue:#145a8d;--bg:#f4f7fa;--text:#1f2933;--muted:#687784;--line:#dce4ea;--gold:#966d00}}*{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;background:var(--bg);color:var(--text);line-height:1.55}}header{{background:linear-gradient(135deg,var(--navy),var(--blue));color:white;padding:42px 20px}}.wrap{{max-width:980px;margin:auto}}h1{{margin:0 0 8px;font-size:clamp(2rem,5vw,3.2rem)}}header p{{margin:4px 0;opacity:.9}}main{{padding:28px 20px 60px}}section{{margin:32px 0}}h2{{color:var(--navy)}}.top-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.top-card,.paper{{background:white;border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 4px 15px rgba(15,45,70,.06)}}.top-card{{display:flex;flex-direction:column;gap:7px;text-decoration:none;color:inherit}}.top-card span,.rating{{color:var(--gold)}}.paper{{margin:14px 0;padding:22px}}.paper h3{{margin:8px 0 5px}}.meta,.authors{{color:var(--muted);font-size:.9rem}}.paper-top{{display:flex;justify-content:space-between}}.must{{font-weight:800;color:#9a5e00}}.why{{background:#f6f9fb;border-left:4px solid var(--blue);padding:10px 12px}}a{{color:var(--blue)}}.stats,.empty{{background:white;border:1px solid var(--line);border-radius:12px;padding:14px;color:var(--muted)}}footer{{border-top:1px solid var(--line);padding:25px 20px;color:var(--muted);font-size:.82rem;background:white}}@media(max-width:760px){{.top-grid{{grid-template-columns:1fr}}}}</style></head><body><header><div class="wrap"><h1>ACS & Secondary Prevention Weekly</h1><p>Curated PubMed literature surveillance</p><p>{start:%d %B %Y} – {end:%d %B %Y}</p></div></header><main class="wrap"><div class="stats">PubMed candidates: {total} · Q1 allow-list matches: {q1n} · Final selection: {final}</div><section><h2>Top papers this week</h2><div class="top-grid">{top}</div></section>{sections}</main><footer><div class="wrap"><strong>ACS & Secondary Prevention Weekly</strong><br>Automated PubMed surveillance. Q1 eligibility uses the repository's manually maintained allow-list. Verify the original publication before clinical or scientific use.</div></footer></body></html>'''

def send_email(report,end):
    user=os.getenv('SMTP_USER','').strip(); pwd=os.getenv('SMTP_APP_PASSWORD','').strip(); rec=os.getenv('REPORT_RECIPIENT','').strip()
    if not all([user,pwd,rec]): print('Email not configured; skipping.'); return
    bullets=''.join(f'<li style="margin-bottom:12px"><strong>{esc(p["title"])}</strong><br>{esc(p["journal"])}<br>{esc(p.get("why_it_matters"))}</li>' for p in report['top_papers'][:3]) or '<li>No priority eligible papers this week.</li>'
    msg=EmailMessage(); msg['Subject']=f'ACS & Secondary Prevention Weekly — {end:%d %b %Y}'; msg['From']=user; msg['To']=rec
    msg.set_content(f'Weekly report: {SITE_URL}')
    msg.add_alternative(f'<html><body style="font-family:Arial"><h2>ACS & Secondary Prevention Weekly</h2><ol>{bullets}</ol><p><a href="{esc(SITE_URL)}" style="background:#0b2d4d;color:white;padding:12px 18px;text-decoration:none;border-radius:8px">View full weekly report</a></p></body></html>',subtype='html')
    with smtplib.SMTP_SSL('smtp.gmail.com',465,context=ssl.create_default_context()) as s: s.login(user,pwd); s.send_message(msg)

def main():
    end=date.today(); start=end-timedelta(days=7); allowed,_=load_q1(); ids=search_pubmed(start,end); arts=parse_pubmed(ids)
    filtered=[a for a in arts if norm(a.journal) in allowed and not ({x.lower() for x in a.publication_types}&{'case reports','letter','editorial','comment','published erratum'})]
    print(f'PubMed candidates: {len(arts)}; Q1/type filter: {len(filtered)}')
    report=analyze(filtered); out=render(report,start,end,len(arts),len(filtered)); DOCS.mkdir(exist_ok=True); ARCHIVE.mkdir(parents=True,exist_ok=True)
    (DOCS/'index.html').write_text(out,encoding='utf-8'); (ARCHIVE/f'{end.isoformat()}.html').write_text(out,encoding='utf-8'); send_email(report,end)

if __name__=='__main__':
    try: main()
    except Exception as e: print(f'ERROR: {e}',file=sys.stderr); raise

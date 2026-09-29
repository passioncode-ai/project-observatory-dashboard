#!/usr/bin/env python3
"""Render reviewed Markdown and data into the static article. No network or user state."""
from pathlib import Path
import hashlib, html, json, math, re, sys
ROOT=Path(__file__).resolve().parents[1]
SITE=ROOT/'site'
URL='https://observatory.sshlg.me'
# The article stays at its original address; the product it describes now lives on PassionCode.ai.
PRODUCT='https://passioncode.ai/observatory/'
ORG={'@type':'Organization','@id':'https://passioncode.ai/#organization','name':'PassionCode.ai','url':'https://passioncode.ai/'}

def inline(s):
 s=html.escape(s)
 s=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',s)
 s=re.sub(r'\*\*(.+?)\*\*',r'<strong>\1</strong>',s)
 return re.sub(r'`([^`]+)`',r'<code>\1</code>',s)

def slug(s):return re.sub(r'[^a-z0-9]+','-',s.lower()).strip('-')

def flow(title,steps,caption):
 return '<figure class="editorial-figure flow-figure"><h3>'+title+'</h3><ol class="figure-flow">'+''.join('<li><strong>'+a+'</strong><span>'+b+'</span></li>' for a,b in steps)+'</ol><figcaption>'+caption+'</figcaption></figure>'

def figures():
 data=json.loads((ROOT/'docs/site/case-study.json').read_text())
 # The plotted quantities come from the reviewed receipt, not a second list of values.
 counts=[x['count'] for x in data['surfaces']]
 assert counts==[66,136] and sum(counts)==data['total']
 chart='<figure class="editorial-figure" id="cleanup-chart"><p class="figure-kicker">REVIEWED LOCAL CLEANUP · 14 SEPTEMBER 2026</p><h3>Where the replacements happened</h3><p class="figure-note">Value-replacement events, on the same scale</p><div class="bar-chart">'
 for name,n in zip(['claude-mem SQLite','Chroma SQLite'],counts):
  chart+=f'<div class="bar-row"><div class="bar-label"><span>{name}</span><strong>{n}</strong></div><div class="bar-track" aria-hidden="true"><svg viewBox="0 0 150 8" preserveAspectRatio="none"><rect width="{n}" height="8"/></svg></div></div>'
 chart+='<div class="bar-axis" aria-hidden="true"><span>0</span><span>50</span><span>100</span><span>150</span></div></div><figcaption>'+str(data['total'])+' replacement events in total. Several events can involve the same key. This is not a count of unique credentials or confirmed breaches. <a href="https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/site/CASE-STUDY.md">Source and scope</a>.</figcaption></figure>'
 return {
 'origin':flow('How the project grew',[('Find the projects','What is in these folders?'),('Connect the context','Which repository and services?'),('Follow the access','Where are the credentials?'),('Keep observing','What changed since I looked?')],'The progression of the project, based on my own account. These are stages of the idea, not a dated incident timeline.'),
 'replacements':chart,
 'copy-route':flow('How a credential can acquire another copy',[('Tool output','A command prints a value.'),('Agent session','The result becomes available to the agent.'),('Retained artifact','A transcript or optional memory integration may keep it.')],'Illustrative pathway. Retention depends on the tool, permissions and installed integrations. It does not reconstruct every copy in the cleanup record.'),
 'cartoon':'<figure class="editorial-figure cartoon"><img src="../assets/credential-copies-cartoon.png" width="1536" height="1024" loading="lazy" decoding="async" alt="A fox guards an .env safe while a printer makes key copies labelled session, memory and export. The key stayed out of Git. The copies had other plans."><figcaption>An original editorial cartoon: keeping a key out of Git does not account for copies in the rest of the workflow.</figcaption></figure>'
 }

def build(check=False):
 text=(ROOT/'docs/content/long-post.md').read_text();title=text.splitlines()[0][2:];f=figures();body=[];heads=[]
 for block in text.split('\n\n')[1:]:
  block=block.strip()
  if not block:continue
  if block.startswith('<!-- figure:'):
   key=re.fullmatch(r'<!-- figure: ([a-z-]+) -->',block)[1];body.append(f[key]);continue
  if block.startswith('## '):
   label=block[3:];anchor=slug(label);heads.append((anchor,label));body.append(f'<h2 id="{anchor}">'+inline(label)+'</h2>')
  elif block.startswith('> '):body.append('<blockquote class="pullquote"><p>'+inline(block[2:])+'</p></blockquote>')
  else:body.append('<p>'+inline(block)+'</p>')
 words=len(re.sub(r'<!--.*?-->','',text).split());minutes=math.ceil(words/220)
 deck='I started with a map of my project folders. Repeated key warnings led me into Claude Code workflows, old conversations and local memory stores.'
 p=SITE/'field-notes/index.html';page=p.read_text()
 page=re.sub(r'<title>.*?</title>','<title>API key copies in agent workflows: my Observatory story</title>',page)
 page=re.sub(r'<div class="article-header">.*?<div class="article-body">', '<div class="article-header"><p class="eyebrow">FIELD NOTES / PROJECT OBSERVATORY</p><h1>'+html.escape(title)+'</h1><p class="article-deck">'+deck+'</p><p class="article-meta"><a rel="author" href="https://passioncode.ai/">PassionCode.ai</a> · <time datetime="2026-09-21">21 September 2026</time> · '+str(minutes)+' min read</p><details class="article-toc"><summary>In this story</summary><ol>'+''.join(f'<li><a href="#{a}">{html.escape(t)}</a></li>' for a,t in heads)+'</ol></details></div><div class="article-body">',page,flags=re.S)
 page=re.sub(r'<div class="article-body">.*?</div></article>','<div class="article-body">'+''.join(body)+'</div></article>',page,flags=re.S)
 article={'@context':'https://schema.org','@type':'BlogPosting','@id':URL+'/field-notes/#article','headline':title,'description':deck,'datePublished':'2026-09-21','dateModified':'2026-09-29','author':ORG,'publisher':ORG,'image':{'@type':'ImageObject','url':URL+'/assets/observatory-cover.png','width':1672,'height':941},'mainEntityOfPage':URL+'/field-notes/','inLanguage':'en','wordCount':words,'about':[{'@type':'Thing','name':'API credential retention in agent workflows'},{'@type':'SoftwareApplication','name':'Project Observatory','url':PRODUCT}]}
 page=re.sub(r'<script type="application/ld\+json">.*?</script>','<script type="application/ld+json">'+json.dumps(article,separators=(',',':'))+'</script>',page,count=1,flags=re.S)
 if check:
  if p.read_text()!=page:raise SystemExit("Article differs from reviewed Markdown; run tools/build_article.py")
 else:p.write_text(page)
 return words,minutes

if __name__=='__main__':
 words,minutes=build(check="--check" in sys.argv);print(f'Article rendered: {words} source words; {minutes} min at 220 words/min')

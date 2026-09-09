"""Build self-contained article and embeddable figures. Run with uv --with markdown."""
from pathlib import Path
import subprocess
import markdown

here = Path(__file__).resolve().parent
root = here.parents[3]
subprocess.run(['bun', 'build', str(here / 'main.ts'), '--target', 'browser', '--format', 'iife', '--outfile', str(here / 'main.js')], check=True, cwd=root)
css = (here / 'style.css').read_text()
js = (here / 'main.js').read_text().replace('</script', '<\\/script')
figures = {name: (here / f'{name}.html').read_text() for name in ['traffic', 'architecture']}

def document(title, body, extra=''):
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title><style>{css}\n{extra}</style></head>
<body>{body}<script>{js}</script></body></html>'''

body = markdown.markdown((root / 'docs/drafts/typeahead-sequel.md').read_text(), extensions=['extra'])
for name, figure in figures.items():
    body = body.replace(f'<!-- {name} -->', figure)
article = '<header class="page-header"><a href="https://pckt.blog/b/waow-tech">waow / typeahead</a><span>Draft · September 5, 2026</span></header><main class="article">' + body + '<footer class="page-footer"><p>Draft for editing · Data collected September 5, 2026.</p></footer></main>'
(root / 'docs/drafts/typeahead-sequel.html').write_text(document('typeahead at the race', article))
for name, figure in figures.items():
    (here / f'{name}-embed.html').write_text(document(f'Typeahead — {name}', figure, 'body{background:white}.reader-figure{margin:0 auto;max-width:720px}'))
print('Built article and two standalone embeds.')

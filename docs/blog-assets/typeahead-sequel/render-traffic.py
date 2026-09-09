"""Render only the saved aggregate evidence; no credentials or network calls."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

root = Path(__file__).resolve().parent
rows = json.loads((root / 'traffic-evidence.json').read_text())['data']
labels = [r['window'][11:16] for r in rows]
counts = [r['searches'] for r in rows]
p95 = [r['p95_ms'] for r in rows]
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 12,
    'axes.spines.top': False, 'axes.spines.right': False,
    'axes.spines.left': False, 'axes.spines.bottom': False,
    'axes.labelcolor': '#152940', 'text.color': '#152940',
    'xtick.color': '#52647a', 'ytick.color': '#52647a'})
fig, axes = plt.subplots(2, 1, figsize=(11, 6.8), sharex=True,
    gridspec_kw={'hspace': .32}, facecolor='#f7f9fc')
fig.suptitle('More searches, still a small answer', x=.10, y=.975,
    ha='left', fontsize=23, fontweight='bold')
for ax in axes:
    ax.set_facecolor('#f7f9fc')
    ax.grid(axis='y', alpha=.18)
    ax.set_axisbelow(True)
    ax.tick_params(length=0, pad=7)
axes[0].bar(labels, counts, color='#245cba', width=.6)
axes[0].set_ylim(0, 6500)
axes[0].set_ylabel('Searches / 15 min', labelpad=15)
peak = max(range(len(counts)), key=counts.__getitem__)
axes[0].text(peak, counts[peak] + 357, f'{counts[peak]:,}',
    ha='center', color='#245cba', weight='bold')
axes[1].plot(labels, p95, color='#08766c', lw=3, marker='o', ms=5)
axes[1].set_ylim(0, 25)
axes[1].set_ylabel('Engine p95 (ms)', labelpad=15)
axes[1].set_xlabel('September 4, 2026 · window start, UTC', labelpad=14)
fig.text(.10, .035,
    'Source: Logfire completed search spans. These windows precede 1-in-10 sampling.\n'
    'Engine duration only; excludes edge/network latency and ingest health.',
    fontsize=10, color='#52647a')
fig.subplots_adjust(left=.13, right=.96, top=.86, bottom=.20)
for ext in ('svg', 'png'):
    fig.savefig(root / f'traffic.{ext}', dpi=220, facecolor=fig.get_facecolor())

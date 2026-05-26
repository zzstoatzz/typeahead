import json, datetime

d = json.load(open('/tmp/pckt_backup.json'))
blobs = json.load(open('/tmp/blobs.json'))
items = d['content']['items']

def img_block(key, alt):
    m = blobs[key]
    return {
        "$type": "blog.pckt.block.image",
        "attrs": {
            "align": "center",
            "alt": alt,
            "blob": {"$type": "blob", "ref": {"$link": m['cid']}, "mimeType": "image/png", "size": m['size']},
            "naturalWidth": m['w'],
            "naturalHeight": m['h'],
        },
    }

ALTS = {
    'diagram.png': "typeahead.waow.tech architecture: continuous ingest, offline snapshot build, and bounded snapshot+overlay serving",
    'eq-keys.png': "keys_d(h) equals the union over j of { d:sigma : sigma is a prefix of dot-suffix s_j, with 1 <= |sigma| <= P }",
    'dns-tree.png': "a handle is a path through the DNS tree; indexing every dot-suffix lets one prefix query return an entire subtree of subdomains",
    'eq-emissions.png': "E equals the sum over all handles of |keys(h)| -- build emissions count every (key, DID) tuple",
    'eq-artifact.png': "A equals the sum over keys of min(N, deg(k)) -- the served artifact stores only the top-N capped rows",
    'cost-bars.png': "cost decoupling: build emissions grow +69% while the served artifact grows only +0.9%, because the top-N cap pushes cost into the offline build",
}

# --- inline-math rewrites on facet-free blocks (exact substring; assert hit) ---
def repl(i, old, new):
    t = items[i]['plaintext']
    assert old in t, f"block {i}: missing {old!r}"
    items[i]['plaintext'] = t.replace(old, new)

repl(25, r'$h = \ell_1.\ell_2.\cdots.\ell_k$', 'h = ℓ₁.ℓ₂.⋯.ℓₖ')
repl(25, r'$q \sqsubseteq h$', 'q ⊑ h')
repl(26, r'$s_j = \ell_{j+1}.\cdots.\ell_k$', 'sⱼ = ℓⱼ₊₁.⋯.ℓₖ')
repl(28, r'$h = \texttt{tessa.germnetwork.com}$', 'h = tessa.germnetwork.com')
repl(28, 'top-$N$', 'top-N')
repl(28, '$O(1)$', 'O(1)')
repl(32, '($E$)', '(E)')
repl(35, '($A$)', '(A)')
repl(35, 'top-$N$', 'top-N')
# block 37: $N$ -> N, facets are BEFORE byte 121 so unaffected
repl(37, '$N$', 'N')

# block 39: top-$N$ -> top-N at byte 33, facet 'd:' at [50:52] must shift -2
t39 = items[39]['plaintext']
edit_byte = len(t39[:t39.index('top-$N$')].encode())  # 33
items[39]['plaintext'] = t39.replace('top-$N$', 'top-N')
delta = 2
for f in items[39]['facets']:
    ix = f['index']
    if ix['byteStart'] >= edit_byte:
        ix['byteStart'] -= delta
        ix['byteEnd'] -= delta
# verify facet still anchors 'd:'
nb = items[39]['plaintext'].encode()
f0 = items[39]['facets'][0]['index']
assert nb[f0['byteStart']:f0['byteEnd']].decode() == 'd:', "block39 facet realign failed"

# --- replace display-math / placeholder blocks with image blocks ---
items[27] = img_block('eq-keys.png', ALTS['eq-keys.png'])
items[29] = img_block('dns-tree.png', ALTS['dns-tree.png'])
items[33] = img_block('eq-emissions.png', ALTS['eq-emissions.png'])
items[36] = img_block('eq-artifact.png', ALTS['eq-artifact.png'])
items[38] = img_block('cost-bars.png', ALTS['cost-bars.png'])

# --- assemble: drop 5..15 (vestigial), insert diagram after block 16 ---
DROP = set(range(5, 16))  # 5..15 inclusive
new_items = []
for i, b in enumerate(items):
    if i in DROP:
        continue
    new_items.append(b)
    if i == 16:
        new_items.append(img_block('diagram.png', ALTS['diagram.png']))

d['content']['items'] = new_items

# --- regenerate textContent + bump updatedAt ---
def block_text(b):
    t = b['$type']
    if t.endswith('.text') or t.endswith('.heading'):
        return b.get('plaintext', '')
    if t.endswith('.image'):
        return b.get('attrs', {}).get('alt', '')
    if t.endswith('.orderedList'):
        return "\n".join(c.get('plaintext', '') for c in b.get('content', []))
    return ''
d['textContent'] = "\n".join(s for s in (block_text(b) for b in new_items) if s)
d['updatedAt'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')

json.dump(d, open('/tmp/pckt_new.json', 'w'), ensure_ascii=False, indent=2)

# --- print new block listing for review ---
print(f"blocks: {len(items)} -> {len(new_items)}   (dropped {len(DROP)}, added 1 diagram, swapped 5)")
print("=== NEW block order ===")
for i, b in enumerate(new_items):
    t = b['$type'].replace('blog.pckt.block.', '')
    txt = b.get('plaintext', b.get('attrs', {}).get('alt', '') if t == 'image' else '')
    tag = ' [IMG]' if t == 'image' else ''
    print(f"[{i:2}] {t:13}{tag} {txt[:78]!r}")

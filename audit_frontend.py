import os
import hashlib
from pathlib import Path

root = Path(__file__).parent / 'frontend' / 'src'

print("=== Empty directories ===")
for p in sorted(root.rglob('*')):
    if p.is_dir() and not any(p.iterdir()):
        print(p.relative_to(root))

print("\n=== All files ===")
files = [f for f in root.rglob('*') if f.is_file()]
for f in sorted(files, key=lambda x: x.stat().st_size, reverse=True):
    print(f"{f.stat().st_size:>8}  {f.relative_to(root)}")

print("\n=== Duplicate content files (by first 5KB) ===")
hashes = {}
for f in files:
    try:
        content = f.read_bytes()
        key = content[:min(len(content), 5000)]
        h = hashlib.sha256(key).hexdigest()
        if h in hashes:
            print(f"DUPLICATE: {f.relative_to(root)} <=> {hashes[h].relative_to(root)}")
        else:
            hashes[h] = f
    except Exception:
        pass

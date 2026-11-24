"""Show first few chunks to understand the text structure."""
import sys

path = sys.argv[1] if len(sys.argv) > 1 else 'wiki_text.txt'
n = int(sys.argv[2]) if len(sys.argv) > 2 else 5

with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

chunks = [c.strip() for c in content.split('\n\n') if c.strip()]
print(f"Total chunks: {len(chunks):,}")
print(f"File size: {len(content)/1024/1024:.1f} MB\n")

for i, chunk in enumerate(chunks[:n]):
    print(f"{'='*60}")
    print(f"CHUNK {i+1} (len={len(chunk)}):")
    print(f"{'='*60}")
    print(chunk[:500])
    print(f"\n... (total {len(chunk)} chars)\n")
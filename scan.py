# scan.py - sectioned low-footprint scanner for the assistant
import os
import sys

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v"}
SKIP_DIRS = {
    "windows", "$recycle.bin", "system volume information",
    "node_modules", ".git", "windowsapps", "programdata",
}
# depth limit keeps the programs section small (launchers only)
SECTION_DEPTH = {"user": None, "d": None, "programs": 2}

def section_roots(section):
    if section == "user":
        home = os.path.expanduser("~")
        return [os.path.join(home, d) for d in
                ("Desktop", "Documents", "Downloads", "Videos", "Music", "Pictures")]
    if section == "d":
        return ["D:\\"]
    if section == "programs":
        return [r"C:\Program Files", r"C:\Program Files (x86)"]
    return []

def scan(section, cache_path):
    roots = section_roots(section)
    max_depth = SECTION_DEPTH.get(section)
    lines = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
            if max_depth is not None:
                if dirpath.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
                    dirnames[:] = []
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in VIDEO_EXT:
                    tag = "V"
                elif ext == ".pdf":
                    tag = "P"
                elif ext == ".exe":
                    tag = "A"
                else:
                    continue
                lines.append(f"{section}|{tag}|{os.path.join(dirpath, fn)}")
    old = []
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            old = [l.rstrip("\n") for l in f if l.strip()]
    kept = [l for l in old if not l.startswith(section + "|")]
    with open(cache_path, "w", encoding="utf-8") as f:
        all_lines = kept + lines
        f.write("\n".join(all_lines) + ("\n" if all_lines else ""))
    return len(lines)

def main():
    section, cache_path, marker_path = sys.argv[1], sys.argv[2], sys.argv[3]
    n = scan(section, cache_path)
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write(f"{section}|{n}")

if __name__ == "__main__":
    main()
"""Run every test suite in order and print one summary table.

Each suite manages its own temporary folders. Suites marked 'browser' need
Agent Browser.cmd open first; the rest run fully offline.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\Python")
REPORTS = Path(r"D:\Work\agent-state\reports")
CREATE_NO_WINDOW = 0x08000000

SUITES = [
    ("agent.selftest", True, 11),
    ("agent.missiontest", True, 13),
    ("agent.learntest", True, 10),
    ("agent.aitest", True, 7),
    ("agent.indextest", True, 7),
    ("agent.runtest", False, 9),
    ("brain.knowtest", False, 16),
    ("brain.verifytest", False, 21),
    ("brain.plantest", False, 20),
    ("brain.integratetest", False, 17),
    ("brain.ingesttest", False, 13),
    ("brain.bridgetest", False, 14),
    ("brain.lessontest", False, 12),
    ("brain.namingtest", False, 10),
    ("farsi.corpustest", False, 14),
    ("farsi.usagetest", False, 14),
    ("farsi.intenttest", False, 15),
    ("farsi.nlgtest", False, 17),
    ("farsi.executetest", False, 15),
    ("farsi.watchtest", True, 13),
]


def run_suite(name: str) -> dict:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               AGENT_ISOLATED="1")
    started = time.time()
    try:
        done = subprocess.run(
            [sys.executable, "-m", name], cwd=str(ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, creationflags=CREATE_NO_WINDOW,
        )
        out, code = (done.stdout or "") + (done.stderr or ""), done.returncode
    except subprocess.TimeoutExpired:
        out, code = "timeout after 300s", -1
    except OSError as exc:
        out, code = str(exc), -1
    lines = out.splitlines()
    passed = sum(1 for line in lines if line.strip().startswith("✓"))
    failed = [line.strip() for line in lines if line.strip().startswith("!")]
    return {"name": name, "code": code, "passed": passed, "failed": failed,
            "seconds": round(time.time() - started, 1), "output": out}


def main() -> int:
    print("جاروی کامل تست‌ها")
    print("مجموعه‌های مرورگری نیاز دارند Agent Browser.cmd باز باشد.")
    print("-" * 62)
    results = []
    for name, needs_browser, expected in SUITES:
        tag = "browser" if needs_browser else "offline"
        print(f"… {name} ({tag})", flush=True)
        report = run_suite(name)
        report["expected"] = expected
        results.append(report)
        mark = "OK " if report["code"] == 0 and not report["failed"] else "FAIL"
        print(f"  {mark}  {report['passed']}/{expected} ✓  "
              f"({report['seconds']}s, exit={report['code']})")
        for line in report["failed"][:3]:
            print("     ", line[:150])

    print("-" * 62)
    bad = [r for r in results if r["code"] != 0 or r["failed"]]
    total_checks = sum(r["passed"] for r in results)
    print(f"مجموع بررسی‌های پاس‌شده: {total_checks}")
    print(f"مجموعه‌های سالم: {len(results) - len(bad)} از {len(results)}")
    if bad:
        print("\nنیاز به رسیدگی:")
        for report in bad:
            print(f"  {report['name']}  exit={report['code']}  "
                  f"{report['passed']}/{report['expected']}")
            for line in report["failed"][:2]:
                print("     ", line[:150])
            if not report["failed"]:
                tail = report["output"].strip().splitlines()
                if tail:
                    print("     ", tail[-1][:150])

    REPORTS.mkdir(parents=True, exist_ok=True)
    target = REPORTS / f"sweep_{int(time.time())}.txt"
    chunks = []
    for report in results:
        chunks.append(f"===== {report['name']} "
                      f"(exit={report['code']}, {report['passed']}/{report['expected']}) =====")
        chunks.append(report["output"])
    target.write_text("\n".join(chunks), encoding="utf-8")
    print("\nگزارش کامل:", target)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
"""Clean Persian text files for corpus ingestion.

Removes emojis, markdown, common English words, chat AI sections, and duplicates.
Produces a clean text file ready for farsi add-file.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

# Emoji ranges (comprehensive but not exhaustive)
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)

# Common English terms to replace or remove
ENGLISH_REPLACEMENTS = {
    r"\bRPD\b": "مدل تصمیم‌گیری سریع",
    r"\bOODA\b": "حلقه مشاهده-جهت‌یابی-تصمیم-عمل",
    r"\bBig Five\b": "پنج عامل شخصیت",
    r"\bOCEAN\b": "پنج عامل شخصیت",
    r"\bSCARF\b": "مدل اسکارف",
    r"\bFBI\b": "اف‌بی‌آی",
    r"\bMIT\b": "ام‌آی‌تی",
    r"\bMBA\b": "ام‌بی‌ای",
    r"\bJWT\b": "جی‌دبلیو‌تی",
    r"\bJSON\b": "جی‌سان",
    r"\btoken\b": "توکن",
    r"\bMVP\b": "حداقل محصول قابل‌ارائه",
    r"\bCut-Loss\b": "توقف ضرر",
    r"\bBlack Swan\b": "قوی سیاه",
    r"\bHoney Traps\b": "تله‌های عسل",
    r"\bDisinformation\b": "اطلاعات غلط",
    r"\bPre-text\b": "پیش‌متن",
    r"\bTimeline\b": "خط زمانی",
    r"\bAffect Labeling\b": "برچسب‌زنی احساسات",
    r"\bOSINT\b": "اطلاعات منبع‌باز",
    r"\bElicitation\b": "استخراج اطلاعات",
    r"\bWar Gaming\b": "بازی جنگ",
    r"\bPre-Mortem\b": "پیش‌کالبدشکافی",
    r"\bGame Theory\b": "نظریه بازی‌ها",
    r"\bPrisoner's Dilemma\b": "معمای زندانی",
    r"\bTit for Tat\b": "چشم در برابر چشم",
    r"\bRepeated Games\b": "بازی‌های تکرارشونده",
    r"\bShadow of the Future\b": "سایه آینده",
    r"\bBATNA\b": "بهترین جایگزین",
    r"\bCore IP\b": "مالکیت فکری اصلی",
    r"\bGrim Trigger\b": "ماشه بی‌رحم",
    r"\bNYT Test\b": "تست نیویورک تایمز",
    r"\bReversibility Test\b": "تست برگشت‌پذیری",
    r"\bSuperforecasting\b": "پیش‌بینی سوپر",
    r"\bGood Judgment Project\b": "پروژه قضاوت خوب",
    r"\bFoxes vs Hedgehogs\b": "روباه‌ها در برابر خارپشت‌ها",
    r"\bFermi Problems\b": "مسائل فرمی",
    r"\bBypassing\b": "دور زدن",
    r"\bReframing\b": "قاب‌بندی مجدد",
    r"\bFake Engagement Bait\b": "طعمه تعامل جعلی",
    r"\bDigital Exhaust\b": "اگزوز دیجیتال",
    r"\bHidden Pain Point\b": "نقطه درد پنهان",
    r"\bMovement Mirroring\b": "آینه‌ی حرکتی",
    r"\bThe Steeple\b": "برج دست",
    r"\bAccusation Audit\b": "حسابرسی اتهام",
    r"\bParallel OODA\b": "حلقه موازی",
    r"\bPre-loaded Observation\b": "پیش‌بارگذاری مشاهده",
    r"\bSatisficing\b": "راضی‌کنندگی",
    r"\bEscalation of Commitment\b": "تعهد به تصمیم اشتباه",
    r"\bSchwerpunkt\b": "نقطه شکست",
    r"\bDecisive Point\b": "نقطه تعیین‌کننده",
    r"\bNudge\b": "تلنگر",
    r"\bBinary Tracking\b": "ردیابی باینری",
    r"\bNever Miss Twice\b": "هرگز دو بار پشت سر نه",
    r"\bRing\b": "حلقه",
    r"\bFDA\b": "سازمان غذا و دارو",
    r"\bCFO\b": "مدیر مالی",
    r"\bSMS\b": "پیامک",
    r"\bLate Night FM DJ\b": "مجری رادیو شبانه",
    r"\bMetaphysical Naturalism\b": "طبیعت‌گرایی متافیزیکی",
    r"\bPantheism\b": "پانتئیسم",
    r"\bSimulation Hypothesis\b": "فرضیه شبیه‌سازی",
    r"\bInfinite Regress\b": "بی‌نهایت‌نمایی",
    r"\bSorites Paradox\b": "پارادوکس توده",
    r"\bOntological Causality\b": "علیت هستی‌شناختی",
    r"\bTemporal Causality\b": "علیت زمانی",
}

# Patterns to remove (chat AI sections, markdown, etc.)
REMOVE_PATTERNS = [
    r"### USER.*?(?=###|$)",
    r"### ASSISTANT.*?(?=###|$)",
    r"تفکر تکمیل شد",
    r"Oops! There was an issue connecting to.*",
    r"مشکلی در اتصال به.*",
    r"هشدار امنیت محتوا:.*",
    r"^\*\*.*?\*\*$",  # **bold**
    r"^## .*$",  # ## headers
    r"^### .*$",  # ### headers
    r"^---+$",  # ---
    r"^\*\*\*.*?\*\*\*$",  # ***bold italic***
    r"```[\s\S]*?```",  # code blocks
    r"`[^`]+`",  # inline code
    r"^\s*[-*+]\s+",  # bullet points
    r"^\s*\d+\.\s+",  # numbered lists
]


def clean_text(text: str) -> str:
    """Apply all cleaning rules to the text."""
    
    # Remove emojis
    text = EMOJI_PATTERN.sub("", text)
    
    # Remove patterns
    for pattern in REMOVE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.MULTILINE | re.IGNORECASE)
    
    # Replace English terms
    for pattern, replacement in ENGLISH_REPLACEMENTS.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # Remove remaining standalone English words (2+ letters)
    text = re.sub(r"\b[A-Za-z]{2,}\b", "", text)
    
    # Clean up multiple spaces and newlines
    text = re.sub(r" +", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    
    # Remove lines that are only whitespace or punctuation
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not re.match(r"^[^\w\s]*$", stripped):
            cleaned_lines.append(line)
    
    return "\n".join(cleaned_lines)


def remove_duplicates(text: str) -> str:
    """Remove duplicate paragraphs using hash."""
    paragraphs = text.split("\n\n")
    seen_hashes = set()
    unique_paragraphs = []
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        
        # Create hash of normalized paragraph
        normalized = re.sub(r"\s+", " ", para).strip()
        para_hash = hashlib.md5(normalized.encode("utf-8")).hexdigest()
        
        if para_hash not in seen_hashes:
            seen_hashes.add(para_hash)
            unique_paragraphs.append(para)
    
    return "\n\n".join(unique_paragraphs)


def clean_file(input_path: Path, output_path: Path) -> dict:
    """Clean a text file and write the result."""
    
    if not input_path.exists():
        return {"error": f"فایل ورودی پیدا نشد: {input_path}"}
    
    # Read input
    text = input_path.read_text(encoding="utf-8")
    original_lines = len(text.split("\n"))
    original_chars = len(text)
    
    # Clean
    cleaned = clean_text(text)
    
    # Remove duplicates
    deduplicated = remove_duplicates(cleaned)
    
    # Write output
    output_path.write_text(deduplicated, encoding="utf-8")
    
    final_lines = len(deduplicated.split("\n"))
    final_chars = len(deduplicated)
    
    return {
        "input": str(input_path),
        "output": str(output_path),
        "original_lines": original_lines,
        "original_chars": original_chars,
        "final_lines": final_lines,
        "final_chars": final_chars,
        "reduction_percent": round((1 - final_chars / original_chars) * 100, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.clean",
        description="تمیز کردن فایل متنی برای پیکره",
    )
    parser.add_argument("input", type=Path, help="فایل ورودی")
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="فایل خروجی (پیش‌فرض: input-clean.txt)",
    )
    
    args = parser.parse_args(argv)
    
    if args.output is None:
        args.output = args.input.with_name(args.input.stem + "-clean.txt")
    
    result = clean_file(args.input, args.output)
    
    if "error" in result:
        print("خطا:", result["error"])
        return 1
    
    print("تمیزکاری کامل شد:")
    print(f"  ورودی: {result['input']}")
    print(f"  خروجی: {result['output']}")
    print(f"  خطوط: {result['original_lines']} → {result['final_lines']}")
    print(f"  نویسه‌ها: {result['original_chars']} → {result['final_chars']}")
    print(f"  کاهش: {result['reduction_percent']}%")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
; ===================================================
; PART 1/5 - Header, Config, Hotkeys, Command Router
; ===================================================
#Requires AutoHotkey v2.0
#SingleInstance Force
#Include agent_bridge.ahk
; ===================================================
; CONFIG
; ===================================================
; VPN: Microsoft Store build of Hotspot Shield
global VpnExe := ""
global VpnAppId := "6F71D7A7.HotspotShieldFreeVPN_nsbqstbb9qxb6!App"
global VpnWindowTitle := ""
global VpnConnectControl := ""
global GamesRoot := "D:\Games"
global VideoDownloader := "D:\Python\grab.bat"
global CommandsFile := A_ScriptDir "\commands.ini"
global LearnedMap := LoadCommands()
global LastCommand := ""
global ModeName := ""
global ModeStartTick := 0
global TimerEndTick := 0

; Sectioned disk scan
global ScanScript := "D:\Python\scan.py"
global ScanCache := A_ScriptDir "\media_cache.txt"
global ScanState := A_ScriptDir "\scan_state.txt"
global ScanMarker := A_ScriptDir "\scan_done.txt"
global ScanSections := ["user", "d", "programs"]
global ScanRunning := false
global ScanCurrentSection := ""
global ScanStartTick := 0
global LastSearch := []
global LastImageResults := []
global ImageIndexFile := "D:\Python\image_index.json"

; Time tracker
global TimeLog := Map()
global LastDailyReportDate := ""

; News briefing
global NewsScript := "D:\Python\morning_briefing.py"
global NewsLatest := "D:/Python/news_latest.txt"
global NewsGui := ""
global NewsEdit := ""
global NewsRunning := false
global NewsStartTick := 0

; Tiko-chi daily reminder
global TikoUrl := "https://tiko-chi.vercel.app/"
global TikoTime := "20:00"
global TikoStateFile := A_ScriptDir "\tiko_state.txt"
global TikoCacheFile := A_ScriptDir "\tiko_cache.txt"
global TikoLastFire := FileExist(TikoStateFile) ? Trim(FileRead(TikoStateFile, "UTF-8")) : ""

; ===== NEW FEATURE CONFIG =====
; AI (feature 12, 13): fill LlmApiKey to enable
global LlmApiKey := ""
global LlmApiUrl := "https://api.openai.com/v1/chat/completions"
global LlmModel := "gpt-4o-mini"
; Prayer times (feature 2)
global PrayerCity := "Tehran"
global PrayerScript := "D:\Python\prayer_times.py"
; Prices (feature 1)
global PricesScript := "D:\Python\prices.py"
; AI helper (feature 12, 13, translate fallback)
global AiScript := "D:\Python\ai_helper.py"
; Downloads organizer (feature 11)
global DownloadsFolder := EnvGet("USERPROFILE") "\Downloads"
; Pomodoro (feature 8)
global PomWorkMin := 25
global PomBreakMin := 5
global PomState := ""        ; "" | "work" | "break"
global PomEndTick := 0
global PomCycles := 0
; Clipboard manager (feature 9)
global ClipHistory := []
global ClipHistMax := 30
global ClipHistGui := ""
; Disk alert (feature 15)
global DiskWarnGB := 10
global LastDiskWarnDay := ""
; Window layout (feature 10)
global WindowLayoutFile := A_ScriptDir "\window_layout.txt"
; System monitor (feature 14)
global SysMonTimerOn := false

; Persian/short aliases -> exact shortcut name inside D:\Games
global AliasMap := Map(
    "هولو نایت", "Hollow Knight",
    "هولو", "Hollow Knight",
    "هالو نایت", "Hollow Knight",
    "هالو", "Hollow Knight",
    "کال اف دیوتی", "CoDWaW",
    "کال آف دیوتی", "CoDWaW",
    "cod", "CoDWaW",
    "waw", "CoDWaW",
    "ماینکرفت", "TLauncher",
    "ماین", "TLauncher",
    "تلانچر", "TLauncher",
    "سینگولاریتی", "Singularity",
    "سینگول", "Singularity"
)
global GameMap := Map()

; Scenario modes. Action tokens: "vpn" | "games" | "run|<target>" | "kill|<proc>"
global ScenarioMap := Map(
    "حالت درس", ["run|https://archive-rouge-eta.vercel.app/", "run|https://chat.qwen.ai/"],
    "حالت بازی", ["vpn", "games"],
    "حالت فیلم", ["vpn", "run|https://www.youtube.com"]
)

; Apps and websites
global AppMap := Map(
    "ایجنت", "D:\Python\console.cmd",
    "یوتیوب", "https://www.youtube.com",
    "youtube", "https://www.youtube.com",
    "گوگل", "https://www.google.com",
    "google", "https://www.google.com",
    "اینستاگرام", "https://www.instagram.com",
    "اینستا", "https://www.instagram.com",
    "instagram", "https://www.instagram.com",
    "تیکوچی", "https://tiko-chi.vercel.app/",
    "tiko", "https://tiko-chi.vercel.app/",
    "tikochi", "https://tiko-chi.vercel.app/",
    "ارشیو", "https://archive-rouge-eta.vercel.app/",
    "آرشیو", "https://archive-rouge-eta.vercel.app/",
    "archive", "https://archive-rouge-eta.vercel.app/",
    "کویین", "https://chat.qwen.ai/",
    "qwen", "https://chat.qwen.ai/",
    "دیپ سیک", "https://chat.deepseek.com/",
    "دیپسیک", "https://chat.deepseek.com/",
    "deepseek", "https://chat.deepseek.com/",
    "ماز", "https://biomaze.ir/panel/classes/categories?year=43372291-d7d2-412e-a07c-164db8321454&display=category",
    "maze", "https://biomaze.ir/panel/classes/categories?year=43372291-d7d2-412e-a07c-164db8321454&display=category",
    "biomaze", "https://biomaze.ir/panel/classes/categories?year=43372291-d7d2-412e-a07c-164db8321454&display=category",
    "chrome", "chrome.exe",
    "مرورگر", "chrome.exe",
    "telegram", "telegram.exe",
    "تلگرام", "telegram.exe"
)
; ===== VIRTUAL HAND CONFIG =====
global VHandScript := "D:\Python\virtual_hand.py"
global VHandPython := "D:\project\library\venv\Scripts\python.exe"
global VHandCmdFile := "D:\Python\hand_cmd.txt"
global VHandPid := 0

SetTimer(CheckTikoReminder, 60000)
SetTimer(TrackActiveWindow, 10000)
SetTimer(CheckDailyTasks, 60000)

; ===================================================
; Startup hooks for NEW features
; ===================================================
OnClipboardChange(ClipChanged)              ; feature 9: clipboard history
SetTimer(DiskSpaceCheck, 3600000)           ; feature 15: hourly disk check
AgentInit()

; --- صندوق پیام داشبورد (فاز B) ---
global DashInbox := "D:\Work\agent-state\ahk_inbox.txt"
global DashOutbox := "D:\Work\agent-state\ahk_outbox.txt"
SetTimer(DashPoll, 500)

DashSay(msg) {
    global DashOutbox
    try FileAppend(msg "`n", DashOutbox, "UTF-8")
}

DashPoll() {
    global DashInbox
    if !FileExist(DashInbox)
        return
    work := DashInbox ".work"
    try FileMove(DashInbox, work, 1)   ; اتمیک: پیام‌های جدید گم نمی‌شوند
    if !FileExist(work)
        return
    txt := FileRead(work, "UTF-8")
    FileDelete(work)
    for line in StrSplit(txt, "`n", "`r") {
        line := Trim(line)
        if (line != "") {
            DashSay("→ " line)
            Handle(line)
        }
    }
}
; ===================================================
; HOTKEYS - existing
; Ctrl+Alt+V = open clipboard link in browser
; Ctrl+Alt+Shift+V = send clipboard link to grab.bat
; ===================================================
^!v::
{
    url := Trim(A_Clipboard)
    if !IsUrl(url)
    {
        Flash("کلیپ‌بورد لینک نیست.")
        return
    }
    Run(url)
}

^!+v::
{
    global VideoDownloader
    url := Trim(A_Clipboard)
    if !IsUrl(url)
    {
        Flash("کلیپ‌بورد لینک نیست.")
        return
    }
    if !FileExist(VideoDownloader)
    {
        MsgBox("Video downloader not found:`n" VideoDownloader, "Error", 16)
        return
    }
    A_Clipboard := url
    Run(VideoDownloader)
    ToolTip("لینک در کلیپ‌بورد آماده است؛ داخل grab با Ctrl+V پیست کن")
    SetTimer(RemoveHint, -3000)
}

^!Space::
{
    OpenPalette()
}

^!+Space::
{
    global LastCommand
    if (LastCommand == "")
    {
        Flash("فرمان قبلی‌ای وجود ندارد.")
        return
    }
    Handle(LastCommand)
}

; ===================================================
; HOTKEYS - NEW features
; Ctrl+Alt+T       = translate selected text (feature 4)
; Ctrl+Alt+O       = OCR selected region (feature 5)
; Ctrl+Alt+Shift+T = speak selected text (feature 6)
; Ctrl+Alt+Shift+C = count words/chars of selection (feature 7)
; Ctrl+Alt+H       = clipboard history (feature 9)
; ===================================================
^!t::TranslateSelection()
^!o::OcrRegion()
^!+t::SpeakSelection()
^!+c::CountSelectedText()
^!h::ShowClipboardHistory()

Flash(msg, ms := 2500)
{
    ToolTip(msg)
    SetTimer(RemoveHint, -ms)
    DashSay(msg)
}

RemoveHint()
{
    ToolTip()
}

IsUrl(s)
{
    return RegExMatch(s, "i)^(https?://|www\.)") > 0
}

; Copies the current selection and returns it, restoring the clipboard.
GetSelectedText()
{
    old := A_Clipboard
    A_Clipboard := ""
    try Send("^c")
    if !ClipWait(1.5)
    {
        A_Clipboard := old
        return ""
    }
    sel := A_Clipboard
    A_Clipboard := old
    return sel
}

; ===================================================
; COMMAND ROUTER
; ===================================================
Handle(cmd)
{
    global AppMap, LearnedMap, GamesRoot, LastCommand
    c := StrLower(Normalize(cmd))

    ; Compound commands: "cmd1 ؛ cmd2" or "cmd1 سپس cmd2"
    if !InStr(c, "یادداشت") && (InStr(c, "؛") || InStr(c, "سپس"))
    {
        seq := StrReplace(c, "سپس", "؛")
        for p in StrSplit(seq, "؛")
        {
            p := Trim(p)
            if (p != "")
                Handle(p)
        }
        LastCommand := cmd  ; [BUG FIX] store the full compound command for repeat
        return
    }

    ; [BUG FIX] Save last command (was never set before)
    LastCommand := cmd
if AgentHandle(c, cmd)
return

    ; Word document creation
    if InStr(c, "ورد بساز") || InStr(c, "فایل ورد") || InStr(c, "word document")
    {
        nameMatch := RegExMatch(cmd, "i)اسم\s+(.+?)(?:\s+و\s+|$)", &namePart)
        contentMatch := RegExMatch(cmd, "i)محتوای\s+(.+)$", &contentPart)
        if (nameMatch && contentMatch)
            CreateWordDocument(Trim(namePart[1]), Trim(contentPart[1]))
        else
            MsgBox("فرمت صحیح:`nورد بساز با اسم [نام] و محتوای [متن]", "راهنما", 64)
        return
    }

    if LearnedMap.Has(c)
    {
        Run(LearnedMap[c])
        return
    }

    ; Layout fix: "تغییر زبان ..."
    if InStr(c, "تغییر زبان") || InStr(c, "تغییرزبان")
    {
        rest := StrReplace(c, "تغییر زبان", "")
        rest := Trim(StrReplace(rest, "تغییرزبان", ""))
        if (rest == "")
            rest := Trim(A_Clipboard)
        fixed := FixLayout(rest)
        if (fixed == "")
        {
            Flash("متنی برای تبدیل نیست.")
            return
        }
        A_Clipboard := fixed
        Flash("تبدیل شد و در کلیپ‌بورد قرار گرفت.")
        return
    }

    ; [BUG FIX] Google search: only trigger on "گوگل:" (with colon)
    ; Previously "if InStr(c, "گوگل")" caught any mention of گوگل
    ; including "گوگل کروم" which should go to AppMap.
    if InStr(c, "گوگل:")
    {
        pos := InStr(c, "گوگل:")
        q := Trim(SubStr(c, pos + StrLen("گوگل:")))
        if (q == "")
            Flash("چی رو سرچ کنم؟")
        else
        {
            q := StrReplace(q, " ", "+")
            Run("https://www.google.com/search?q=" q)
        }
        return
    }

    ; ===== NEW FEATURES (features 1-16) =====
    if HandleNewFeatures(c, cmd)
        return

    ; 0) Full shutdown
    shutdownWords := ["خاموشی کامل", "خاموش کردن", "shutdown", "power off"]
    if ContainsAny(c, shutdownWords)
    {
        ShutdownComputer()
        return
    }

    ; 1) VPN
    vpnWords := ["فیلتر", "شکن", "فیلترشکن", "وی پی ان", "وی‌پی‌ان", "ویپی‌ان", "ویپی ان", "vpn", "hotspot", "هاتسپات", "هات اسپات"]
    if FuzzyContainsAny(c, vpnWords)
    {
        if ConfirmCommand(cmd, "باز کردن فیلترشکن", "Hotspot Shield VPN")
            StartVPN()
        return
    }

    ; 2) Video downloader
    downloaderWords := ["دانلود کننده", "دانلودکننده", "دانلودر", "فیلم دانلودر", "دانلود فیلم", "دانلودر فیلم", "grab", "downloader"]
    if FuzzyContainsAny(c, downloaderWords)
    {
        if ConfirmCommand(cmd, "اجرای دانلودر ویدیو", "D:\Python\grab.bat")
            RunVideoDownloader()
        return
    }

    ; 8) Timer / reminder
    if ParseTimer(cmd)
        return

    ; 9) Scenario modes
    if TryScenario(c)
        return

    ; 10) Quick note
    if HandleNote(c)
        return

    ; 11) Tiko-chi
    if HandleTiko(c)
        return

    ; 14) End active mode / stop timer
    if HandleEnd(c)
        return

    ; 12) Sectioned disk scan / cache search
    if HandleScan(c)
        return

    ; 13) News briefing
    if InStr(c, "اخبار") || InStr(c, "news")
    {
        RunNews()
        return
    }

    ; 16) Image search and open
    if RegExMatch(c, "^(?:عکس|تصویر)\s+(\d+)$", &rm)
    {
        OpenImageResult(Integer(rm[1]))
        return
    }
    if InStr(c, "عکس") || InStr(c, "تصویر")
    {
        SearchImages(c)
        return
    }

    ; 17) Website search
    if InStr(c, "جستجو در سایت") || InStr(c, "search site")
    {
        SiteAction(c)
        return
    }

    ; 18) Run Python script with arguments
    if InStr(c, "اجرا کن پایتون") || InStr(c, "run python")
    {
        RunPythonScript(c)
        return
    }
; 19) Virtual Hand (Gesture Control)
if FuzzyContainsAny(c, ["دست مجازی", "کنترل با دست", "virtual hand"])
{
    StartVirtualHand()
    return
}
if FuzzyContainsAny(c, ["توقف دست مجازی", "بستن دست مجازی", "stop virtual hand"])
{
    StopVirtualHand()
    return
}

    ; 3) Video / music search
    videoWords := ["ویدیو", "ویدئو", "کلیپ", "فیلم", "آهنگ", "موزیک", "موسیقی", "پخش", "سرچ", "جستجو", "video", "music"]
    if FuzzyContainsAny(c, videoWords)
    {
        q := RemoveWords(c, videoWords)
        if (q != "")
        {
            q := StrReplace(q, " ", "+")
            searchUrl := "https://www.youtube.com/results?search_query=" q
            if ConfirmCommand(cmd, "جستجوی ویدیو", q)
                Run(searchUrl)
        }
        else
            Run("https://www.youtube.com")
        return
    }

    ; 4) Apps and sites (fuzzy)
    for name, target in AppMap
    {
        if FuzzyMatch(c, name)
        {
            if ConfirmCommand(cmd, "باز کردن", target)
                Run(target)
            return
        }
    }

    ; 5) Games
    gameWords := ["بازی", "گیم", "اجرای", "بازکردن", "باز کن", "باز", "اجرا", "بالا بیار", "بالابیار", "بزن", "لانچ", "open", "launch", "play", "game"]
    if FuzzyContainsAny(c, gameWords)
    {
        q := RemoveWords(c, gameWords)
        if (q == "")
        {
            Run(GamesRoot)
            return
        }
        if InStr(q, "پوشه") || InStr(q, "فولدر") || InStr(q, "folder")
        {
            Run(GamesRoot)
            return
        }
        OpenNamedTarget(q)
        return
    }

    ; 7) Today's date
    dateWords := ["تاریخ", "تقویم", "امروز"]
    if FuzzyContainsAny(c, dateWords)
    {
        ShowTodayDate()
        return
    }

    ; 6) Fallback: unknown command -> learn or Google
    OpenNamedTarget(c)
}
; ===================================================
; PART 2/5 - AI PRIVACY LOCK + new-feature router + AI + local features
; ===================================================

; ===================================================
; AI PRIVACY LOCK (features 12, 13)
; Locked by default; no network until explicitly unlocked this session.
; ===================================================
global AiLocked := true
global _AiKeyResult := ""
global _AiKeyDone := false

; Gate every AI feature. Returns true only when unlocked and key present.
AiEnsureUnlocked()
{
    global AiLocked
    if !AiLocked
        return true
    res := MsgBox("قابلیت هوش مصنوعی قفل است.`n`nحالت مرورگری: ایجنت مثل خودت وارد سایت Qwen/DeepSeek می‌شود (بدون کلید API).`nقفل برای این جلسه باز شود؟", "قفل هوش مصنوعی", "YesNo Icon!")
    if (res != "Yes")
        return false
    AiLocked := false
    Flash("قفل هوش مصنوعی برای این جلسه باز شد (حالت مرورگری).")
    return true
}

; Re-lock command: clears the key from memory.
AiLock()
{
    global AiLocked, LlmApiKey
    LlmApiKey := ""
    AiLocked := true
    Flash("هوش مصنوعی دوباره قفل شد و کلید از حافظه پاک شد.")
}

; Password-masked input for the API key (not shown, not saved)
AiAskKey()
{
    global _AiKeyResult, _AiKeyDone
    _AiKeyResult := ""
    _AiKeyDone := false
    g := Gui("+AlwaysOnTop +ToolWindow", "کلید هوش مصنوعی")
    g.BackColor := "111111"
    g.SetFont("s10 cEEEEEE", "Segoe UI")
    g.Add("Text", "x12 y12 w420", "کلید API را وارد کن (فقط در حافظه می‌ماند):")
    ed := g.Add("Edit", "x12 y40 w420 +Password +Background222222 cFFFFFF", "")
    okBtn := g.Add("Button", "x12 y84 w100 h30 Default", "تأیید")
    cancelBtn := g.Add("Button", "x120 y84 w100 h30", "انصراف")
    okBtn.OnEvent("Click", (*) => AiKeySubmit(g, ed, true))
    cancelBtn.OnEvent("Click", (*) => AiKeySubmit(g, ed, false))
    g.OnEvent("Escape", (*) => AiKeySubmit(g, ed, false))
    g.OnEvent("Close", (*) => AiKeySubmit(g, ed, false))
    g.Show("Center")
    ed.Focus()
    while !_AiKeyDone
        Sleep(50)
    return _AiKeyResult
}
AiKeySubmit(g, ed, ok)
{
    global _AiKeyResult, _AiKeyDone
    _AiKeyResult := ok ? Trim(ed.Value) : ""
    _AiKeyDone := true
    try g.Destroy()
}

; JSON helpers for the OpenAI-compatible chat endpoint
JsonEsc(s)
{
    s := StrReplace(s, "\", "\\")
    s := StrReplace(s, Chr(34), "\" Chr(34))
    s := StrReplace(s, "`n", "\n")
    s := StrReplace(s, "`r", "\r")
    s := StrReplace(s, "`t", "\t")
    return s
}
JsonUnesc(s)
{
    s := StrReplace(s, "\n", "`n")
    s := StrReplace(s, "\r", "`r")
    s := StrReplace(s, "\t", "`t")
    s := StrReplace(s, "\" Chr(34), Chr(34))
    s := StrReplace(s, "\\", "\")
    return s
}

; Sends one chat message. Returns the reply text, or "" on failure.
AiRequest(prompt, systemMsg := "")
{
    global LlmApiUrl, LlmApiKey, LlmModel
if (LlmApiKey = "")
{
Flash("حالت مرورگری فعال است؛ برای هوش مصنوعی از «ایجنت ...» یا «کد بنویس ...» استفاده کن.")
return ""
}
    q := Chr(34)
    body := "{" q "model" q ":" q LlmModel q "," q "messages" q ":["
    if (systemMsg != "")
        body .= "{" q "role" q ":" q "system" q "," q "content" q ":" q JsonEsc(systemMsg) q "},"
    body .= "{" q "role" q ":" q "user" q "," q "content" q ":" q JsonEsc(prompt) q "}]," q "temperature" q ":0.4}"
    try
    {
        req := ComObject("WinHttp.WinHttpRequest.5.1")
        req.Open("POST", LlmApiUrl, false)
        req.SetRequestHeader("Content-Type", "application/json")
        req.SetRequestHeader("Authorization", "Bearer " LlmApiKey)
        req.Send(body)
        resp := req.ResponseText
        if RegExMatch(resp, q "content" q "\s*:\s*" q "((?:[^" q "\\]|\\.)*)", &m)
            return JsonUnesc(m[1])
        return ""
    }
    catch
        return ""
}

; Feature 12: AI summarize (LOCKED by default)
AiSummarize()
{
    if !AiEnsureUnlocked()
        return
    txt := GetSelectedText()
    if (txt == "")
        txt := Trim(A_Clipboard)
    if (txt == "")
    {
        Flash("متنی برای خلاصه‌سازی نیست؛ متنی را انتخاب یا کپی کن.")
        return
    }
    Flash("در حال خلاصه‌سازی…")
    out := AiRequest(txt, "این متن را به فارسی روان و کوتاه خلاصه کن. فقط خلاصه را برگردان.")
    if (out == "")
        Flash("خلاصه‌سازی ناموفق بود (اینترنت یا کلید را چک کن).")
    else
        MsgBox(out, "خلاصه", 64)
}

; Feature 13: AI chat (LOCKED by default)
AiChat()
{
    if !AiEnsureUnlocked()
        return
    ib := InputBox("سوال خود را از هوش مصنوعی بپرس:", "چت با هوش مصنوعی", "w480 h140")
    if (ib.Result != "OK" || Trim(ib.Value) == "")
        return
    q := Trim(ib.Value)
    Flash("در حال فکر کردن…")
    out := AiRequest(q)
    if (out == "")
        Flash("پاسخ دریافت نشد (اینترنت یا کلید را چک کن).")
    else
        MsgBox(out, "پاسخ هوش مصنوعی", 64)
}

; ===================================================
; Router for all NEW features (called from Handle)
; ===================================================
HandleNewFeatures(c, cmd)
{
    if (FuzzyContainsAny(c, ["قیمت طلا", "قیمت دلار", "قیمت ارز", "قیمت بیت کوین", "قیمت بیتکوین", "قیمت رمزارز"]))
    {
        ShowPrices(c)
        return true
    }
    if (FuzzyContainsAny(c, ["اوقات شرعی", "وقت اذان", "وقت نماز"]))
    {
        ShowPrayerTimes()
        return true
    }
    if (FuzzyContainsAny(c, ["مناسبت", "مناسبت ها", "مناسبت های امروز", "رویداد امروز"]))
    {
        ShowTodayEvents()
        return true
    }
    if InStr(c, "ترجمه")
    {
        TranslateCommand(cmd)
        return true
    }
    if (FuzzyContainsAny(c, ["متن از عکس", "عکس به متن", "استخراج متن", "اسکن متن"]))
    {
        OcrRegion()
        return true
    }
    if (FuzzyContainsAny(c, ["بخوان", "بلند بخوان", "تبدیل به صدا", "پخش صدا"]))
    {
        SpeakSelection()
        return true
    }
    if (FuzzyContainsAny(c, ["شمارش کلمه", "تعداد کلمات", "شمارش متن", "تعداد کاراکتر"]))
    {
        CountSelectedText()
        return true
    }
    if InStr(c, "پومودورو")
    {
        PomodoroCommand(c)
        return true
    }
    if (FuzzyContainsAny(c, ["تاریخچه کلیپ بورد", "کلیپ بورد", "حافظه کلیپ بورد"]))
    {
        ShowClipboardHistory()
        return true
    }
    if InStr(c, "ذخیره چیدمان")
    {
        SaveWindowLayout()
        return true
    }
    if InStr(c, "بازیابی چیدمان") || InStr(c, "بازگردانی چیدمان")
    {
        RestoreWindowLayout()
        return true
    }
    if (FuzzyContainsAny(c, ["مرتب کن", "مرتب سازی دانلودها", "مرتب کردن دانلودها"]))
    {
        OrganizeDownloads()
        return true
    }
    if (FuzzyContainsAny(c, ["خلاصه کن", "خلاصه سازی", "خلاصه متن"]))
    {
        AiSummarize()
        return true
    }
    if (FuzzyContainsAny(c, ["قفل هوش مصنوعی", "قفل کن هوش مصنوعی"]))
    {
        AiLock()
        return true
    }
    if (FuzzyContainsAny(c, ["چت با هوش مصنوعی", "سوال از هوش مصنوعی", "دستیار هوشمند", "هوش مصنوعی"]))
    {
        AiChat()
        return true
    }
    if (FuzzyContainsAny(c, ["مانیتور", "وضعیت سیستم", "مصرف رم", "مصرف سی پی یو"]))
    {
        ShowSystemMonitor()
        return true
    }
    if (FuzzyContainsAny(c, ["فضای دیسک", "حجم درایو", "فضای خالی"]))
    {
        ShowDiskSpace()
        return true
    }
    if (FuzzyContainsAny(c, ["رمز عبور", "تولید رمز", "پسورد"]))
    {
        GeneratePassword()
        return true
    }
    if (FuzzyContainsAny(c, ["کیوآر", "کیو آر", "qr"]))
    {
        MakeQr(cmd)
        return true
    }
    return false
}

; ===================================================
; Feature 6 - Text to speech
; ===================================================
global SpeakVoice := ""
SpeakSelection()
{
    txt := GetSelectedText()
    if (txt == "")
        txt := Trim(A_Clipboard)
    if (txt == "")
    {
        Flash("متنی برای خواندن نیست.")
        return
    }
    SpeakText(txt)
}
SpeakText(txt)
{
    global SpeakVoice
    try
    {
        if !IsObject(SpeakVoice)
            SpeakVoice := ComObject("SAPI.SpVoice")
        SpeakVoice.Speak(txt, 1) ; async
    }
    catch
        Flash("خواندن متن در دسترس نیست.")
}

; ===================================================
; Feature 7 - Word / character count
; ===================================================
CountSelectedText()
{
    txt := GetSelectedText()
    if (txt == "")
        txt := Trim(A_Clipboard)
    if (txt == "")
    {
        Flash("متنی انتخاب یا کپی نشده است.")
        return
    }
    words := 0
    for w in StrSplit(txt, [" ", "`t", "`n", "`r"])
        if (Trim(w) != "")
            words++
    chars := StrLen(txt)
    lines := StrSplit(txt, "`n").Length
    MsgBox("کلمه: " words "`nکاراکتر: " chars "`nخط: " lines, "آمار متن", 64)
}

; ===================================================
; Feature 11 - Organize Downloads folder
; ===================================================
OrganizeDownloads()
{
    global DownloadsFolder
    if !DirExist(DownloadsFolder)
    {
        Flash("پوشه دانلودها پیدا نشد.")
        return
    }
    cats := Map(
        "عکس", ["jpg","jpeg","png","gif","bmp","webp","svg"],
        "ویدیو", ["mp4","mkv","avi","mov","wmv","flv","webm"],
        "موسیقی", ["mp3","wav","flac","m4a","ogg","aac"],
        "اسناد", ["pdf","doc","docx","txt","rtf","odt","xls","xlsx","ppt","pptx","csv"],
        "فشرده", ["zip","rar","7z","tar","gz"],
        "برنامه", ["exe","msi","apk","bat"])
    moved := 0
    Loop Files DownloadsFolder "\*", "F"
    {
        ext := StrLower(A_LoopFileExt)
        target := ""
        for cat, exts in cats
        {
            for e in exts
                if (e == ext)
                {
                    target := cat
                    break
                }
            if (target != "")
                break
        }
        if (target == "")
            continue
        destDir := DownloadsFolder "\" target
        if !DirExist(destDir)
            DirCreate(destDir)
        dest := destDir "\" A_LoopFileName
        if FileExist(dest)
        {
            dot := InStr(A_LoopFileName, ".", , 0)
            stem := (dot > 1) ? SubStr(A_LoopFileName, 1, dot - 1) : A_LoopFileName
            dest := destDir "\" stem "_" A_Now "." ext
        }
        try
        {
            FileMove(A_LoopFileFullPath, dest)
            moved++
        }
    }
    Flash("مرتب‌سازی انجام شد: " moved " فایل جابه‌جا شد.")
}

; ===================================================
; Feature 15 - Disk space + low-space warning
; ===================================================
FmtSize(bytes)
{
    units := ["بایت", "کیلوبایت", "مگابایت", "گیگابایت", "ترابایت"]
    i := 1
    v := bytes
    while (v >= 1024 && i < units.Length)
    {
        v /= 1024
        i++
    }
    return Round(v, 1) " " units[i]
}
ShowDiskSpace()
{
    report := ""
    for d in ["C:", "D:", "E:", "F:", "G:"]
    {
        try
        {
            free := DriveGetSpaceFree(d)
            cap := DriveGetCapacity(d)
            if (cap > 0)
            {
                pct := Round(free / cap * 100)
                report .= d " آزاد: " FmtSize(free) " از " FmtSize(cap) " (" pct "%)`n"
            }
        }
    }
    if (report == "")
        Flash("درایوی پیدا نشد.")
    else
        MsgBox(report, "فضای دیسک", 64)
}
DiskSpaceCheck()
{
    global DiskWarnGB, LastDiskWarnDay
    try
    {
        free := DriveGetSpaceFree("C:")
        if (free < DiskWarnGB * 1024 * 1024 * 1024)
        {
            today := SubStr(A_Now, 1, 8)
            if (LastDiskWarnDay != today)
            {
                LastDiskWarnDay := today
                MsgBox("فضای درایو C رو به اتمام است!`nفضای آزاد: " FmtSize(free), "هشدار دیسک", 48)
            }
        }
    }
}
; ===================================================
; PART 3/5 - Pomodoro, Clipboard manager, Window layout,
;            System monitor, OCR, Password
; ===================================================

; ===================================================
; Feature 8 - Pomodoro (work/break cycles)
; ===================================================
PomodoroCommand(c)
{
    global PomState, PomEndTick, PomCycles
    if InStr(c, "توقف") || InStr(c, "لغو") || InStr(c, "stop")
    {
        PomodoroStop()
        return
    }
    if InStr(c, "وضعیت") || InStr(c, "status")
    {
        if (PomState == "")
            Flash("پومودورو فعال نیست.")
        else
        {
            remainSec := Max(0, Round((PomEndTick - A_TickCount) / 1000))
            Flash("پومودورو: فاز " PomState " | حدود " Round(remainSec / 60) " دقیقه مانده", 4000)
        }
        return
    }
    if (PomState != "")
    {
        Flash("پومودورو همین حالا فعال است. برای توقف بگو: توقف پومودورو")
        return
    }
    PomCycles := 0
    PomodoroStartPhase("work")
}
PomodoroStartPhase(phase)
{
    global PomState, PomEndTick, PomWorkMin, PomBreakMin
    PomState := phase
    mins := (phase == "work") ? PomWorkMin : PomBreakMin
    PomEndTick := A_TickCount + mins * 60000
    SetTimer(PomodoroTick, 1000)
    Flash("پومودورو: " ((phase == "work") ? PomWorkMin " دقیقه کار شروع شد" : PomBreakMin " دقیقه استراحت"), 3500)
}
PomodoroTick()
{
    global PomState, PomEndTick, PomCycles, PomWorkMin, PomBreakMin
    if (PomState == "")
    {
        SetTimer(PomodoroTick, 0)
        return
    }
    if (A_TickCount >= PomEndTick)
    {
        SoundBeep(880, 250)
        SoundBeep(880, 250)
        SoundBeep(880, 400)
        if (PomState == "work")
        {
            PomCycles++
            Flash("🍅 زمان کار تمام شد! " PomBreakMin " دقیقه استراحت کن.", 8000)
            PomState := "break"
            PomEndTick := A_TickCount + PomBreakMin * 60000
        }
        else
        {
            Flash("☕ استراحت تمام شد. برگرد سر کار!", 8000)
            PomState := "work"
            PomEndTick := A_TickCount + PomWorkMin * 60000
        }
    }
}
PomodoroStop()
{
    global PomState, PomEndTick, PomCycles
    PomState := ""
    PomEndTick := 0
    SetTimer(PomodoroTick, 0)
    Flash("پومودورو متوقف شد. چرخه‌های کامل: " PomCycles, 3500)
}

; ===================================================
; Feature 9 - Clipboard manager with history
; ===================================================
ClipChanged(type)
{
    global ClipHistory, ClipHistMax
    ; type: 0=empty, 1=text, 2=non-text
    if (type != 1)
        return
    txt := A_Clipboard
    if (Trim(txt) == "")
        return
    ; remove any existing copy so the item moves to top without duplicates
    Loop ClipHistory.Length
    {
        if (ClipHistory[A_Index] == txt)
        {
            ClipHistory.RemoveAt(A_Index)
            break
        }
    }
    ClipHistory.InsertAt(1, txt)
    if (ClipHistory.Length > ClipHistMax)
        ClipHistory.Pop()
}
ShowClipboardHistory()
{
    global ClipHistory, ClipHistGui
    if (ClipHistory.Length == 0)
    {
        Flash("تاریخچه کلیپ‌بورد خالی است.")
        return
    }
    if (ClipHistGui != "")
    {
        try ClipHistGui.Destroy()
    }
    ClipHistGui := Gui("+AlwaysOnTop", "تاریخچه کلیپ‌بورد")
    ClipHistGui.BackColor := "111111"
    ClipHistGui.SetFont("s10", "Segoe UI")
    lv := ClipHistGui.Add("ListView", "x12 y12 w480 h360 -Multi +Background222222 cFFFFFF", ["#", "متن"])
    lv.ModifyCol(1, 40)
    lv.ModifyCol(2, 420)
    for i, item in ClipHistory
    {
        short := StrReplace(item, "`n", " ")
        if (StrLen(short) > 90)
            short := SubStr(short, 1, 90) "…"
        lv.Add("", i, short)
    }
    lv.OnEvent("DoubleClick", ClipHistPick)
    copyBtn := ClipHistGui.Add("Button", "x12 y382 w130 h30", "کپی انتخاب‌شده")
    copyBtn.OnEvent("Click", (*) => ClipHistPick(lv))
    ClipHistGui.OnEvent("Close", (*) => ClipHistGui.Hide())
    ClipHistGui.OnEvent("Escape", (*) => ClipHistGui.Hide())
    ClipHistGui.Show("Center")
    lv.Focus()
}
ClipHistPick(ctrl, row := 0)
{
    global ClipHistory
    if (row == 0)
        row := ctrl.GetNext(0)
    if (row < 1 || row > ClipHistory.Length)
    {
        Flash("موردی انتخاب نشده است.")
        return
    }
    A_Clipboard := ClipHistory[row]
    Flash("در کلیپ‌بورد قرار گرفت.")
}

; ===================================================
; Feature 10 - Save / restore window layout
; ===================================================
SaveWindowLayout()
{
    global WindowLayoutFile
    lines := ""
    count := 0
    for hwnd in WinGetList()
    {
        try
        {
            ; skip minimized windows and the desktop shell
            if WinGetMinMax("ahk_id " hwnd) == -1
                continue
            proc := WinGetProcessName("ahk_id " hwnd)
            if (proc == "" || proc == "explorer.exe")
                continue
            WinGetPos(&x, &y, &w, &h, "ahk_id " hwnd)
            if (w < 50 || h < 50)
                continue
            lines .= proc "|" x "|" y "|" w "|" h "`n"
            count++
        }
    }
    if (count == 0)
    {
        Flash("پنجره‌ای برای ذخیره پیدا نشد.")
        return
    }
    if FileExist(WindowLayoutFile)
        FileDelete(WindowLayoutFile)
    FileAppend(lines, WindowLayoutFile, "UTF-8")
    Flash("چیدمان " count " پنجره ذخیره شد.")
}
RestoreWindowLayout()
{
    global WindowLayoutFile
    if !FileExist(WindowLayoutFile)
    {
        Flash("چیدمان ذخیره‌شده‌ای وجود ندارد. اول بگو: ذخیره چیدمان")
        return
    }
    restored := 0
    usedHwnds := Map()
    for line in StrSplit(FileRead(WindowLayoutFile, "UTF-8"), "`n", "`r")
    {
        line := Trim(line)
        if (line == "")
            continue
        p := StrSplit(line, "|")
        if (p.Length < 5)
            continue
        proc := p[1]
        x := Integer(p[2]), y := Integer(p[3]), w := Integer(p[4]), h := Integer(p[5])
        for hwnd in WinGetList("ahk_exe " proc)
        {
            if usedHwnds.Has(hwnd)
                continue
            try
            {
                WinMove(x, y, w, h, "ahk_id " hwnd)
                usedHwnds[hwnd] := true
                restored++
                break
            }
        }
    }
    Flash("چیدمان بازیابی شد: " restored " پنجره.")
}

; ===================================================
; Feature 14 - System monitor (CPU / RAM / disk / battery)
; ===================================================
ShowSystemMonitor()
{
    info := "📊 وضعیت سیستم`n`n"
    cpu := GetCpuUsage()
    info .= "🔲 پردازنده (CPU): " (cpu == "" ? "نامشخص" : cpu "٪") "`n"
    mem := GetMemoryInfo()
    if (mem != "")
    {
        usedGB := Round((mem["total"] - mem["avail"]) / 1073741824, 1)
        totalGB := Round(mem["total"] / 1073741824, 1)
        info .= "🧠 حافظه (RAM): " usedGB " از " totalGB " گیگ (" mem["load"] "٪)`n"
    }
    try
    {
        free := DriveGetSpaceFree("C:\")
        cap := DriveGetCapacity("C:\")
        info .= "💽 درایو C: " FmtSize(free) " آزاد از " FmtSize(cap) "`n"
    }
    batt := GetBatteryInfo()
    if (batt != "")
        info .= "🔋 باتری: " batt
    MsgBox(info, "مانیتور سیستم", 64)
}
GetCpuUsage()
{
    try
    {
        wmi := ComObjGet("winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2")
        total := 0
        n := 0
        for cpu in wmi.ExecQuery("SELECT LoadPercentage FROM Win32_Processor")
        {
            total += cpu.LoadPercentage
            n++
        }
        if (n > 0)
            return Round(total / n)
    }
    return ""
}
GetMemoryInfo()
{
    memStatus := Buffer(64, 0)
    NumPut("UInt", 64, memStatus, 0)
    if DllCall("GlobalMemoryStatusEx", "Ptr", memStatus)
    {
        memLoad := NumGet(memStatus, 4, "UInt")
        totalPhys := NumGet(memStatus, 8, "UInt64")
        availPhys := NumGet(memStatus, 16, "UInt64")
        return Map("load", memLoad, "total", totalPhys, "avail", availPhys)
    }
    return ""
}
GetBatteryInfo()
{
    sps := Buffer(12, 0)
    if DllCall("GetSystemPowerStatus", "Ptr", sps)
    {
        acStatus := NumGet(sps, 0, "UChar")
        batteryFlag := NumGet(sps, 1, "UChar")
        pct := NumGet(sps, 2, "UChar")
        if (batteryFlag == 128) ; no battery (desktop)
            return ""
        return pct "٪" ((acStatus == 1) ? " (در حال شارژ)" : "")
    }
    return ""
}

; ===================================================
; Feature 5 - OCR via built-in Windows 11 OCR engine
; ===================================================
OcrRegion()
{
    ps1 := A_Temp "\assistant_ocr.ps1"
    resultFile := A_Temp "\assistant_ocr_result.txt"
    s := ""
    s .= 'param(' "`n"
    s .= '[int]$X = 0,' "`n"
    s .= '[int]$Y = 0,' "`n"
    s .= '[int]$W = 0,' "`n"
    s .= '[int]$H = 0,' "`n"
    s .= '[string]$OutFile = "$env:TEMP\assistant_ocr_result.txt"' "`n"
    s .= ')' "`n"
    s .= 'Add-Type -AssemblyName System.Runtime.WindowsRuntime' "`n"
    s .= 'Add-Type -AssemblyName System.Windows.Forms' "`n"
    s .= 'Add-Type -AssemblyName System.Drawing' "`n"
    s .= '$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and $_.GetGenericArguments().Count -eq 1 })[0]' "`n"
    s .= 'Function Await($WinRTTask, $ResultType) { $asTask = $asTaskGeneric.MakeGenericMethod($ResultType); $netTask = $asTask.Invoke($null, @($WinRTTask)); $netTask.Wait(-1) | Out-Null; $netTask.Result }' "`n"
    s .= 'if ($W -le 0 -or $H -le 0) { $b = [System.Windows.Forms.SystemInformation]::VirtualScreen; $X = $b.X; $Y = $b.Y; $W = $b.Width; $H = $b.Height }' "`n"
    s .= '$bmp = New-Object System.Drawing.Bitmap $W, $H' "`n"
    s .= '$graphics = [System.Drawing.Graphics]::FromImage($bmp)' "`n"
    s .= '$graphics.CopyFromScreen($X, $Y, 0, 0, (New-Object System.Drawing.Size $W, $H))' "`n"
    s .= '$graphics.Dispose()' "`n"
    s .= '$imgPath = "$env:TEMP\assistant_ocr_screen.png"' "`n"
    s .= '$bmp.Save($imgPath, [System.Drawing.Imaging.ImageFormat]::Png)' "`n"
    s .= '$bmp.Dispose()' "`n"
    s .= '[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation.UniversalApiContract, ContentType=WindowsRuntime]' "`n"
    s .= '[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation.UniversalApiContract, ContentType=WindowsRuntime]' "`n"
    s .= '[void][Windows.Storage.StorageFile, Windows.Foundation.UniversalApiContract, ContentType=WindowsRuntime]' "`n"
    s .= '$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imgPath)) ([Windows.Storage.StorageFile])' "`n"
    s .= '$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])' "`n"
    s .= '$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])' "`n"
    s .= '$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])' "`n"
    s .= '$ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()' "`n"
    s .= '$result = Await ($ocr.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])' "`n"
    s .= '$result.Text | Out-File -FilePath $OutFile -Encoding UTF8' "`n"
    s .= 'Remove-Item $imgPath -ErrorAction SilentlyContinue' "`n"
    if FileExist(ps1)
        FileDelete(ps1)
    FileAppend(s, ps1, "UTF-8")
    if FileExist(resultFile)
        FileDelete(resultFile)
    wx := 0, wy := 0, ww := 0, wh := 0
    try WinGetPos(&wx, &wy, &ww, &wh, "A")
    Flash("در حال استخراج متن از پنجره فعال…", 4000)
    try
    {
        RunWait('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' ps1 '" -X ' wx ' -Y ' wy ' -W ' ww ' -H ' wh, , "Hide")
    }
    catch as e
    {
        Flash("اجرای OCR ناموفق بود: " e.Message, 4000)
        return
    }
    if FileExist(resultFile)
    {
        txt := Trim(FileRead(resultFile, "UTF-8"))
        if (txt == "")
            Flash("متنی در پنجره پیدا نشد.", 4000)
        else
        {
            A_Clipboard := txt
            MsgBox(txt, "متن استخراج‌شده (در کلیپ‌بورد کپی شد)", 64)
        }
    }
    else
        Flash("OCR نتیجه‌ای نداد. مطمئن شو بسته زبان مربوطه در ویندوز نصب است.", 5000)
}

; ===================================================
; Feature 16a - Strong password generator
; ===================================================
GeneratePassword()
{
    ib := InputBox("طول رمز عبور (4 تا 64):", "تولید رمز عبور", "w320 h110", "16")
    if (ib.Result != "OK")
        return
    ; [BUG FIX] Integer() can throw on non-numeric input
    len := 16
    try len := Integer(Trim(ib.Value))
    if (len < 4)
        len := 4
    if (len > 64)
        len := 64
    chars := "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_=+"
    pwd := ""
    Loop len
    {
        idx := Random(1, StrLen(chars))
        pwd .= SubStr(chars, idx, 1)
    }
    A_Clipboard := pwd
    MsgBox("رمز عبور تولید شد و در کلیپ‌بورد کپی شد:`n`n" pwd, "رمز عبور", 64)
}
; ===================================================
; PART 4/5 - Web features: Prices, Prayer times, Calendar events,
;            Translation (AI-locked), QR code
;            + Core helpers (utility, normalize, fuzzy, shutdown, VPN,
;              calendar, games, learned commands, timer, scenarios, notes, tiko)
; ===================================================

; QR helper path (the other script paths are already in CONFIG)
global QrScript := "D:\Python\qr_gen.py"

; ---------- Feature 4: Translation (respects the AI privacy lock) ----------
TranslateSelection()
{
    txt := GetSelectedText()
    if (txt == "")
        txt := Trim(A_Clipboard)
    if (txt == "")
    {
        Flash("متنی برای ترجمه نیست؛ متنی را انتخاب یا کپی کن.")
        return
    }
    TranslateText(txt)
}
TranslateCommand(cmd)
{
    rest := Trim(StrReplace(cmd, "ترجمه", ""))
    rest := StrReplace(rest, "به انگلیسی", "")
    rest := StrReplace(rest, "به فارسی", "")
    rest := Trim(rest)
    if InStr(rest, "کن ") == 1
        rest := Trim(SubStr(rest, 4))
    if (rest == "")
    {
        TranslateSelection()
        return
    }
    TranslateText(rest)
}
TranslateText(txt)
{
    ; goes through the AI engine, so the privacy lock applies
    if !AiEnsureUnlocked()
        return
    faCount := 0
    Loop StrLen(txt)
    {
        o := Ord(SubStr(txt, A_Index, 1))
        if (o >= 1536 && o <= 1791)
            faCount++
    }
    isFa := (faCount > StrLen(txt) / 4)
    target := isFa ? "انگلیسی" : "فارسی"
    Flash("در حال ترجمه به " target "…")
    out := AiRequest(txt, "این متن را به زبان " target " روان و دقیق ترجمه کن. فقط ترجمه را برگردان.")
    if (out == "")
        Flash("ترجمه ناموفق بود (اینترنت یا کلید را چک کن).")
    else
    {
        A_Clipboard := out
        MsgBox(out, "ترجمه (در کلیپ‌بورد کپی شد)", 64)
    }
}

; ---------- Feature 2: Prayer times ----------
ShowPrayerTimes()
{
    global PrayerCity, PrayerScript
    if !FileExist(PrayerScript)
    {
        Flash("فایل prayer_times.py در D:\Python پیدا نشد.")
        return
    }
    resultFile := A_ScriptDir "\prayer_result.txt"
    if FileExist(resultFile)
        FileDelete(resultFile)
    Flash("در حال دریافت اوقات شرعی…")
    try
        RunWait('python "' PrayerScript '" "' PrayerCity '" "' resultFile '"', "D:\Python", "Hide")
    catch as e
    {
        Flash("اجرای اوقات شرعی ناموفق بود: " e.Message)
        return
    }
    if FileExist(resultFile)
    {
        txt := Trim(FileRead(resultFile, "UTF-8"))
        if (txt == "")
            Flash("اوقات شرعی دریافت نشد؛ اینترنت/فیلترشکن را چک کن.")
        else
            MsgBox(txt, "اوقات شرعی " PrayerCity, 64)
    }
    else
        Flash("اوقات شرعی دریافت نشد.")
}

; ---------- Feature 1: Live prices ----------
ShowPrices(c)
{
    global PricesScript
    if !FileExist(PricesScript)
    {
        Flash("فایل prices.py در D:\Python پیدا نشد.")
        return
    }
    what := "crypto"
    if InStr(c, "طلا") || InStr(c, "سکه")
        what := "gold"
    else if InStr(c, "دلار") || InStr(c, "ارز") || InStr(c, "تومان")
        what := "dollar"
    resultFile := A_ScriptDir "\prices_result.txt"
    if FileExist(resultFile)
        FileDelete(resultFile)
    Flash("در حال دریافت قیمت‌ها…")
    try
        RunWait('python "' PricesScript '" ' what ' "' resultFile '"', "D:\Python", "Hide")
    catch as e
    {
        Flash("اجرای اسکریپت قیمت ناموفق بود: " e.Message)
        return
    }
    if FileExist(resultFile)
    {
        txt := Trim(FileRead(resultFile, "UTF-8"))
        if (txt == "")
        {
            if (what == "crypto")
                Flash("دریافت قیمت رمزارز ناموفق بود؛ اینترنت/فیلترشکن را چک کن.")
            else
                Flash("دریافت قیمت طلا/دلار ناموفق بود؛ منبع ممکن است نیاز به به‌روزرسانی داشته باشد.")
        }
        else
            MsgBox(txt, "قیمت لحظه‌ای", 64)
    }
    else
        Flash("قیمتی دریافت نشد.")
}

; ---------- Feature 3: Today's calendar events ----------
ShowTodayEvents()
{
    now := A_Now
    gy := Integer(SubStr(now, 1, 4))
    gm := Integer(SubStr(now, 5, 2))
    gd := Integer(SubStr(now, 7, 2))
    j := GregorianToJalali(gy, gm, gd)
    key := Format("{1:02d}", j[2]) "/" Format("{1:02d}", j[3])
    found := []
    for e in LoadEvents()
        if (e.date == key)
            found.Push(e.name)
    jalaliStr := Format("{1:04d}/{2:02d}/{3:02d}", j[1], j[2], j[3])
    if (found.Length == 0)
        MsgBox("📅 " jalaliStr "`n`nمناسبت خاصی در تقویم محلی ثبت نشده است.`nبرای افزودن، فایل events.txt را ویرایش کن.", "مناسبت‌های امروز", 64)
    else
        MsgBox("📅 " jalaliStr "`n`n" JoinList(found), "مناسبت‌های امروز", 64)
}
LoadEvents()
{
    out := [
        {date: "01/01", name: "نوروز - آغاز سال نو"},
        {date: "01/13", name: "سیزده‌بدر - روز طبیعت"},
        {date: "09/30", name: "شب یلدا"},
        {date: "11/22", name: "پیروزی انقلاب اسلامی"},
        {date: "12/29", name: "ملی شدن صنعت نفت"}
    ]
    eventsFile := A_ScriptDir "\events.txt"
    if FileExist(eventsFile)
    {
        for line in StrSplit(FileRead(eventsFile, "UTF-8"), "`n", "`r")
        {
            line := Trim(line)
            if (line == "" || InStr(line, ";") == 1)
                continue
            pos := InStr(line, "|")
            if (pos < 2)
                continue
            out.Push({date: Trim(SubStr(line, 1, pos - 1)), name: Trim(SubStr(line, pos + 1))})
        }
    }
    return out
}

; ---------- Feature 16b: QR code ----------
MakeQr(cmd)
{
    global QrScript
    if !FileExist(QrScript)
    {
        Flash("فایل qr_gen.py در D:\Python پیدا نشد.")
        return
    }
    rest := Trim(RegExReplace(cmd, "i)^(?:کیوآر|کیو آر|qr)\s*", ""))
    if (rest == "")
    {
        ib := InputBox("متن یا لینک برای ساخت کیوآر کد:", "ساخت کیوآر", "w480 h140")
        if (ib.Result != "OK" || Trim(ib.Value) == "")
            return
        rest := Trim(ib.Value)
    }
    inFile := A_ScriptDir "\qr_input.txt"
    outFile := A_ScriptDir "\qr_last.png"
    if FileExist(inFile)
        FileDelete(inFile)
    FileAppend(rest, inFile, "UTF-8")
    if FileExist(outFile)
        FileDelete(outFile)
    Flash("در حال ساخت کیوآر…")
    try
        RunWait('python "' QrScript '" "' inFile '" "' outFile '"', "D:\Python", "Hide")
    catch as e
    {
        Flash("ساخت کیوآر ناموفق بود: " e.Message)
        return
    }
    if FileExist(outFile)
        Run(outFile)
    else
        Flash("ساخت کیوآر ناموفق بود؛ اینترنت را چک کن.")
}

; ===================================================
; Core helpers
; ===================================================

; ---------- [BUG FIX] Missing JoinList function ----------
; Used by ShowTodayEvents and FireTikoReminder
JoinList(items)
{
    out := ""
    for i, item in items
    {
        if (i > 1)
            out .= "، "
        out .= item
    }
    return out
}

; ---------- Utility ----------
ContainsAny(haystack, needles)
{
    for needle in needles
        if InStr(haystack, needle)
            return true
    return false
}
ShortPath(p, max := 70)
{
    return (StrLen(p) > max) ? "…" SubStr(p, -max + 2) : p
}

; ---------- Text normalization ----------
Normalize(s)
{
    s := Trim(s)
    s := StrReplace(s, "ي", "ی")
    s := StrReplace(s, "ك", "ک")
    s := StrReplace(s, "أ", "ا")
    s := StrReplace(s, "إ", "ا")
    s := StrReplace(s, "آ", "ا")
    s := StrReplace(s, "ة", "ه")
    s := StrReplace(s, "ؤ", "و")
    s := StrReplace(s, "ئ", "ی")
    s := StrReplace(s, Chr(8204), "")
    s := StrReplace(s, Chr(8203), "")
    s := StrReplace(s, Chr(160), " ")
    Loop
    {
        old := s
        s := StrReplace(s, "  ", " ")
        if (old == s)
            break
    }
    return s
}
RemoveWords(s, words)
{
    for w in words
        s := StrReplace(s, w, "")
    Loop
    {
        old := s
        s := StrReplace(s, "  ", " ")
        if (old == s)
            break
    }
    return Trim(s)
}

; ---------- Fuzzy matching ----------
FuzzyContainsAny(text, patterns, threshold := 0.8)
{
    for pattern in patterns
        if FuzzyMatch(text, pattern, threshold)
            return true
    return false
}
; [BUG FIX] Rewritten: word-level match now requires ALL pattern words
; to be present (was ANY, which caused false positives like
; "چت با هوش مصنوعی" matching "قفل هوش مصنوعی" via shared word "هوش").
FuzzyMatch(text, pattern, threshold := 0.8)
{
    text := StrLower(Normalize(text))
    pattern := StrLower(Normalize(pattern))
    if (text == pattern)
        return true
    if (pattern == "")
        return false
    if InStr(text, pattern)
        return true
    textCompact := StrReplace(text, " ", "")
    patternCompact := StrReplace(pattern, " ", "")
    if (patternCompact != "" && InStr(textCompact, patternCompact))
        return true
    ; All pattern words must be found in text (as substring or high similarity)
    pWords := StrSplit(pattern, " ")
    tWords := StrSplit(text, " ")
    allMatch := true
    for pWord in pWords
    {
        if (pWord == "")
            continue
        found := false
        for tWord in tWords
        {
            if (tWord == pWord) || InStr(tWord, pWord) || InStr(pWord, tWord)
            {
                found := true
                break
            }
            if (StrLen(pWord) >= 3 && StrLen(tWord) >= 3 && CalculateSimilarity(tWord, pWord) >= threshold)
            {
                found := true
                break
            }
        }
        if (!found)
        {
            allMatch := false
            break
        }
    }
    if (allMatch && pWords.Length > 0)
        return true
    ; Full compact similarity as last resort (stricter threshold)
    if (patternCompact != "" && textCompact != "" && CalculateSimilarity(textCompact, patternCompact) >= 0.85)
        return true
    return false
}
CalculateSimilarity(s1, s2)
{
    if (s1 == s2)
        return 1.0
    len1 := StrLen(s1)
    len2 := StrLen(s2)
    if (len1 == 0 || len2 == 0)
        return 0.0
    return 1.0 - (Levenshtein(s1, s2) / Max(len1, len2))
}
Levenshtein(s1, s2)
{
    len1 := StrLen(s1)
    len2 := StrLen(s2)
    if (len1 == 0)
        return len2
    if (len2 == 0)
        return len1
    prev := []
    Loop len2 + 1
        prev.Push(A_Index - 1)
    Loop len1
    {
        i := A_Index
        curr := [i]
        Loop len2
        {
            j := A_Index
            cost := (SubStr(s1, i, 1) == SubStr(s2, j, 1)) ? 0 : 1
            curr.Push(Min(prev[j + 1] + 1, curr[j] + 1, prev[j] + cost))
        }
        prev := curr
    }
    return prev[len2 + 1]
}

; ---------- Shutdown ----------
; [BUG FIX] Removed invalid /full flag (not a real shutdown.exe option)
ShutdownComputer()
{
    result := MsgBox("لپ‌تاپ کاملاً خاموش می‌شود (Full shutdown).`nادامه می‌دهی؟", "تأیید خاموشی", "YesNo Icon?")
    if (result = "Yes")
        Run("shutdown /s /t 0")
}

; ---------- VPN ----------
LaunchVPN()
{
    global VpnExe, VpnAppId
    if (VpnExe != "" && FileExist(VpnExe))
    {
        Run(VpnExe)
        return true
    }
    if (VpnAppId != "")
    {
        Run("explorer.exe shell:AppsFolder\" VpnAppId)
        return true
    }
    found := SearchShortcut("hotspot")
    if (found != "")
    {
        Run(found)
        return true
    }
    MsgBox("VPN launch path not found.", "Error", 16)
    return false
}
StartVPN()
{
    global VpnWindowTitle, VpnConnectControl
    if !LaunchVPN()
        return
    if (VpnWindowTitle != "" && VpnConnectControl != "")
    {
        if WinWait(VpnWindowTitle, , 8)
        {
            WinActivate(VpnWindowTitle)
            Sleep(400)
            ControlClick(VpnConnectControl, VpnWindowTitle)
        }
    }
}

; ---------- Date / Jalali calendar ----------
ShowTodayDate()
{
    now := A_Now
    gy := Integer(SubStr(now, 1, 4))
    gm := Integer(SubStr(now, 5, 2))
    gd := Integer(SubStr(now, 7, 2))
    j := GregorianToJalali(gy, gm, gd)
    jalali := Format("{1:04d}/{2:02d}/{3:02d}", j[1], j[2], j[3])
    gregorian := Format("{1:04d}/{2:02d}/{3:02d}", gy, gm, gd)
    MsgBox("شمسی: " jalali "`nمیلادی: " gregorian, "تاریخ امروز", 64)
}
GregorianToJalali(gy, gm, gd)
{
    g_d_m := [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if (gy > 1600)
    {
        jy := 979
        gy -= 1600
    }
    else
    {
        jy := 0
        gy -= 621
    }
    gy2 := (gm > 2) ? gy + 1 : gy
    days := 365 * gy + Integer((gy2 + 3) / 4) - Integer((gy2 + 99) / 100) + Integer((gy2 + 399) / 400) - 80 + gd + g_d_m[gm]
    jy += 33 * Integer(days / 12053)
    days := Mod(days, 12053)
    jy += 4 * Integer(days / 1461)
    days := Mod(days, 1461)
    if (days > 365)
    {
        jy += Integer((days - 1) / 365)
        days := Mod(days - 1, 365)
    }
    if (days < 186)
    {
        jm := 1 + Integer(days / 31)
        jd := 1 + Mod(days, 31)
    }
    else
    {
        jm := 7 + Integer((days - 186) / 30)
        jd := 1 + Mod(days - 186, 30)
    }
    return [jy, jm, jd]
}

; ---------- Video downloader ----------
RunVideoDownloader()
{
    global VideoDownloader
    if !FileExist(VideoDownloader)
    {
        MsgBox("Video downloader not found:`n" VideoDownloader, "Error", 16)
        return
    }
    Run(VideoDownloader)
}

; ---------- Games / shortcuts ----------
OpenNamedTarget(name)
{
    global AliasMap, GameMap, LastSearch
    q := StrLower(Normalize(name))
    if (q == "")
        return
    searchQuery := q
    for alias, target in AliasMap
    {
        if FuzzyMatch(q, alias)
        {
            searchQuery := StrLower(target)
            break
        }
    }
    for key, target in GameMap
    {
        if FuzzyMatch(searchQuery, key)
        {
            Run(target)
            return
        }
    }
    found := SearchShortcut(searchQuery)
    if (found != "")
    {
        Run(found)
        return
    }
    hits := CacheSearch(q)
    if (hits.Length == 1)
    {
        Run(hits[1])
        return
    }
    if (hits.Length > 1)
    {
        LastSearch := hits
        list := ""
        for i, p in hits
            list .= i ". " ShortPath(p) "`n"
        MsgBox(list "`nبرای باز کردن بگو: نتیجه N", "نتایج حافظه اسکن", 64)
        return
    }
    ib := InputBox("این فرمان شناخته نشد.`nآن را به یک لینک یا مسیر فایل وصل کن (خالی = جستجوی گوگل):", "یادگیری فرمان جدید", "w480 h140")
    if (ib.Result = "OK")
    {
        target := Trim(ib.Value)
        if (target != "")
        {
            if IsUrl(target) || FileExist(target)
            {
                SaveCommand(name, target)
                Run(target)
                return
            }
            Flash("لینک یا مسیر معتبر نیست؛ به گوگل می‌رویم.")
        }
    }
    Run("https://www.google.com/search?q=" StrReplace(name, " ", "+"))
}
SearchShortcut(query)
{
    global GamesRoot
    static cache := Map()
    static cacheTime := 0
    q := StrLower(query)
    if (A_TickCount - cacheTime > 600000)
    {
        cache := Map()
        cacheTime := A_TickCount
    }
    if cache.Has(q)
        return cache[q]
    found := ""
    roots := [GamesRoot, A_Desktop, A_StartMenuCommon, A_StartMenu]
    for root in roots
    {
        Loop Files root "\*.lnk", "R"
        {
            if InStr(StrLower(A_LoopFileName), q)
            {
                found := A_LoopFileFullPath
                break 2
            }
        }
    }
    cache[q] := found
    return found
}

; ---------- Layout fix / learned commands / Word ----------
FixLayout(s)
{
    static m := Map(
        "q", "ض", "w", "ص", "e", "ث", "r", "ق", "t", "ف", "y", "غ", "u", "ع", "i", "ه", "o", "خ", "p", "ح",
        "a", "ش", "s", "س", "d", "ی", "f", "ب", "g", "ل", "h", "ا", "j", "ت", "k", "ن", "l", "م",
        "z", "ظ", "x", "ط", "c", "ز", "v", "ر", "b", "ذ", "n", "د", "m", "پ",
        ";", "ک", "'", "گ", "[", "ج", "]", "چ", ",", "و")
    out := ""
    for ch in StrSplit(s)
        out .= m.Has(ch) ? m[ch] : ch
    return out
}
LoadCommands()
{
    global CommandsFile
    m := Map()
    if !FileExist(CommandsFile)
        return m
    for line in StrSplit(FileRead(CommandsFile, "UTF-8"), "`n", "`r")
    {
        line := Trim(line)
        if (line == "" || InStr(line, ";") == 1)
            continue
        pos := InStr(line, "=")
        if (pos < 2)
            continue
        m[StrLower(Normalize(SubStr(line, 1, pos - 1)))] := Trim(SubStr(line, pos + 1))
    }
    return m
}
SaveCommand(key, target)
{
    global CommandsFile, LearnedMap
    k := StrLower(Normalize(key))
    LearnedMap[k] := target
    FileAppend(k "=" target "`n", CommandsFile, "UTF-8")
}
CreateWordDocument(name, content)
{
    if (name == "" || content == "")
    {
        MsgBox("اسم یا محتوا خالی است.", "خطا", 48)
        return
    }
    name := RegExReplace(name, '[\\/:*?"<>|]', "_")
    filepath := A_Desktop "\" name ".doc"
    html := "<html xmlns:o='urn:schemas-microsoft-com:office:office'`n"
    html .= "xmlns:w='urn:schemas-microsoft-com:office:word'`n"
    html .= "xmlns='http://www.w3.org/TR/REC-html40'>`n"
    html .= "<head><meta charset='utf-8'><title>" name "</title></head>`n"
    html .= "<body>`n"
    html .= StrReplace(content, "`n", "<br>`n")
    html .= "`n</body></html>"
    FileAppend(html, filepath, "UTF-8")
    Run(filepath)
}
ConfirmCommand(cmd, action, target)
{
    global LearnedMap
    if LearnedMap.Has(StrLower(Normalize(cmd)))
        return true
    result := MsgBox("آیا منظور شما این بود؟`n`n" action ":`n" target "`n`nاگر درست است Yes، وگرنه No را بزنید تا مسیر درست را نشان دهید.", "تأیید فرمان", "YesNo Icon?")
    if (result = "Yes")
    {
        SaveCommand(cmd, target)
        return true
    }
    ib := InputBox("مسیر یا لینک درست را وارد کنید:", "یادگیری فرمان صحیح", "w480 h140")
    if (ib.Result = "OK")
    {
        newTarget := Trim(ib.Value)
        if (newTarget != "")
        {
            if IsUrl(newTarget) || FileExist(newTarget)
            {
                SaveCommand(cmd, newTarget)
                Run(newTarget)
                return false
            }
            MsgBox("مسیر معتبر نیست.", "خطا", 48)
        }
    }
    return false
}

; ---------- Timer / reminder ----------
ParseTimer(cmd)
{
    global TimerEndTick
    s := cmd
    Loop 10
        s := StrReplace(s, Chr(1776 + A_Index - 1), String(A_Index - 1))
    if !RegExMatch(s, "i)(تایمر|یادم بنداز|یادآوری|timer|reminder)\D{0,10}(\d+)\s*(ثانیه|دقیقه|ساعت|second|minute|hour)?", &tm)
        return false
    n := Integer(tm[2])
    unit := tm[3]
    if (unit == "")
        unit := "دقیقه"
    if InStr(unit, "ثانیه") || InStr(unit, "second")
        ms := n * 1000
    else if InStr(unit, "ساعت") || InStr(unit, "hour")
        ms := n * 3600000
    else
        ms := n * 60000
    SetTimer(TimerFire, -ms)
    TimerEndTick := A_TickCount + ms
    ToolTip("تایمر تنظیم شد: " n " " unit)
    SetTimer(RemoveHint, -2500)
    return true
}
TimerFire()
{
    global TimerEndTick
    TimerEndTick := 0
    SoundBeep(750, 400)
    SoundBeep(950, 600)
    MsgBox("وقت تمام شد!", "یادآور", 64)
}

; ---------- Scenario modes ----------
TryScenario(c)
{
    global ScenarioMap
    for modeName in ScenarioMap
    {
        if InStr(c, StrLower(Normalize(modeName)))
        {
            RunScenario(modeName)
            return true
        }
    }
    return false
}
RunScenario(name)
{
    global ScenarioMap, GamesRoot, ModeName, ModeStartTick
    for action in ScenarioMap[name]
    {
        if (action == "vpn")
            StartVPN()
        else if (action == "games")
            Run(GamesRoot)
        else if InStr(action, "run|") == 1
            Run(SubStr(action, 5))
        else if InStr(action, "kill|") == 1
        {
            try ProcessClose(SubStr(action, 6))
        }
        Sleep(400)
    }
    ModeName := name
    ModeStartTick := A_TickCount
    ToolTip("حالت اجرا شد: " name)
    SetTimer(RemoveHint, -2500)
}

; ---------- Notes ----------
HandleNote(c)
{

    notesFile := A_ScriptDir "\notes.txt"
    if InStr(c, "یادداشت از کلیپ")
    {
        txt := Trim(A_Clipboard)
        if (txt == "")
        {
            Flash("کلیپ‌بورد خالی است.")
            return true
        }
        now := A_Now
        j := GregorianToJalali(Integer(SubStr(now,1,4)), Integer(SubStr(now,5,2)), Integer(SubStr(now,7,2)))
        stamp := Format("{1:04d}/{2:02d}/{3:02d} {4:02d}:{5:02d}", j[1], j[2], j[3], Integer(SubStr(now,9,2)), Integer(SubStr(now,11,2)))
        FileAppend(stamp "`n" txt "`n`n", notesFile, "UTF-8")
        DashSay("یادداشت از کلیپ‌بورد ذخیره شد (" StrLen(txt) " نویسه)")
        Flash("یادداشت از کلیپ‌بورد ذخیره شد.")
        return true
    }
    if InStr(c, "یادداشت ها") || InStr(c, "یادداشتها") || InStr(c, "notes") || (c == "یادداشت")
    {
        if FileExist(notesFile)
            Run(notesFile)
        else
            Flash("هنوز یادداشتی نیست.")
        return true
    }
    if RegExMatch(c, "^یادداشت\s*[:：]?\s*(.+)$", &nt)
    {
        now := A_Now
        j := GregorianToJalali(Integer(SubStr(now, 1, 4)), Integer(SubStr(now, 5, 2)), Integer(SubStr(now, 7, 2)))
        stamp := Format("{1:04d}/{2:02d}/{3:02d} {4:02d}:{5:02d}", j[1], j[2], j[3], Integer(SubStr(now, 9, 2)), Integer(SubStr(now, 11, 2)))
        FileAppend(stamp " - " nt[1] "`n", notesFile, "UTF-8")
        Flash("یادداشت ذخیره شد.")
        return true
    }
    return false
}

; ---------- Tiko-chi ----------
HandleTiko(c)
{
    global TikoTime
    if InStr(c, "سینک تیکو") || InStr(c, "sync tiko")
    {
        SyncTiko()
        return true
    }
    if InStr(c, "یادآور تیکو") || InStr(c, "یادآوری تیکو")
    {
        if InStr(c, "تست")
        {
            FireTikoReminder()
            return true
        }
        s := c
        Loop 10
            s := StrReplace(s, Chr(1776 + A_Index - 1), String(A_Index - 1))
        if RegExMatch(s, "(\d{1,2}[:：]\d{2})", &tm)
        {
            parts := StrSplit(StrReplace(tm[1], "：", ":"), ":")
            hh := Format("{1:02d}", Integer(parts[1]))
            mm := Format("{1:02d}", Integer(parts[2]))
            if (Integer(hh) < 24 && Integer(mm) < 60)
            {
                TikoTime := hh ":" mm
                Flash("زمان یادآور تیکو: " TikoTime)
                return true
            }
        }
        Flash("یادآور تیکو هر روز ساعت " TikoTime)
        return true
    }
    return false
}
FireTikoReminder()
{
    global TikoUrl, TikoLastFire, TikoStateFile
    TikoLastFire := SubStr(A_Now, 1, 8)
    if FileExist(TikoStateFile)
        FileDelete(TikoStateFile)
    FileAppend(TikoLastFire, TikoStateFile, "UTF-8")
    data := LoadTikoCache()
    body := "وقت یادآوری عادت‌ها و وظایف تیکوچی رسید.`n"
    if (data[1].Length)
        body .= "`nعادت‌ها: " JoinList(data[1])
    if (data[2].Length)
        body .= "`nوظیفه‌ها: " JoinList(data[2])
    body .= "`n`nبرو تیکوچی و تیک بزن؟"
    result := MsgBox(body, "یادآور تیکوچی", "YesNo Icon!")
    if (result = "Yes")
        Run(TikoUrl)
}
CheckTikoReminder()
{
    global TikoTime, TikoLastFire
    now := A_Now
    today := SubStr(now, 1, 8)
    if (TikoLastFire == today)
        return
    hm := SubStr(now, 9, 2) ":" SubStr(now, 11, 2)
    if (StrCompare(hm, TikoTime) >= 0)
        FireTikoReminder()
}
SyncTiko()
{
    global TikoTime, TikoCacheFile
    dir := EnvGet("USERPROFILE") "\Downloads"
    newest := ""
    newestTime := 0
    Loop Files dir "\routine-backup-*.json"
    {
        if (A_LoopFileTimeModified > newestTime)
        {
            newestTime := A_LoopFileTimeModified
            newest := A_LoopFileFullPath
        }
    }
    if (newest == "")
    {
        Flash("فایل routine-backup در Downloads پیدا نشد.")
        return
    }
    src := FileRead(newest, "UTF-8")
    habits := ParseTikoNames(src, "habits", "logs")
    tasks := ParseTikoNames(src, "tasks", "habits")
    t := ParseTikoTime(src)
    if (t != "")
        TikoTime := t
    if FileExist(TikoCacheFile)
        FileDelete(TikoCacheFile)
    buf := ""
    for h in habits
        buf .= "H|" h "`n"
    for t2 in tasks
        buf .= "T|" t2 "`n"
    FileAppend(buf, TikoCacheFile, "UTF-8")
    Flash("سینک شد: " habits.Length " عادت و " tasks.Length " وظیفه | یادآور " TikoTime)
}
ParseTikoNames(src, startKey, endKey)
{
    q := Chr(34)
    p1 := InStr(src, q startKey q)
    if (!p1)
        return []
    p2 := InStr(src, q endKey q, false, p1)
    if (!p2)
        p2 := StrLen(src)
    block := SubStr(src, p1, p2 - p1)
    out := []
    parts := StrSplit(block, q "name" q)
    Loop parts.Length - 1
    {
        v := Trim(ExtractQuotedAfterColon(parts[A_Index + 1]))
        if (v != "")
            out.Push(v)
    }
    return out
}
ExtractQuotedAfterColon(chunk)
{
    c := InStr(chunk, ":")
    if (!c)
        return ""
    q1 := InStr(chunk, Chr(34), false, c)
    if (!q1)
        return ""
    q2 := InStr(chunk, Chr(34), false, q1 + 1)
    if (!q2)
        return ""
    return SubStr(chunk, q1 + 1, q2 - q1 - 1)
}
ParseTikoTime(src)
{
    q := Chr(34)
    p := InStr(src, q "time" q)
    if (!p)
        return ""
    t := Trim(ExtractQuotedAfterColon(SubStr(src, p)))
    return RegExMatch(t, "^\d{1,2}:\d{2}$") ? t : ""
}
LoadTikoCache()
{
    global TikoCacheFile
    habits := []
    tasks := []
    if FileExist(TikoCacheFile)
    {
        for line in StrSplit(FileRead(TikoCacheFile, "UTF-8"), "`n", "`r")
        {
            line := Trim(line)
            if InStr(line, "H|") == 1
                habits.Push(SubStr(line, 3))
            else if InStr(line, "T|") == 1
                tasks.Push(SubStr(line, 3))
        }
    }
    return [habits, tasks]
}
; ===================================================
; PART 5/5 - Palette, Scan, News, Image/Site/Python,
;            HandleEnd/LogEndSession, Time tracker
; ===================================================

; ---------- Globals for this part ----------
; [BUG FIX] Removed duplicate "global LastImageResults" and
; "global ImageIndexFile" - both are already declared in CONFIG (Part 1).
; Re-declaring them caused a "duplicate declaration" error in AHK v2.
global PaletteGui := ""
global PaletteEdit := ""
global PaletteList := ""
global PaletteCommands := []
global PaletteFiltered := []

; ===================================================
; Palette (Ctrl+Alt+Space)
; ===================================================
OpenPalette()
{
    global PaletteGui, PaletteEdit, PaletteList, PaletteCommands, PaletteFiltered
    if (PaletteGui != "")
    {
        try PaletteGui.Destroy()
    }
    PaletteCommands := BuildPaletteCommands()
    PaletteFiltered := PaletteCommands.Clone()
    PaletteGui := Gui("+AlwaysOnTop +ToolWindow", "پالت فرمان")
    PaletteGui.BackColor := "111111"
    PaletteGui.SetFont("s11", "Segoe UI")
    PaletteEdit := PaletteGui.Add("Edit", "x12 y12 w360 h30 +Background222222 cFFFFFF", "")
    PaletteEdit.OnEvent("Change", PaletteFilter)
    runBtn := PaletteGui.Add("Button", "x380 y12 w92 h30 Default", "اجرا")
    runBtn.OnEvent("Click", PaletteRunTyped)
    PaletteList := PaletteGui.Add("ListView", "x12 y50 w460 h270 -Hdr -Multi +Background222222 cFFFFFF", ["فرمان"])
    PaletteList.ModifyCol(1, 440)
    PaletteList.OnEvent("DoubleClick", PaletteRunSelected)
    PaletteRefreshList()
    PaletteGui.OnEvent("Close", (*) => PaletteGui.Hide())
    PaletteGui.OnEvent("Escape", (*) => PaletteGui.Hide())
    PaletteGui.Show("Center w484 h334")
    PaletteEdit.Focus()
}
BuildPaletteCommands()
{
    cmds := ["خاموشی کامل", "فیلترشکن", "دانلودر", "تاریخ", "وضعیت سیستم", "فضای دیسک",
        "پومودورو", "توقف پومودورو", "تاریخچه کلیپ بورد", "ذخیره چیدمان", "بازیابی چیدمان", "مرتب سازی دانلودها",
        "قیمت طلا", "قیمت دلار", "قیمت بیت کوین", "اوقات شرعی", "مناسبت های امروز", "اخبار",
        "تولید رمز عبور", "کیوآر", "خلاصه کن", "چت با هوش مصنوعی", "قفل هوش مصنوعی",
        "اسکن بعدی", "وضعیت اسکن", "حالت درس", "حالت بازی", "حالت فیلم",
        "سینک تیکو", "یادآور تیکو"]
    for k in LearnedMap
        cmds.Push(k)
    return cmds
}
PaletteFilter(ctrl, info)
{
    global PaletteEdit, PaletteCommands, PaletteFiltered
    q := StrLower(Normalize(PaletteEdit.Value))
    PaletteFiltered := []
    for c in PaletteCommands
        if (q == "" || InStr(StrLower(Normalize(c)), q))
            PaletteFiltered.Push(c)
    PaletteRefreshList()
}
PaletteRefreshList()
{
    global PaletteList, PaletteFiltered
    PaletteList.Delete()
    for c in PaletteFiltered
        PaletteList.Add("", c)
    if (PaletteFiltered.Length > 0)
        PaletteList.Modify(1, "Select Focus")
}
PaletteRunSelected(ctrl, row := 0)
{
    global PaletteFiltered, PaletteGui
    if (row == 0)
        row := ctrl.GetNext(0)
    if (row < 1 || row > PaletteFiltered.Length)
        return
    cmd := PaletteFiltered[row]
    PaletteGui.Hide()
    Handle(cmd)
}
PaletteRunTyped(ctrl, info)
{
    global PaletteEdit, PaletteList, PaletteFiltered, PaletteGui
    row := PaletteList.GetNext(0)
    cmd := ""
    if (row >= 1 && row <= PaletteFiltered.Length)
        cmd := PaletteFiltered[row]
    else
        cmd := Trim(PaletteEdit.Value)
    if (cmd == "")
        return
    PaletteGui.Hide()
    Handle(cmd)
}

; ===================================================
; Sectioned disk scan / cache search
; ===================================================
HandleScan(c)
{
    if InStr(c, "اسکن بعدی") || InStr(c, "next scan")
    {
        StartNextScan()
        return true
    }
    if InStr(c, "اسکن دوباره") || InStr(c, "rescan")
    {
        sec := NormalizeSectionKey(RemoveWords(c, ["اسکن", "دوباره"]))
        if (sec == "")
            sec := "user"
        RescanSection(sec)
        return true
    }
    if InStr(c, "وضعیت اسکن") || InStr(c, "scan status")
    {
        ScanStatus()
        return true
    }
    if RegExMatch(c, "^نتیجه\s*(\d+)$", &res)
    {
        OpenSearchResult(Integer(res[1]))
        return true
    }
    if InStr(c, "پیدا کن") || InStr(c, "جستجوی فایل") || InStr(c, "find") || InStr(c, "search")
    {
        q := Trim(StrReplace(c, "پیدا کن", ""))
        q := Trim(StrReplace(q, "جستجوی فایل", ""))
        q := Trim(StrReplace(q, "find", ""))
        q := Trim(StrReplace(q, "search", ""))
        if (q == "")
        {
            Flash("چی رو پیدا کنم؟")
            return true
        }
        SearchCache(q)
        return true
    }
    return false
}
StartNextScan()
{
    global ScanScript, ScanMarker, ScanSections, ScanRunning, ScanCurrentSection, ScanStartTick
    if ScanRunning
    {
        Flash("اسکن در حال اجراست: " ScanCurrentSection)
        return
    }
    done := ReadScanState()
    nextSec := ""
    for sec in ScanSections
    {
        if !done.Has(sec)
        {
            nextSec := sec
            break
        }
    }
    if (nextSec == "")
    {
        Flash("اسکن همه بخش‌ها قبلاً انجام شده. برای تکرار بگو: اسکن دوباره d")
        return
    }
    ScanRunning := true
    ScanCurrentSection := nextSec
    ScanStartTick := A_TickCount
    if FileExist(ScanMarker)
        FileDelete(ScanMarker)
    Run('python "' ScanScript '" ' nextSec ' "' ScanCache '" "' ScanMarker '"', "D:\Python", "Hide")
    Flash("اسکن بخش " nextSec " شروع شد.")
    SetTimer(CheckScanDone, 2000)
}
CheckScanDone()
{
    global ScanMarker, ScanRunning, ScanCurrentSection, ScanStartTick
    if !ScanRunning
    {
        SetTimer(CheckScanDone, 0)
        return
    }
    if FileExist(ScanMarker)
    {
        SetTimer(CheckScanDone, 0)
        ScanRunning := false
        elapsed := Round((A_TickCount - ScanStartTick) / 1000)
        AppendScanState(ScanCurrentSection)
        Flash("اسکن " ScanCurrentSection " تمام شد (" elapsed " ثانیه).")
    }
}
AppendScanState(sec)
{
    global ScanState
    done := ReadScanState()
    if done.Has(sec)
        return
    FileAppend(sec "`n", ScanState, "UTF-8")
}
ReadScanState()
{
    global ScanState
    done := Map()
    if FileExist(ScanState)
    {
        for line in StrSplit(FileRead(ScanState, "UTF-8"), "`n", "`r")
        {
            line := Trim(line)
            if (line != "")
                done[line] := true
        }
    }
    return done
}
ScanStatus()
{
    global ScanSections, ScanCache
    done := ReadScanState()
    total := 0
    if FileExist(ScanCache)
    {
        for line in StrSplit(FileRead(ScanCache, "UTF-8"), "`n", "`r")
            if (Trim(line) != "")
                total++
    }
    msg := "وضعیت اسکن:`n"
    for sec in ScanSections
        msg .= sec ": " (done.Has(sec) ? "انجام شده" : "انجام نشده") "`n"
    msg .= "تعداد آیتم‌های کش: " total
    MsgBox(msg, "وضعیت اسکن", 64)
}
CacheSearch(query)
{
    global ScanCache, LastSearch
    hits := []
    q := StrLower(query)
    if FileExist(ScanCache)
    {
        for line in StrSplit(FileRead(ScanCache, "UTF-8"), "`n", "`r")
        {
            line := Trim(line)
            if (line == "")
                continue
            if InStr(StrLower(line), q)
            {
                p1 := InStr(line, "|")
p2 := InStr(line, "|", , p1 + 1)
path := SubStr(line, p2 + 1)
                if FileExist(path)
                    hits.Push(path)
                if (hits.Length >= 20)
                    break
            }
        }
    }
    LastSearch := hits
    return hits
}
SearchCache(query)
{
    hits := CacheSearch(query)
    if (hits.Length == 0)
    {
        Flash("چیزی در کش پیدا نشد.")
        return
    }
    if (hits.Length == 1)
    {
        Run(hits[1])
        return
    }
    list := ""
    for i, p in hits
    {
        if (i > 10)
            break
        list .= i ". " ShortPath(p) "`n"
    }
    list .= "`nبرای باز کردن بگو: نتیجه N"
    MsgBox(list, "نتایج جستجو", 64)
}
OpenSearchResult(n)
{
    global LastSearch
    if (n < 1 || n > LastSearch.Length)
    {
        Flash("نتیجه‌ای با این شماره نیست.")
        return
    }
    Run(LastSearch[n])
}
RescanSection(sec)
{
    global ScanState, ScanMarker, ScanScript, ScanRunning, ScanCurrentSection, ScanStartTick
    if ScanRunning
    {
        Flash("اسکن در حال اجراست.")
        return
    }
    done := ReadScanState()
    if done.Has(sec)
    {
        buf := ""
        for s in done
            if (s != sec)
                buf .= s "`n"
        if FileExist(ScanState)
            FileDelete(ScanState)
        FileAppend(buf, ScanState, "UTF-8")
    }
    ScanRunning := true
    ScanCurrentSection := sec
    ScanStartTick := A_TickCount
    if FileExist(ScanMarker)
        FileDelete(ScanMarker)
    Run('python "' ScanScript '" ' sec ' "' ScanCache '" "' ScanMarker '"', "D:\Python", "Hide")
    Flash("اسکن دوباره بخش " sec " شروع شد.")
    SetTimer(CheckScanDone, 2000)
}
NormalizeSectionKey(s)
{
    s := StrLower(Normalize(s))
    if (s == "کاربر" || s == "user" || s == "u")
        return "user"
    if (s == "دی" || s == "d")
        return "d"
    if (s == "برنامه ها" || s == "برنامهها" || s == "programs" || s == "p")
        return "programs"
    return s
}

; ===================================================
; News briefing
; ===================================================
RunNews()
{
    global NewsScript, NewsLatest, NewsRunning, NewsStartTick
    if NewsRunning
    {
        Flash("دریافت اخبار در حال اجراست.")
        return
    }
    NewsRunning := true
    NewsStartTick := A_TickCount
    if FileExist(NewsLatest)
        FileDelete(NewsLatest)
    Run('"' A_ComSpec '" /c python "' NewsScript '"', , "Hide")
    Flash("در حال دریافت اخبار…")
    SetTimer(CheckNewsDone, 2000)
}
CheckNewsDone()
{
    global NewsLatest, NewsRunning, NewsStartTick
    if !NewsRunning
    {
        SetTimer(CheckNewsDone, 0)
        return
    }
    if FileExist(NewsLatest)
    {
        SetTimer(CheckNewsDone, 0)
        NewsRunning := false
        ShowNewsGui(FileRead(NewsLatest, "UTF-8"))
        return
    }
    if (A_TickCount - NewsStartTick > 60000)
    {
        SetTimer(CheckNewsDone, 0)
        NewsRunning := false
        Flash("دریافت اخبار طول کشید یا ناموفق بود.")
    }
}
ShowNewsGui(content)
{
    global NewsGui, NewsEdit
    if (NewsGui != "")
    {
        try NewsGui.Destroy()
    }
    NewsGui := Gui("+AlwaysOnTop", "اخبار")
    NewsGui.BackColor := "111111"
    NewsGui.SetFont("s11", "Segoe UI")
    NewsEdit := NewsGui.Add("Edit", "x12 y12 w520 h380 +ReadOnly +Background222222 cEEEEEE", content)
    NewsGui.OnEvent("Close", (*) => NewsGui.Hide())
    NewsGui.OnEvent("Escape", (*) => NewsGui.Hide())
    NewsGui.Show("Center")
}

; ===================================================
; Image search
; ===================================================
SearchImages(c)
{
    global ImageIndexFile, LastImageResults
    query := StrReplace(c, "عکس", "")
    query := StrReplace(query, "تصویر", "")
    query := Trim(query)
    if (query == "")
    {
        Flash("دنبال چه عکسی می‌گردی؟")
        return
    }
    index := LoadImageIndex()
    if (index.Length == 0)
    {
        Flash("فایل ایندکس عکس‌ها وجود ندارد یا خالی است.")
        return
    }
    results := []
    for img in index
        if InStr(img.text, query) || InStr(img.name, query) || InStr(img.folder, query)
            results.Push(img.path)
    if (results.Length == 0)
    {
        Flash("عکسی با عنوان " query " پیدا نشد.")
        return
    }
    LastImageResults := results
    if (results.Length == 1)
    {
        Run(results[1])
        return
    }
    list := ""
    for i, p in results
    {
        if (i > 15)
            break
        list .= i ". " ShortPath(p) "`n"
    }
    list .= "`nبرای باز کردن بگو: عکس N"
    MsgBox(list, "نتایج جستجوی عکس", 64)
}
OpenImageResult(n)
{
    global LastImageResults
    if (LastImageResults.Length == 0)
    {
        Flash("اول یک جستجوی عکس انجام بده.")
        return
    }
    if (n < 1 || n > LastImageResults.Length)
    {
        Flash("عکسی با این شماره نیست.")
        return
    }
    Run(LastImageResults[n])
}
LoadImageIndex()
{
    global ImageIndexFile
    out := []
    if !FileExist(ImageIndexFile)
        return out
    src := FileRead(ImageIndexFile, "UTF-8")
    pos := 1
    Loop
    {
        start := InStr(src, "{", , pos)
        if (!start)
            break
        end := InStr(src, "}", , start)
        if (!end)
            break
        obj := SubStr(src, start, end - start + 1)
        item := {text: ExtractJsonField(obj, "text"), name: ExtractJsonField(obj, "name"), folder: ExtractJsonField(obj, "folder"), path: ExtractJsonField(obj, "path")}
        out.Push(item)
        pos := end + 1
    }
    return out
}
ExtractJsonField(obj, key)
{
    q := Chr(34)
    pattern := q key q "\s*:\s*" q "([^" q "]*)" q
    if RegExMatch(obj, pattern, &m)
        return m[1]
    return ""
}

; ===================================================
; Site search
; ===================================================
SiteAction(cmd)
{
    sites := Map(
        "گوگل", "https://www.google.com/search?q=",
        "یوتیوب", "https://www.youtube.com/results?search_query=",
        "ایسنا", "https://www.isna.ir/search?keyword=",
        "ویکی پدیا", "https://fa.wikipedia.org/wiki/Special:Search?search=")
    rest := Trim(StrReplace(cmd, "جستجو در سایت", ""))
    foundSite := ""
    prefix := ""
    for site, url in sites
    {
        if InStr(rest, site)
        {
            foundSite := site
            prefix := url
            break
        }
    }
    if (foundSite == "")
    {
        Flash("کدوم سایت؟ (گوگل، یوتیوب، ایسنا، ویکی پدیا)")
        return
    }
    q := Trim(StrReplace(rest, foundSite, ""))
    if (q == "")
    {
        Flash("چی رو جستجو کنم؟")
        return
    }
    Run(prefix StrReplace(q, " ", "+"))
}

; ===================================================
; Run Python script with args
; ===================================================
RunPythonScript(cmd)
{
    rest := Trim(StrReplace(cmd, "اجرا کن پایتون", ""))
    rest := Trim(StrReplace(rest, "run python", ""))
    if (rest == "")
    {
        Flash("کدوم اسکریپت پایتون؟")
        return
    }
    parts := StrSplit(rest, " ")
    script := parts[1]
    args := ""
    Loop parts.Length - 1
        args .= " " parts[A_Index + 1]
    scriptPath := "D:/Python/" script
    if !FileExist(scriptPath)
        scriptPath := "D:/Python/" script ".py"
    if !FileExist(scriptPath)
    {
        Flash("اسکریپت پیدا نشد: " script)
        return
    }
    Run('python "' scriptPath '"' args)
}

; ===================================================
; End active mode / stop timer
; ===================================================
HandleEnd(c)
{
    global ModeName, ModeStartTick, TimerEndTick
    if !(InStr(c, "تمام") || InStr(c, "تموم") || InStr(c, "پایان حالت") || InStr(c, "پایان"))
        return false
    msg := ""
    if (TimerEndTick > 0)
    {
        remain := Round((TimerEndTick - A_TickCount) / 60000, 1)
        SetTimer(TimerFire, 0)
        TimerEndTick := 0
        msg := "تایمر لغو شد (" remain " دقیقه مانده بود)."
    }
    if (ModeName != "")
    {
        mins := Round((A_TickCount - ModeStartTick) / 60000)
        LogEndSession(ModeName, mins)
        msg .= (msg != "" ? "`n" : "") ModeName " پایان یافت و در یادداشت‌ها ثبت شد (" mins " دقیقه)."
        ModeName := ""
        ModeStartTick := 0
    }
    if (msg == "")
        msg := "حالت یا تایمر فعالی برای پایان دادن نیست."
    Flash(msg, 3500)
    return true
}
LogEndSession(modeName, mins)
{
    notesFile := A_ScriptDir "/notes.txt"
    now := A_Now
    j := GregorianToJalali(Integer(SubStr(now, 1, 4)), Integer(SubStr(now, 5, 2)), Integer(SubStr(now, 7, 2)))
    stamp := Format("{1:04d}/{2:02d}/{3:02d} {4:02d}:{5:02d}", j[1], j[2], j[3], Integer(SubStr(now, 9, 2)), Integer(SubStr(now, 11, 2)))
    FileAppend(stamp " - پایان " modeName " (" mins " دقیقه)`n", notesFile, "UTF-8")
}

; ===================================================
; Time tracker + nightly report
; ===================================================
TrackActiveWindow()
{
    global TimeLog
    hwnd := WinExist("A")
    if (!hwnd)
        return
    proc := ""
    try proc := WinGetProcessName("ahk_id " hwnd)
    if (proc == "")
        return
    if TimeLog.Has(proc)
        TimeLog[proc] += 10
    else
        TimeLog[proc] := 10
}
CheckDailyTasks()
{
    global LastDailyReportDate, ModeName, ModeStartTick
    now := A_Now
    today := SubStr(now, 1, 8)
    hh := Integer(SubStr(now, 9, 2))
    if (LastDailyReportDate == today)
        return
    if (hh == 23)
    {
        LastDailyReportDate := today
        if (ModeName != "")
        {
            mins := Round((A_TickCount - ModeStartTick) / 60000)
            LogEndSession(ModeName, mins)
            ModeName := ""
            ModeStartTick := 0
        }
        GenerateTimeReport()
    }
}
GenerateTimeReport()
{
    global TimeLog
    netReport := ""
    tempFile := A_ScriptDir "\net_report.txt"
    try
    {
        RunWait(A_ComSpec ' /c python "D:/Python/net_usage.py" report > "' tempFile '"', , "Hide")
        if FileExist(tempFile)
            netReport := Trim(FileRead(tempFile, "UTF-8"))
    }
    pairs := []
    for proc, sec in TimeLog
        pairs.Push({proc: proc, sec: sec})
    Loop pairs.Length - 1
    {
        Loop pairs.Length - A_Index
        {
            if (pairs[A_Index].sec < pairs[A_Index + 1].sec)
            {
                tmp := pairs[A_Index]
                pairs[A_Index] := pairs[A_Index + 1]
                pairs[A_Index + 1] := tmp
            }
        }
    }
    report := "🌙 گزارش شبانه`n`nپرمصرف‌ترین برنامه‌ها:`n"
    count := 0
    for p in pairs
    {
        count++
        if (count > 5)
            break
        report .= count ". " p.proc ": " Round(p.sec / 60) " دقیقه`n"
    }
    if (netReport != "")
        report .= "`nمصرف اینترنت:`n" netReport
    MsgBox(report, "گزارش شبانه", 64)
}
; ===================================================
; Feature 19 - Virtual Hand v2 (Optimized)
; ===================================================
StartVirtualHand()
{
    global VHandScript, VHandPython, VHandPid, VHandCmdFile
    
    ; Clean up any dead process first
    if (VHandPid != 0)
    {
        if !ProcessExist(VHandPid)
            VHandPid := 0
        else
        {
            try ProcessClose(VHandPid)
            Sleep(300)
            VHandPid := 0
        }
    }
    
    if !FileExist(VHandPython)
    {
        MsgBox("محیط پایتون پیدا نشد:`n" VHandPython, "خطا", 16)
        return
    }
    if !FileExist(VHandScript)
    {
        MsgBox("فایل virtual_hand.py پیدا نشد.", "خطا", 16)
        return
    }
    
    res := MsgBox("هشدار حریم خصوصی:`n`nوبکم فعال می‌شود (کاملاً آفلاین).`n`nادامه؟", "دست مجازی", "YesNo Icon!")
    if (res != "Yes")
        return
    
    try FileDelete(VHandCmdFile)
    Sleep(50)
    FileAppend("START", VHandCmdFile, "UTF-8")
    
    try
    {
        Run('"' VHandPython '" "' VHandScript '"', "D:\Python", "Hide", &VHandPid)
        Flash("✋ دست مجازی فعال شد. دستت را در مرکز دوربین بگیر.")
        SetTimer(CheckVHandAlive, -3000)
    }
    catch as e
    {
        Flash("خطا: " e.Message)
        VHandPid := 0
    }
}

CheckVHandAlive()
{
    global VHandPid
    if (VHandPid == 0)
        return
    if !ProcessExist(VHandPid)
    {
        Flash("⚠️ دست مجازی بسته شد. برای جزئیات: D:\Python\vh_log.txt")
        VHandPid := 0
    }
}

StopVirtualHand()
{
    global VHandPid, VHandCmdFile
    
    if (VHandPid == 0)
    {
        ; تلاش برای پیدا کردن پروسه orphan با WMI
        killed := KillOrphanPython()
        if (killed > 0)
            Flash("دست مجازی (orphan) بسته شد.")
        else
            Flash("دست مجازی فعال نیست.")
        return
    }
    
    ; 1. ارسال فرمان STOP از طریق فایل
    try FileDelete(VHandCmdFile)
    Sleep(50)
    FileAppend("STOP", VHandCmdFile, "UTF-8")
    
    ; 2. صبر برای بستن graceful
    Sleep(400)
    
    ; 3. Force kill اگر هنوز زنده بود
    if ProcessExist(VHandPid)
    {
        try ProcessClose(VHandPid)
    }
    
    VHandPid := 0
    try FileDelete(VHandCmdFile)
    Flash("✋ دست مجازی متوقف شد.")
}

; تابع کمکی: پیدا و بستن پروسه‌های python.exe که virtual_hand.py اجرا کرده‌اند
KillOrphanPython()
{
    killed := 0
    try
    {
        wmi := ComObjGet("winmgmts:\\.\root\cimv2")
        procs := wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='python.exe'")
        for proc in procs
        {
            if InStr(proc.CommandLine, "virtual_hand.py")
            {
                try ProcessClose(proc.ProcessId)
                killed++
            }
        }
    }
    return killed
}
; ===================================================
; agent_bridge.ahk — HUD + approvals + auto-start
; ===================================================
global AgentDir := "D:\Python"
global AgentStateDir := "D:\Work\agent-state"
global AgentToken := ""
global AgentSince := 0
global AgentPollBusy := false
global AgentHudGui := ""
global AgentHudText := ""
global AgentShownApprovals := Map()
global AgentEngineOn := false

^!a:: {
    ib := InputBox("دستور ایجنت (مثال: ایجنت go https://example.com)", "ایجنت", "w520 h130")
    if (ib.Result = "OK" && Trim(ib.Value) != "")
        Handle(Trim(ib.Value))
}

AgentInit() {
    global AgentToken, AgentStateDir
    try AgentToken := Trim(FileRead(AgentStateDir "\bridge.token", "UTF-8"))
    try Run('"' A_AhkPath '" "D:\Python\agent_watchdog.ahk"', , "Hide")
    AgentEnsureBridge()
    SetTimer(AgentPoll, 800)
}

AgentHttp(method, path, body := "") {
    global AgentToken
    try {
        req := ComObject("WinHttp.WinHttpRequest.5.1")
        req.Open(method, "http://127.0.0.1:8765" . path, false)
        req.SetTimeouts(700, 700, 700, 2500)
        req.SetRequestHeader("X-Agent-Token", AgentToken)
        if (body != "")
            req.SetRequestHeader("Content-Type", "text/plain; charset=utf-8")
        req.Send(body)
        return req.ResponseText
    } catch {
        return ""
    }
}

AgentEnsureBridge() {
    global AgentDir
    static lastTry := 0
    if (A_TickCount - lastTry < 10000)
        return
    lastTry := A_TickCount
    if (AgentHttp("GET", "/health") != "")
        return
    Run("python -m agent bridge", AgentDir, "Hide")
    Sleep(1200)
    Run("python -m agent.daemon", AgentDir, "Hide")
}

AgentEnsureDaemon() {
    global AgentDir
    static lastTry := 0
    if (A_TickCount - lastTry < 15000)
        return
    lastTry := A_TickCount
    Run("python -m agent.daemon", AgentDir, "Hide")
}

AgentPoll() {
    global AgentSince, AgentPollBusy, AgentEngineOn
    if AgentPollBusy
        return
    AgentPollBusy := true
    resp := AgentHttp("GET", "/ahk/poll?since=" . AgentSince)
    if (resp = "") {
        AgentPollBusy := false
        AgentEnsureBridge()
        return
    }
    for line in StrSplit(resp, "`n", "`r") {
        line := Trim(line)
        if (line = "")
            continue
        tag := SubStr(line, 1, 1)
        if (tag = "H") {
            AgentEngineOn := (SubStr(line, 3) = "1")
            if !AgentEngineOn
                AgentEnsureDaemon()
        } else if (tag = "E") {
            p := StrSplit(line, " ", , 4)
            if (p.Length < 3)
                continue
            AgentSince := Integer(p[2])
            AgentShowEvent(p[3], p.Length >= 4 ? p[4] : "")
        } else if (tag = "A") {
            p := StrSplit(line, " ", , 4)
            if (p.Length >= 3)
                AgentAskApproval(Integer(p[2]), p.Length >= 4 ? p[4] : p[3])
        }
    }
    AgentPollBusy := false
}

AgentShowEvent(kind, text) {
    hud := (text != "") ? kind . ": " . text : kind
    AgentHud(hud)
    for k in ["captcha", "login", "step_failed", "mission_done", "download_pending", "emergency_stop"]
        if (k = kind) {
            Flash("🤖 " . hud, 6000)
            SoundBeep(700, 150)
            break
        }
}

AgentHud(text) {
    global AgentHudGui, AgentHudText
    if (AgentHudGui = "") {
        AgentHudGui := Gui("+AlwaysOnTop +ToolWindow -Caption", "AgentHUD")
        AgentHudGui.BackColor := "111111"
        AgentHudGui.SetFont("s10 cEEEEEE", "Segoe UI")
        AgentHudText := AgentHudGui.Add("Text", "w430", "")
    }
    AgentHudText.Value := text
    AgentHudGui.Show("x" . (A_ScreenWidth - 450) . " y" . (A_ScreenHeight - 110) . " NoActivate")
    SetTimer(AgentHudHide, -6000)
}

AgentHudHide() {
    global AgentHudGui
    try AgentHudGui.Hide()
}

AgentAskApproval(id, summary) {
    global AgentShownApprovals
    if AgentShownApprovals.Has(id)
        return
    AgentShownApprovals[id] := true
    res := MsgBox(summary, "تأیید ایجنت", "YesNo Icon?")
    resp := AgentHttp("POST", "/ahk/decision", id . (res = "Yes" ? " 1" : " 0"))
    if !InStr(resp, '"changed": true')
        Flash("این درخواست تأیید منقضی شده بود.")
    AgentShownApprovals.Delete(id)
}

AgentSend(text) {
    return AgentHttp("POST", "/ahk/cmd", text)
}

AgentHandle(c, cmd) {
    if (c = "توقف ایجنت") {
        AgentSend("stop")
        Flash("⛔ فرمان توقف ارسال شد.")
        return true
    }
    if (c = "ادامه ایجنت") {
        AgentSend("clear_stop")
        Flash("✅ توقف اضطراری برداشته شد.")
        return true
    }
    if (c = "بستن مرورگر ایجنت") {
        AgentSend("close_browser")
        return true
    }
    if RegExMatch(cmd, "i)^ایجنت\s+(.+)$", &m) {
        AgentSend("takeover`t" . m[1])
        Flash("🤖 دستور ارسال شد: " . m[1])
        return true
    }
    if RegExMatch(cmd, "i)^مأموریت\s+(.+)$", &m) {
        AgentSend("run_mission`t" . m[1])
        Flash("🤖 مأموریت در صف: " . m[1])
        return true
    }
    if RegExMatch(cmd, "i)^پایان یادگیری\s*(.*)$", &m) {
        name := Trim(m[1])
        AgentSend("learn_stop`t" . (name != "" ? name : "macro"))
        Flash("💾 ضبط پایان یافت.")
        return true
    }
    if RegExMatch(cmd, "i)^یادگیری\s*(.*)$", &m) {
        AgentSend("learn_start`t" . Trim(m[1]))
        Flash("🎬 ضبط شروع شد؛ در مرورگر ایجنت کار کن.")
        return true
    }
    if RegExMatch(cmd, "i)^ماکرو\s+(\S+)\s*(.*)$", &m) {
        AgentSend("replay`t" . m[1] . "`t" . StrReplace(Trim(m[2]), " ", ";"))
        Flash("🔁 بازپخش ماکرو: " . m[1])
        return true
    }
    if RegExMatch(cmd, "i)^کد بنویس\s+(.+)$", &m) {
        task := StrReplace(m[1], '"', "'")
        Run('python -m agent.ai ask --task "' . task . '"', "D:\Python")
        return true
    }
    return false
}
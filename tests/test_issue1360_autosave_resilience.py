"""Regression coverage for #1360-follow-up: the appearance autosave must never
fail SILENTLY. Root cause of the user's "every checkbox reverts on reload" was
that the browser never sent a single /api/settings POST — the autosave path died
before the fetch (a throw in the payload builder, outside any try/catch, plus a
console-only catch that hid failures). Harden: (1) payload capture can never throw
— fall back to a minimal payload; (2) retry transient POST failures; (3) surface
failure visibly (status 'failed' + toast) instead of console-only.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_minimal_payload_fallback_declared():
    src = _read("static/panels.js")
    assert "function _minimalAppearancePayload()" in src, (
        "a throw-proof minimal payload builder must exist so capture can never block a save"
    )
    # it must reference the core appearance checkbox ids
    for cid in ("settingsAutoScrollFollow", "settingsRenderUserMarkdown",
                "settingsSessionJumpButtons", "settingsProjectQuickCreate"):
        assert cid in src


def test_scheduler_guards_payload_capture():
    src = _read("static/panels.js")
    # _scheduleAppearanceAutosave must wrap the payload builder in try/catch and
    # fall back to _minimalAppearancePayload on throw (no silent kill, no missing POST).
    sched = src.split("function _scheduleAppearanceAutosave(){", 1)[1].split(
        "function _autosaveAppearanceSettings", 1)[0]
    assert "try{" in sched and "_minimalAppearancePayload()" in sched, (
        "scheduler must try/catch payload capture and fall back to minimal payload"
    )


def test_autosave_retries_then_surfaces_failure():
    src = _read("static/panels.js")
    auto = src.split("async function _autosaveAppearanceSettings(payload", 1)[1].split(
        "function _retryAppearanceAutosave()", 1)[0]
    assert "MAX_ATTEMPTS" in auto, "autosave must retry transient POST failures"
    assert "if(attempt < MAX_ATTEMPTS)" in auto, "must retry before giving up"
    assert "showToast(t('settings_autosave_failed')" in auto, (
        "final failure must be surfaced visibly (toast), not console-only"
    )
    assert "_setAppearanceAutosaveStatus('failed')" in auto


def test_retry_handler_uses_safe_capture():
    src = _read("static/panels.js")
    retry = src.split("function _retryAppearanceAutosave(){", 1)[1].split(
        "function _setPreferencesAutosaveStatus", 1)[0]
    assert "_minimalAppearancePayload()" in retry, (
        "retry must also fall back to minimal payload if the builder throws"
    )


def test_payload_capture_throw_never_blocks_post_node():
    """End-to-end: if _appearancePayloadFromUi throws, the scheduler still POSTs
    via the minimal payload (the exact failure mode that produced zero POSTs)."""
    import subprocess

    assert __import__("shutil").which("node"), "node required"
    js = r"""
const fs=require('fs');const vm=require('vm');
const els={};
['settingsAutoScrollFollow','settingsRenderUserMarkdown','settingsLargeTextPasteAsAttachment',
 'settingsSessionJumpButtons','settingsSessionEndlessScroll','settingsProjectQuickCreateButtons',
 'settingsShowTitlebarProfile','settingsTheme','settingsSkin','settingsFontSize'].forEach(id=>{
  els[id]={id,checked:false,value:'default',style:{},dataset:{},_onchange:null,
    set onchange(fn){this._onchange=fn;},get onchange(){return this._onchange;},
    addEventListener(){},classList:{add(){},remove(){},toggle(){},contains(){return false;}},querySelectorAll(){return [];}};});
const posts=[];
let server={auto_scroll_follow:false};
const sb={console,setTimeout,clearTimeout,setInterval,clearInterval,JSON,Math,Date,RegExp,Promise,Error,
  addEventListener(){},removeEventListener(){},dispatchEvent(){},window:{},
  document:{getElementById:()=>null,querySelector:()=>null,querySelectorAll:()=>[],documentElement:{dataset:{}},addEventListener(){}},
  localStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  fetch:async(u,opts)=>{const b=opts&&opts.body?JSON.parse(opts.body):null;
    if(String(u).includes('/api/settings')&&(!opts||opts.method==='GET'||!opts.method))return{ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(server))};
    if(String(u).includes('/api/settings')&&opts&&opts.method==='POST'){posts.push(b);for(const[k,v]of Object.entries(b)){if(typeof v==='boolean')server[k]=v;}return{ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(server))};}
    return{ok:true,status:200,json:async()=>({})};}};
sb.window=sb;sb.globalThis=sb;vm.createContext(sb);
sb.$=id=>els[id]||null;
sb.api=async(p,opts={})=>{const url=(typeof p==='string'&&p.startsWith('http'))?p:'http://x/'+String(p).replace(/^\//,'');return sb.fetch(url,opts);};
sb.checkWebUIVersionSkew=()=>{};sb._persistAutoScrollFollow=v=>v;sb._readPersistedAutoScrollFollow=()=>true;
sb._applySessionNavigationPrefs=()=>{};sb._syncThemePicker=()=>{};sb._buildSkinPicker=()=>{};sb._applyFontSize=()=>{};sb._syncFontSizePicker=()=>{};
sb.t=k=>k;sb.showToast=()=>{};sb._setAppearanceAutosaveStatus=()=>{};sb._applyWorklogDetailsExpandedDefault=()=>{};sb.clearMessageRenderCache=()=>{};sb.renderMessages=()=>{};
// Force _appearancePayloadFromUi to throw in the REAL code path by making a helper it calls throw:
sb._structuredCodeViewFromUi=()=>{throw new Error('boom');};
vm.runInContext(fs.readFileSync('/root/hermes-webui/static/panels.js','utf8'),sb,{filename:'panels.js'});
(async()=>{
  await sb.loadSettingsPanel();
  els.settingsAutoScrollFollow.checked=true; els.settingsAutoScrollFollow.onchange&&els.settingsAutoScrollFollow.onchange();
  await new Promise(r=>setTimeout(r,700));
  if(posts.length===0) throw new Error('NO POST — throw in payload builder still blocked the save');
  const p=posts[posts.length-1];
  if(!(p.auto_scroll_follow===true)) throw new Error('minimal payload did not carry toggle: '+JSON.stringify(p));
  console.log('POST-OK minimal='+JSON.stringify(p));
  process.exit(0);
})().catch(e=>{console.log('ERR',e.message);process.exit(1);});
"""
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=ROOT, timeout=40)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "POST-OK" in proc.stdout, proc.stdout

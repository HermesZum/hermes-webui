"""Regression coverage for #1360 — appearance checkboxes must persist across
a settings-panel re-apply (loadSettingsPanel re-runs on every sub-section
switch and resets the DOM checkboxes to the stale, pre-save server value).

Root cause: `_scheduleAppearanceAutosave` snapshotted the WHOLE DOM at schedule
time. Toggle box A, then a panel re-apply resets A's DOM to false, then toggle
box B -> B's debounced save snapshots A as false, overwriting A. After reload
every checkbox the user touched came back unchecked.

The fix captures PER-FIELD intent at each onchange into a module-scoped
`_appearancePending` map; both the debounced autosave (`_appearancePayloadFromUi`)
and the explicit save (`saveSettings`) prefer those captured values over the
live checkbox. A re-apply that resets the DOM can no longer clobber an earlier
toggle, because the save reads the captured intent, not the (reset) DOM.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_appearance_pending_declared():
    src = _read("static/panels.js")
    assert "let _appearancePending = {};" in src, (
        "module-scoped per-field intent map must exist so re-applies cannot clobber toggles"
    )
    assert "function _markAppearanceChanged(" in src, "helper to record per-field intent must exist"


def test_onchange_handlers_capture_per_field_intent():
    src = _read("static/panels.js")
    # Every appearance checkbox onchange must route through _markAppearanceChanged
    # with its settings key, so its intent survives a later DOM re-apply.
    for key in (
        "session_jump_buttons",
        "session_endless_scroll",
        "auto_scroll_follow",
        "render_user_markdown",
        "large_text_paste_as_attachment",
        "project_quick_create_buttons",
    ):
        assert "_markAppearanceChanged('%s', this.checked)" % key in src, (
            "appearance checkbox for %s must capture its intent per-field" % key
        )


def test_payload_builders_prefer_appearance_pending():
    src = _read("static/panels.js")
    # Both payload builders must consult _appearancePending before the live DOM.
    assert src.count("'auto_scroll_follow' in _appearancePending") >= 2, (
        "auto_scroll_follow override must appear in both _appearancePayloadFromUi and saveSettings"
    )
    assert "'render_user_markdown' in _appearancePending" in src
    assert "'large_text_paste_as_attachment' in _appearancePending" in src
    assert "'project_quick_create_buttons' in _appearancePending" in src
    assert "'session_jump_buttons' in _appearancePending" in src
    assert "'session_endless_scroll' in _appearancePending" in src


def test_appearance_pending_cleared_after_save():
    src = _read("static/panels.js")
    # Cleared after the debounced autosave settles AND after the explicit save.
    assert src.count("if(typeof _appearancePending==='object') _appearancePending={};") >= 2, (
        "pending intent must be cleared once the save settles (autosave + explicit save)"
    )


def test_reapply_does_not_clobber_toggle_node():
    """Simulate: toggle A -> re-apply resets DOM -> toggle B -> save. Both must persist."""
    import subprocess

    assert __import__("shutil").which("node"), "node required for this test"
    src = _read("static/panels.js")
    assert "function _markAppearanceChanged(" in src
    # minimal harness: load panels.js, drive the scenario, inspect POST body
    js = r"""
const fs=require('fs');
const vm=require('vm');
const els={};
['settingsAutoScrollFollow','settingsRenderUserMarkdown','settingsLargeTextPasteAsAttachment',
 'settingsSessionJumpButtons','settingsSessionEndlessScroll','settingsProjectQuickCreateButtons',
 'settingsShowTitlebarProfile','settingsTheme','settingsSkin','settingsFontSize'].forEach(id=>{
  els[id]={id,checked:false,value:'',style:{},dataset:{},_onchange:null,
    set onchange(fn){this._onchange=fn;},get onchange(){return this._onchange;},
    addEventListener(){},classList:{add(){},remove(){},toggle(){},contains(){return false;}},querySelectorAll(){return [];}};
});
const posts=[];
let server={auto_scroll_follow:false,render_user_markdown:false,large_text_paste_as_attachment:false,
  session_jump_buttons:false,session_endless_scroll:false,project_quick_create_buttons:false,theme:'dark',skin:'default',font_size:'default'};
const sb={console,setTimeout,clearTimeout,setInterval,clearInterval,JSON,Math,Date,RegExp,Promise,
  addEventListener(){},removeEventListener(){},dispatchEvent(){},window:{},
  document:{getElementById:()=>null,querySelector:()=>null,querySelectorAll:()=>[],documentElement:{dataset:{}},addEventListener(){}},
  localStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  fetch:async(u,opts)=>{const b=opts&&opts.body?JSON.parse(opts.body):null;
    if(String(u).includes('/api/settings')&&(!opts||opts.method==='GET'||!opts.method))return{ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(server))};
    if(String(u).includes('/api/settings')&&opts&&opts.method==='POST'){posts.push(b);for(const[k,v]of Object.entries(b)){if(typeof v==='boolean')server[k]=v;else if(k==='theme'||k==='skin'||k==='font_size')server[k]=v;}return{ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(server))};}
    return{ok:true,status:200,json:async()=>({})};}};
sb.window=sb;sb.globalThis=sb;vm.createContext(sb);
sb.$=id=>els[id]||null;
sb.api=async(p,opts={})=>{const url=(typeof p==='string'&&p.startsWith('http'))?p:'http://x/'+String(p).replace(/^\//,'');return sb.fetch(url,opts);};
sb.checkWebUIVersionSkew=()=>{};sb._persistAutoScrollFollow=v=>v;sb._readPersistedAutoScrollFollow=()=>true;
sb._applySessionNavigationPrefs=()=>{};sb._syncThemePicker=()=>{};sb._buildSkinPicker=()=>{};sb._applyFontSize=()=>{};sb._syncFontSizePicker=()=>{};
sb.t=k=>k;sb.showToast=()=>{};sb._setAppearanceAutosaveStatus=()=>{};sb._applyWorklogDetailsExpandedDefault=()=>{};sb.clearMessageRenderCache=()=>{};sb.renderMessages=()=>{};
vm.runInContext(fs.readFileSync('/root/hermes-webui/static/panels.js','utf8'),sb,{filename:'panels.js'});
(async()=>{
  await sb.loadSettingsPanel();
  els.settingsRenderUserMarkdown.checked=true; els.settingsRenderUserMarkdown.onchange&&els.settingsRenderUserMarkdown.onchange();
  await sb.loadSettingsPanel();  // re-apply resets DOM to stale false
  els.settingsAutoScrollFollow.checked=true; els.settingsAutoScrollFollow.onchange&&els.settingsAutoScrollFollow.onchange();
  await new Promise(r=>setTimeout(r,700));
  const p=posts[posts.length-1];
  if(!(server.render_user_markdown===true&&server.auto_scroll_follow===true)) throw new Error('clobber: '+JSON.stringify({ru:server.render_user_markdown,af:server.auto_scroll_follow}));
  console.log('NO-CLOBBER-OK');
  process.exit(0);
})().catch(e=>{console.log('ERR',e.message);process.exit(1);});
"""
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=ROOT, timeout=40)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "NO-CLOBBER-OK" in proc.stdout, proc.stdout

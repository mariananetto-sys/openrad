#!/usr/bin/env python3
"""Black-box smoke test of the exact public OpenRad Android 2.0.2 debug APK on an Android emulator."""
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path("dist/android-emulator-report")
ROOT.mkdir(parents=True, exist_ok=True)
APP = "com.openrad.mobile.debug"
ACTIVITY = "com.openrad.mobile.MainActivity"
APK_URL = "https://github.com/mariananetto-sys/openrad/releases/download/v2.0.2-android-preview.1/openrad-2.0.2-android-DEBUG-preview.apk"
APK_HASH = "91ce20608516b2747c591fcce0afe82f2d5aead96d5870e27c54810e3a5f758c"
RESULT = {"package": APP, "apk_url": APK_URL, "emulator_api": 34, "screens": [], "failures": [], "tests": []}

def call(*args, timeout=50, check=True, binary=False):
    cmd = list(args)
    cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and cp.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {cp.stderr.decode(errors='replace')[-700:]}")
    return cp.stdout if binary else cp.stdout.decode(errors="replace").strip()

def step(name, func):
    try:
        result = func()
        RESULT["tests"].append({"name": name, "passed": True, "details": str(result)[:500]})
        print(f"PASS: {name} — {result}", flush=True)
        return result
    except Exception as exc:
        RESULT["tests"].append({"name": name, "passed": False, "error": str(exc)})
        RESULT["failures"].append({"test": name, "error": str(exc)})
        print(f"FAIL: {name} — {exc}", flush=True)
        return None

def pid():
    return call("adb", "shell", "pidof", APP, check=False).strip()

def capture(name):
    if not pid():
        raise AssertionError("App process is no longer running")
    time.sleep(2)
    screenshot = ROOT / f"{name}.png"
    screenshot.write_bytes(call("adb", "exec-out", "screencap", "-p", timeout=60, binary=True))
    if screenshot.stat().st_size < 3000:
        raise AssertionError("Screenshot too small")
    hierarchy = None
    for _ in range(4):
        call("adb", "shell", "uiautomator", "dump", "/sdcard/window.xml", check=False)
        xml_str = call("adb", "shell", "cat", "/sdcard/window.xml", check=False)
        try:
            hierarchy = ET.fromstring(xml_str)
            break
        except ET.ParseError:
            time.sleep(2)
    if hierarchy is None:
        raise AssertionError("Cannot retrieve emulator UI hierarchy")
    (ROOT / f"{name}.xml").write_text(xml_str, encoding="utf-8")
    values = [el.attrib.get("text","") for el in hierarchy.iter("node") if el.attrib.get("text","")]
    RESULT["screens"].append({"name":name, "screenshot":screenshot.name, "visible_texts":values[:70], "nodes":len(list(hierarchy.iter("node")))})
    if len(values) < 2:
        raise AssertionError(f"UI contains insufficient text: {values}")
    return hierarchy

def tap(hierarchy,label):
    aliases = {"My networks":["My networks", "Minhas redes"], "Discover":["Discover", "Descobrir"],
               "Auto join":["Auto join", "Entrada automática", "Autoentrar"],
               "Settings":["Settings", "Configurações"]}
    for node in hierarchy.iter("node"):
        text = node.attrib.get("text","")
        desc = node.attrib.get("content-desc","")
        if any(s.casefold() == text.casefold() or s.casefold() == desc.casefold() for s in aliases[label]):
            match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.attrib.get("bounds",""))
            if match:
                x1,y1,x2,y2 = map(int,match.groups())
                call("adb","shell","input","tap",str((x1+x2)//2),str((y1+y2)//2))
                return f"tap {label} at {(x1+x2)//2},{(y1+y2)//2}"
    raise AssertionError(f"Missing tab {label} in emulator UI; seen: {[x.attrib.get('text') for x in hierarchy.iter('node') if x.attrib.get('text')]}")

def main():
    print("Smoke testing exact published debug APK on Android emulator", flush=True)
    RESULT["device"] = step("detect Android emulator", lambda: call("adb","shell","getprop","ro.build.version.release"))
    apk = ROOT / "source-preview.apk"
    def fetch_apk():
        req=urllib.request.Request(APK_URL,headers={"User-Agent":"OpenRad-Emulator-Smoke-Test"})
        with urllib.request.urlopen(req, timeout=120) as response:
            apk.write_bytes(response.read())
        actual=hashlib.sha256(apk.read_bytes()).hexdigest()
        if actual != APK_HASH:
            raise AssertionError(f"APK hash mismatch: {actual}")
        return f"SHA256 verified: {actual}; size={apk.stat().st_size}"
    if not step("download and verify exact 2.0.2 APK", fetch_apk):
        return
    def install():
        out=call("adb","install","-r",str(apk),timeout=120)
        if "Success" not in out: raise AssertionError(out)
        return out
    if not step("install actual APK", install): return
    def check_version():
        dump=call("adb","shell","dumpsys","package",APP)
        if "versionName=2.0.2" not in dump:
            raise AssertionError("Installed package does not report versionName=2.0.2")
        if "versionCode=4" not in dump:
            raise AssertionError("Installed package does not report versionCode=4")
        return "versionName=2.0.2, versionCode=4"
    step("verify installed version",check_version)
    def launch():
        call("adb","shell","input","keyevent","KEYCODE_WAKEUP",check=False)
        call("adb","shell","wm","dismiss-keyguard",check=False)
        out=call("adb","shell","am","start","-W","-n",f"{APP}/{ACTIVITY}",timeout=70)
        time.sleep(8)
        if not pid(): raise AssertionError("App crashed or exited during startup")
        return out[-450:]
    if not step("launch MainActivity without crash",launch): return
    current = step("render initial My networks screen",lambda:capture("01-networks-initial"))
    if current is None: return
    for n,(label,slug) in enumerate([("Discover","discover"),("Auto join","auto-join"),("Settings","settings"),("My networks","networks-return")],start=2):
        if step(f"navigate to {label}",lambda lab=label:tap(current,lab)) is None: continue
        next_ui = step(f"render {label} without crash",lambda sn=f"{n:02d}-{slug}":capture(sn))
        if next_ui is not None: current=next_ui
    def relaunch():
        call("adb","shell","am","force-stop",APP)
        return launch() + "; restart success"
    if step("restart app without crash",relaunch):
        step("render after restart",lambda:capture("06-after-restart"))

try:
    main()
except Exception as exc:
    RESULT["failures"].append({"test":"unexpected","error":str(exc)})
    print(f"ERROR: {exc}",flush=True)
finally:
    try:
        txt=call("adb","logcat","-d","-v","brief",timeout=40,check=False)
        lines=txt.splitlines()
        relevant=[line for line in lines if APP in line or "FATAL EXCEPTION" in line or "AndroidRuntime" in line]
        (ROOT/"logcat_filtered.txt").write_text("\n".join(relevant[-300:]),encoding="utf-8")
        if any("FATAL EXCEPTION" in line for line in relevant):
            RESULT["failures"].append({"test":"AndroidRuntime crash","error":"FATAL EXCEPTION found in logcat"})
    except Exception as exc:
        RESULT["logcat_error"]=str(exc)
    RESULT["passed"]=bool(RESULT["tests"]) and all(t["passed"] for t in RESULT["tests"]) and not RESULT["failures"]
    (ROOT/"report.json").write_text(json.dumps(RESULT,ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["# OpenRad 2.0.2 — Android 14 emulator smoke test","",f"Result: {'PASS' if RESULT['passed'] else 'FAIL'}",
           f"Emulator: Android {RESULT.get('device')} API 34, Google APIs, Pixel 6", "", "## Checks"]
    for test in RESULT["tests"]:
        lines.append(f"- {'PASS' if test['passed'] else 'FAIL'}: {test['name']} — {test.get('details',test.get('error',''))}")
    lines += ["", "## Limitations", "This is a debug preview smoke test, not a production-signed upgrade.", "Emulator checks do not demonstrate live VPN interoperability, end-to-end Radmin connectivity, or physical-device behavior."]
    (ROOT/"summary.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print("\n".join(lines),flush=True)
if not RESULT["passed"]:
    sys.exit(1)

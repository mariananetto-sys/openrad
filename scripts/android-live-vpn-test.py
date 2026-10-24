#!/usr/bin/env python3
"""Test real OpenRad Android APK with live service. Independent of fake/test engines."""
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT=Path("dist/android-live-vpn-report")
ROOT.mkdir(parents=True,exist_ok=True)
APP="com.openrad.mobile.debug"
ACTIVITY="com.openrad.mobile.MainActivity"
NETWORK="Minecraft [Português 16]"
APK_URL="https://github.com/mariananetto-sys/openrad/releases/download/v2.0.2-android-preview.1/openrad-2.0.2-android-DEBUG-preview.apk"
EXPECTED_HASH="91ce20608516b2747c591fcce0afe82f2d5aead96d5870e27c54810e3a5f758c"
R={"apk_sha256_expected":EXPECTED_HASH,"network":NETWORK,"tests":[],"samples":[],"failures":[],"notes":[]}

def run(*args,timeout=45,check=True,binary=False):
    c=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if check and c.returncode:
        raise RuntimeError("Command failed: "+" ".join(args)+": "+c.stderr.decode(errors="replace")[-650:])
    return c.stdout if binary else c.stdout.decode(errors="replace").strip()

def record(name, func, required=False):
    try:
        value=func()
        R["tests"].append({"name":name,"ok":True,"detail":str(value)[:600]})
        print("PASS",name,str(value)[:300],flush=True)
        return value
    except Exception as exc:
        R["tests"].append({"name":name,"ok":False,"error":str(exc),"required":required})
        if required:R["failures"].append({"name":name,"error":str(exc)})
        print("FAIL",name,str(exc)[:400],flush=True)
        return None

def dump_ui():
    run("adb","shell","uiautomator","dump","/sdcard/ui.xml",check=False,timeout=25)
    xml=run("adb","shell","cat","/sdcard/ui.xml",check=False)
    root=ET.fromstring(xml)
    nodes=list(root.iter("node"))
    values=[n.attrib.get("text","") for n in nodes if n.attrib.get("text","")]
    return root,nodes,values,xml

def click(label, partial=False, bottom=False):
    root,nodes,values,xml=dump_ui()
    options=[]
    for n in nodes:
        text=n.attrib.get("text","") or n.attrib.get("content-desc","")
        if (label.casefold() in text.casefold()) if partial else (text.casefold()==label.casefold()):
            bounds=re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",n.attrib.get("bounds",""))
            if bounds:
                x1,y1,x2,y2=map(int,bounds.groups())
                options.append((y1,(x1+x2)//2,(y1+y2)//2))
    if not options:
        raise AssertionError(f"Could not find {label!r} on screen; visible: {values[:45]}")
    y,x,yy=(max(options) if bottom else min(options))
    run("adb","shell","input","tap",str(x),str(yy))
    return f"tap {label} at {x},{yy}"

def capture(name):
    png=ROOT/(name+".png")
    png.write_bytes(run("adb","exec-out","screencap","-p",binary=True,timeout=45))
    root,nodes,values,xml=dump_ui()
    (ROOT/(name+".xml")).write_text(xml,encoding="utf-8")
    R["samples"].append({"name":name,"text":values[:70]})
    print("SCREEN",name," | ".join(values[:12]),flush=True)
    return values

def maybe_permission():
    root,nodes,values,xml=dump_ui()
    visible=" | ".join(values)
    if any(phrase in visible for phrase in ["Connection request","OpenRad wants to set up","Connection Request"]):
        for target in ["OK","Allow","Continue"]:
            try: return click(target)
            except AssertionError: pass
    if any(t in values for t in ["Allow","While using the app"]):
        return click("Allow")
    return None

def wait_connected():
    for i in range(38):
        maybe_permission()
        root,nodes,values,xml=dump_ui()
        line=" | ".join(values)
        print("PHASE",i," | ".join(values[:8]),flush=True)
        if ("You're connected" in values) or ("You’re connected" in values):
            vips=[x for x in values if re.search(r"VPN address:\s*26\.\d+\.\d+\.\d+",x)]
            if not vips:
                raise AssertionError("App reports connected but has no assigned 26.x VPN IP")
            return {"vip":vips[0],"interface_ready":any("VPN interface ready" in v for v in values)}
        if any("Connection needs attention" in x for x in values):
            R["notes"].append("App reports connection error: "+line[:700])
        time.sleep(2)
    raise AssertionError("No live VPN connection within timeout")

def search_public_network():
    click("Discover",bottom=True)
    time.sleep(2)
    click("Search games",partial=True)
    run("adb","shell","input","text","Minecraft")
    run("adb","shell","input","keyevent","KEYCODE_BACK")
    for j in range(20):
        root,nodes,values,xml=dump_ui()
        names=[x for x in values if "Minecraft" in x or "Português" in x]
        print("SEARCH",j,names[:9],flush=True)
        if NETWORK in values:
            capture("03-public-network-found")
            return NETWORK
        if j%5==4:
            run("adb","shell","input","swipe","510","1850","510","650","360")
        time.sleep(3)
    capture("03-public-network-not-found")
    raise AssertionError("Public network not found in first visible search results; network may have changed")

def join_public_network():
    root,nodes,values,xml=dump_ui()
    name_nodes=[n for n in nodes if n.attrib.get("text")==NETWORK]
    if not name_nodes:raise AssertionError("Target network is not visible")
    name_bounds=re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",name_nodes[0].attrib.get("bounds",""))
    name_y=(int(name_bounds.group(2))+int(name_bounds.group(4)))//2
    buttons=[]
    for n in nodes:
        if n.attrib.get("text")=="Join network":
            m=re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",n.attrib.get("bounds",""))
            if m:
                y=(int(m.group(2))+int(m.group(4)))//2
                buttons.append((abs(y-name_y),(int(m.group(1))+int(m.group(3)))//2,y))
    if not buttons:raise AssertionError("Join button for public network not visible")
    _,x,y=min(buttons)
    if abs(y-name_y)>200:raise AssertionError("No Join button near target network")
    run("adb","shell","input","tap",str(x),str(y))
    time.sleep(3)
    for i in range(25):
        click("My networks",bottom=True)
        root,nodes,values,xml=dump_ui()
        if NETWORK in values:
            capture("04-joined-network")
            return NETWORK
        time.sleep(2)
    raise AssertionError("Network join not reflected in My networks")

def find_own_windows_peer():
    # The Windows 2.0.2 instance runs with a separate test identity and no TAP.
    for i in range(22):
        root,nodes,values,xml=dump_ui()
        if any("OpenRad-PR9-LiveTest" in x for x in values):
            capture("05-windows-peer-discovered")
            return "Windows OpenRad-PR9-LiveTest visible"
        run("adb","shell","input","swipe","500","1800","500","800","320")
        time.sleep(2)
    capture("05-peers-last-screen")
    raise AssertionError("Windows test client not visible in peer list; may be below scroll or not connected")

def main():
    record("Android 14 emulator is running",lambda:run("adb","shell","getprop","ro.build.version.release"),True)
    apk=ROOT/"published-openrad-2.0.2-debug.apk"
    def download():
        with urllib.request.urlopen(APK_URL,timeout=130) as resp:apk.write_bytes(resp.read())
        h=hashlib.sha256(apk.read_bytes()).hexdigest()
        assert h==EXPECTED_HASH, f"APK checksum mismatch: {h}"
        return f"exact original APK, sha256={h}"
    if not record("Download and verify published APK",download,True):return
    if not record("Install real APK",lambda:run("adb","install","-r",str(apk),timeout=120),True):return
    if not record("Open Android activity",lambda:run("adb","shell","am","start","-W","-n",f"{APP}/{ACTIVITY}"),True):return
    time.sleep(6)
    capture("01-initial")
    if not record("Request live VPN connection",lambda:click("Connect"),True):return
    time.sleep(2)
    R["live"]=record("Live service connection, assigned VPN IP",wait_connected,True)
    capture("02-after-connect-attempt")
    if not R["live"]:return
    try:
        R["interface"]=run("adb","shell","ip","-o","addr",check=False)
    except Exception as exc:R["interface_error"]=str(exc)
    R["public_search"]=record("Search existing public network from real service",search_public_network,False)
    if R["public_search"]:
        R["join"]=record("Join same network as Windows client",join_public_network,False)
        if R["join"]:
            record("Discover Windows 2.0.2 peer",find_own_windows_peer,False)
    try:
        click("My networks",bottom=True)
        capture("06-final")
    except Exception as exc:R["notes"].append("Final capture: "+str(exc))
    # ADB emulator terminates with job, so the temporary test identity does not persist.

try:
    main()
except Exception as e:
    R["failures"].append({"name":"unexpected","error":str(e)})
    print("UNEXPECTED",repr(e),flush=True)
finally:
    try:
        logs=run("adb","logcat","-d","-v","brief",timeout=40,check=False)
        relevant=[x for x in logs.splitlines() if APP in x or "FATAL EXCEPTION" in x or "AndroidRuntime" in x]
        (ROOT/"logcat.txt").write_text("\n".join(relevant[-450:]),encoding="utf-8")
        R["fatal_exception_count"]=sum("FATAL EXCEPTION" in x for x in relevant)
    except Exception as exc:R["log_error"]=str(exc)
    R["passed_required"]=all(x["ok"] for x in R["tests"] if x.get("required")) and bool(R["tests"]) and not R["failures"]
    (ROOT/"report.json").write_text(json.dumps(R,indent=2,ensure_ascii=False),encoding="utf-8")
    for x in R["tests"]:print("RESULT",x["name"],"PASS" if x["ok"] else "FAIL",flush=True)
    print("REQUIRED_TESTS",R["passed_required"],"FATAL_EXCEPTIONS",R.get("fatal_exception_count"),flush=True)
sys.exit(0 if R["passed_required"] else 1)

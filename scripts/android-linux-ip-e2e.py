#!/usr/bin/env python3
"""Run a real OpenRad 2.0.2 Linux TAP ↔ Android VpnService end-to-end IP test."""
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPORT = Path("dist/android-ip-e2e-results")
REPORT.mkdir(parents=True, exist_ok=True)
CTX = Path(os.environ.get("RUNNER_TEMP","/tmp"))/"openrad-2.0.2-ip-e2e-context.json"
APK_URL="https://github.com/mariananetto-sys/openrad/releases/download/v2.0.2-android-preview.1/openrad-2.0.2-android-DEBUG-preview.apk"
APK_HASH="91ce20608516b2747c591fcce0afe82f2d5aead96d5870e27c54810e3a5f758c"
PACKAGE="com.openrad.mobile.debug"
REPORT_DATA={"date_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()), "steps":[], "packet_tests":[], "failures":[]}

def cmd(*argv,timeout=45,check=True,binary=False):
    p=subprocess.run([str(i) for i in argv],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if check and p.returncode:
        raise RuntimeError("Exit code {}: {}. stderr: {}".format(p.returncode," ".join(map(str,argv)),p.stderr.decode(errors="replace")[-450:]))
    return p.stdout if binary else p.stdout.decode(errors="replace").strip()

def step(name, fn):
    try:
        val=fn()
        print("PASS",name,str(val)[:400],flush=True)
        REPORT_DATA["steps"].append({"name":name,"passed":True,"result":str(val)[:1000]})
        return val
    except Exception as e:
        print("FAIL",name,repr(e),flush=True)
        REPORT_DATA["steps"].append({"name":name,"passed":False,"error":str(e)})
        REPORT_DATA["failures"].append({"name":name,"error":str(e)})
        return None

def cli(ctx, *args, timeout=35):
    response=cmd(ctx["exe"],"--data-dir",ctx["profile"],"--json",*args,timeout=timeout)
    item=json.loads(response)
    if not item.get("ok"):
        raise RuntimeError(f"OpenRad CLI {args[0]}: {item.get('message')}")
    return item

def state(ctx):
    return cli(ctx,"status")["data"]

def run_prepare():
    directory=Path("dist/e2e-binary/openrad-v2.0.2-linux-x86_64").resolve()
    exe=directory/"openrad"
    profile=Path(os.environ["RUNNER_TEMP"])/"openrad-e2e-20261024"
    context={"exe":str(exe),"profile":str(profile),"name":"OpenRadE2E-"+secrets.token_hex(4),
             "password":secrets.token_hex(15),"linux_name":"OpenRad-Linux-E2E"}
    profile.mkdir(mode=0o700,parents=True,exist_ok=True)
    # Write context first so post-job cleanup will still know the test profile.
    CTX.write_text(json.dumps(context),encoding="utf-8")
    CTX.chmod(0o600)
    print("Test Linux binary: ",cmd(exe,"--version"),flush=True)
    print("Linux TUN: ",cmd("ls","-l","/dev/net/tun"),flush=True)
    print("Sudo non-interactive: ",cmd("sudo","-n","true"),flush=True)
    print("Identity:",cli(context,"init","--node-name",context["linux_name"]).get("message"),flush=True)
    print("Force relay:",cli(context,"force-relay","true").get("message"),flush=True)
    print("Start:",cli(context,"start").get("message"),flush=True)
    for i in range(32):
        s=state(context)
        if s.get("phase")=="connected" and s.get("snapshot") and s["snapshot"].get("interface_ready"):
            context["linux_vip"]=s["snapshot"]["vip"]
            break
        if i%5==0:
            print("Linux phase:",s.get("phase"),"tap:",(s.get("snapshot") or {}).get("interface_ready"),"error:",(s.get("snapshot") or {}).get("interface_error"),flush=True)
        time.sleep(2)
    else:
        raise RuntimeError("Linux 2.0.2 didn't establish live connection with functioning TAP; last status="+str(s)[:700])
    print("Linux TAP",cmd("ip","-4","-o","addr","show","dev","radminvpn0"),flush=True)
    pwfile=profile/"password.txt"
    pwfile.write_text(context["password"],encoding="utf-8")
    pwfile.chmod(0o600)
    print("Private test network:",context["name"],cli(context,"create",context["name"],"--password-file",str(pwfile)).get("message"),flush=True)
    CTX.write_text(json.dumps(context),encoding="utf-8")
    print("PREPARED",{"linux_vip":context["linux_vip"],"network":context["name"],"tap":True},flush=True)

def ui():
    cmd("adb","shell","uiautomator","dump","/sdcard/e2e-ui.xml",timeout=30,check=False)
    raw=cmd("adb","shell","cat","/sdcard/e2e-ui.xml")
    tree=ET.fromstring(raw)
    nodes=list(tree.iter("node"))
    texts=[n.attrib.get("text","") for n in nodes if n.attrib.get("text","")]
    return nodes,texts,raw

def midpoint(node):
    m=re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",node.attrib.get("bounds",""))
    if m is None:return None
    a,b,c,d=map(int,m.groups())
    return (a+c)//2,(b+d)//2

def tap_text(text, bottom=False):
    nodes,vals,raw=ui()
    choices=[midpoint(n) for n in nodes if n.attrib.get("text","")==text or n.attrib.get("content-desc","")==text]
    choices=[c for c in choices if c]
    if not choices:raise AssertionError(f"Not visible: {text!r}, UI: {vals[:36]}")
    x,y=sorted(choices,key=lambda p:p[1],reverse=bottom)[0]
    cmd("adb","shell","input","tap",str(x),str(y))
    return (text,x,y)

def screenshot(name):
    path=REPORT/(name+".png")
    path.write_bytes(cmd("adb","exec-out","screencap","-p",binary=True,timeout=45))
    nodes,vals,raw=ui()
    (REPORT/(name+".xml")).write_text(raw,encoding="utf-8")
    REPORT_DATA.setdefault("screens",[]).append({"name":name,"texts":vals[:55]})
    print("SCREEN",name," | ".join(vals[:18]),flush=True)
    return vals

def vpn_permission():
    nodes,vals,raw=ui()
    if "OK" in vals and any("Connection request" in v for v in vals):
        return tap_text("OK")
    if "Allow" in vals and any("OpenRad" in v for v in vals):
        return tap_text("Allow")
    return None

def wait_android_online():
    for i in range(38):
        vpn_permission()
        nodes,vals,raw=ui()
        vips=[v.split(": ",1)[1] for v in vals if re.fullmatch(r"VPN address: 26\.\d+\.\d+\.\d+",v)]
        if any(v in vals for v in ("You're connected","You’re connected")) and vips:
            if not any("VPN interface ready" in v for v in vals):
                time.sleep(2)
                continue
            return vips[0]
        if i%4==0: print("ANDROID CONNECT",i," | ".join(vals[:10]),flush=True)
        time.sleep(2)
    raise RuntimeError("Android live VpnService did not become ready with assigned 26.x VPN address")

def join_private(ctx):
    tap_text("Join private network")
    time.sleep(1)
    nodes,vals,raw=ui()
    edits=[n for n in nodes if "EditText" in n.attrib.get("class","")]
    if len(edits)<2:
        screenshot("03-private-dialog-error")
        raise RuntimeError(f"Missing Android network name/password text inputs (found {len(edits)})")
    # The soft keyboard moves the dialog, so never reuse input coordinates.
    for index,value in enumerate((ctx["name"],ctx["password"])):
        cmd("adb","shell","input","keyevent","KEYCODE_BACK") if index else None
        nodes,vals,raw=ui()
        edits=[n for n in nodes if "EditText" in n.attrib.get("class","")]
        if len(edits)<2:
            raise RuntimeError("Input fields disappeared from join dialog")
        xy=midpoint(edits[index])
        cmd("adb","shell","input","tap",str(xy[0]),str(xy[1]))
        cmd("adb","shell","input","text",value)
        if index==0:
            # Check that text landed in the network field before proceeding.
            nodes,vals,raw=ui()
            edits=[n for n in nodes if "EditText" in n.attrib.get("class","")]
            if ctx["name"] not in edits[0].attrib.get("text",""):
                raise AssertionError("Network name did not enter the first field")
    cmd("adb","shell","input","keyevent","KEYCODE_BACK")
    time.sleep(1)
    screenshot("03-private-network-input")
    tap_text("Join network")
    for i in range(30):
        nodes,vals,raw=ui()
        if ctx["name"] in vals:
            screenshot("04-private-network-joined")
            return ctx["name"]
        if i%5==0:print("ANDROID JOIN",i," | ".join(vals[:13]),flush=True)
        time.sleep(2)
    screenshot("04-private-network-not-joined")
    raise RuntimeError("Android didn't show the private network after joining")

def configure_android_force_relay():
    tap_text("Settings",bottom=True)
    time.sleep(1)
    nodes,vals,raw=ui()
    labels=[n for n in nodes if n.attrib.get("text")=="Force relay only"]
    if not labels:
        raise RuntimeError("Force relay setting is not visible")
    label_xy=midpoint(labels[0])
    toggles=[]
    for n in nodes:
        if n.attrib.get("checkable")=="true" and n.attrib.get("clickable")=="true":
            xy=midpoint(n)
            if xy:
                toggles.append((abs(xy[1]-label_xy[1]),xy,n.attrib.get("checked")))
    if not toggles:
        raise RuntimeError("Could not find force-relay switch")
    dist,xy,checked=min(toggles,key=lambda t:t[0])
    if dist>90:raise RuntimeError(f"Switch too far from Force relay label: {dist} pixels")
    if checked!="true":
        cmd("adb","shell","input","tap",str(xy[0]),str(xy[1]))
    screenshot("01-relay-setting-draft")
    for i in range(12):
        nodes,vals,raw=ui()
        if "Save preferences" in vals:
            tap_text("Save preferences")
            time.sleep(2)
            break
        cmd("adb","shell","input","swipe","530","1770","530","470","280")
        time.sleep(0.5)
    else:raise RuntimeError("Save preferences button not reached")
    tap_text("My networks",bottom=True)
    time.sleep(1)
    return "Force relay only enabled and settings saved"

def peers_linux(ctx):
    return (state(ctx).get("snapshot") or {}).get("peers") or {}

def find_android_peer_linux(ctx):
    for i in range(42):
        ps=peers_linux(ctx)
        for key,entry in ps.items():
            p=entry.get("peer") or {}
            if p.get("name")=="openrad-android":
                print("LINUX SEES ANDROID:",key,entry.get("status"),entry.get("transport"),p.get("vip"),flush=True)
                if entry.get("status")=="Connected" and entry.get("transport"):
                    return {"rid":str(key),"vip":p.get("vip"),"transport":entry.get("transport")}
        if i in (10,24):
            try:
                print("RETRY LINUX PEERS:",cli(ctx,"retry-peers").get("message"),flush=True)
            except Exception as exc:print("RETRY ISSUE:",str(exc)[:150],flush=True)
        if i%6==0:
            print("LINUX PEERS",i, [(v.get("peer") or {}).get("name") for v in ps.values()],flush=True)
        time.sleep(2)
    raise RuntimeError("No authenticated Linux→Android peer connection after timeout")

def ip_ping(label, args, expected):
    p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
    out=p.stdout.decode(errors="replace")+"\n"+p.stderr.decode(errors="replace")
    ok=(p.returncode==0 and "bytes from" in out and (" 0% packet loss" in out or "0% packet loss" in out))
    # adb ping sometimes emits 'bytes from' lowercase or English server '64 bytes from'.
    result={"name":label,"success":ok,"returncode":p.returncode,"output":out[-1800:],"expected_destination":expected}
    REPORT_DATA["packet_tests"].append(result)
    print("PACKET",label,"PASS" if ok else "FAIL",out[-700:],flush=True)
    return ok

def run_android():
    ctx=json.loads(CTX.read_text(encoding="utf-8"))
    REPORT_DATA["linux_vip"]=ctx["linux_vip"]
    REPORT_DATA["ephemeral_network_name"]=ctx["name"]
    # Never save private test password to report.
    apk=Path(os.environ["RUNNER_TEMP"])/"openrad-e2e.apk"
    locally_built=os.environ.get("OPENRAD_E2E_APK")
    if locally_built:
        import shutil
        shutil.copyfile(locally_built,apk)
        REPORT_DATA["apk_source_commit"]=os.environ.get("GITHUB_SHA")
        REPORT_DATA["apk_build_type"]="local CI debug build from tested source"
    else:
        with urllib.request.urlopen(APK_URL,timeout=120) as resp:apk.write_bytes(resp.read())
    h=hashlib.sha256(apk.read_bytes()).hexdigest()
    if not locally_built and h!=APK_HASH: raise AssertionError(f"Published APK hash mismatch: {h}")
    REPORT_DATA["apk_sha256"]=h
    step("Install exact signed-debug preview APK",lambda:cmd("adb","install","-r",str(apk),timeout=120))
    step("Launch OpenRad Android",lambda:cmd("adb","shell","am","start","-W","-n",PACKAGE+"/com.openrad.mobile.MainActivity"))
    time.sleep(5)
    screenshot("01-idle")
    if not step("Force relay on Android",configure_android_force_relay):
        raise RuntimeError("Cannot apply relay-only policy to Android")
    step("Ask Android for VPN connection",lambda:tap_text("Connect"))
    android_vip=step("Start real Android VpnService and obtain VPN IP",wait_android_online)
    screenshot("02-android-vpn-ready")
    if not android_vip:raise RuntimeError("Android VPN startup failed")
    REPORT_DATA["android_vip"]=android_vip
    step("Join ephemeral private network from Android UI",lambda:join_private(ctx))
    peer=step("Establish authenticated Linux ↔ Android peer transport",lambda:find_android_peer_linux(ctx))
    if not peer:
        screenshot("05-peer-not-authenticated")
        raise RuntimeError("Linux/Android failed to establish authenticated peer transport")
    REPORT_DATA["authenticated_peer"]=peer
    REPORT_DATA["linux_ip_before"]=state(ctx).get("snapshot",{}).get("traffic")
    screenshot("05-peer-connected")
    # Try real ICMP/IP packets both directions through both VPN interfaces.
    up=ip_ping("Linux TAP → Android VpnService ICMP",["ping","-I","radminvpn0","-c","3","-W","4",android_vip],android_vip)
    down=ip_ping("Android VpnService → Linux TAP ICMP",["adb","shell","ping","-c","3","-W","4",ctx["linux_vip"]],ctx["linux_vip"])
    REPORT_DATA["linux_ip_after"]=state(ctx).get("snapshot",{}).get("traffic")
    screenshot("06-packet-test-final")
    REPORT_DATA["ip_end_to_end_verified"]=up or down
    if not REPORT_DATA["ip_end_to_end_verified"]:
        raise RuntimeError("Peer auth succeeded but ICMP packets between Linux TAP and Android VpnService were not confirmed")
    print("END_TO_END VERIFIED",{"linux_vip":ctx["linux_vip"],"android_vip":android_vip,"linux_to_android":up,"android_to_linux":down},flush=True)

def run_cleanup():
    if not CTX.exists():
        print("No ephemeral context for cleanup",flush=True)
        return
    ctx=json.loads(CTX.read_text(encoding="utf-8"))
    try:
        x=cli(ctx,"delete",ctx["name"],"--yes",timeout=12)
        print("Removed private test network:",x.get("message"),flush=True)
    except Exception as e:print("Test network cleanup warning:",str(e),flush=True)
    try:
        x=cli(ctx,"stop",timeout=15)
        print("Stopped Linux test VPN:",x.get("message"),flush=True)
    except Exception as e:print("VPN cleanup warning:",str(e),flush=True)
    CTX.unlink(missing_ok=True)

if __name__=="__main__":
    mode=sys.argv[1]
    if mode=="cleanup":
        run_cleanup()
    elif mode=="prepare":
        run_prepare()
    elif mode=="android":
        try:
            run_android()
        except Exception as e:
            REPORT_DATA["failures"].append({"error":str(e)})
            print("E2E TEST FAILED",repr(e),flush=True)
        finally:
            try:
                ctx=json.loads(CTX.read_text(encoding="utf-8"))
                REPORT_DATA["linux_status_final"]=state(ctx)
                # No private identity or password in status snapshot
            except Exception as e: REPORT_DATA["status_error"]=str(e)
            try:
                raw=cmd("adb","logcat","-d","-v","brief",timeout=35,check=False)
                log=[s for s in raw.splitlines() if PACKAGE in s or "FATAL EXCEPTION" in s]
                (REPORT/"android-logcat.txt").write_text("\n".join(log[-350:]),encoding="utf-8")
                REPORT_DATA["fatal_exception_count"]=sum("FATAL EXCEPTION" in x for x in log)
            except Exception as e:REPORT_DATA["logcat_error"]=str(e)
            # Keep only diagnostic event categories and safe transport metadata.
            try:
                from collections import Counter
                profile=Path(ctx["profile"])
                paths=sorted((profile/"diagnostics").glob("connection*.jsonl"))
                records=[]
                for path in paths:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            record=json.loads(line)
                            kind=record.get("event","")
                            if kind in ("peer_connecting","peer_connected","peer_start_failed","peer_retry_scheduled","incoming_record_rejected","peer_cancelled","membership_updated","control_health","control_closed","session_health","server_disconnect","session_attached","control_command_queue_full"):
                                safe={"event":kind,"elapsed_ms":record.get("elapsed_ms")}
                                fields=record.get("details") or {}
                                safe["details"]={key:fields[key] for key in ("rid","incoming","error","peers","eligible","operation","server","status","reason","heartbeats_sent","max_heartbeat_late_ms") if key in fields}
                                records.append(safe)
                        except (ValueError,TypeError):
                            continue
                (REPORT/"linux-diagnostics-safe.json").write_text(json.dumps(records[-500:],indent=2),encoding="utf-8")
                REPORT_DATA["linux_diagnostic_counts"]=dict(Counter(item["event"] for item in records))
                print("LINUX DIAGNOSTICS",REPORT_DATA["linux_diagnostic_counts"],flush=True)
            except Exception as exc:
                REPORT_DATA["linux_diagnostic_error"]=str(exc)
            (REPORT/"report.json").write_text(json.dumps(REPORT_DATA,ensure_ascii=False,indent=2),encoding="utf-8")
            print("FINAL RESULT:",REPORT_DATA.get("ip_end_to_end_verified",False),flush=True)
            if not REPORT_DATA.get("ip_end_to_end_verified",False):
                sys.exit(1)
    else:raise SystemExit("Usage: prepare|android|cleanup")

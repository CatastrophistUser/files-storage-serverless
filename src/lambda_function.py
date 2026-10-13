import base64
import json
import logging
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import quote

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 6
CODE_RE = re.compile("^[" + ALPHABET + "]{" + str(CODE_LENGTH) + "}$")
MAX_FILENAME = 100
POST_EXPIRES = 300

_s3: Any = None


def s3() -> Any:
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name=os.environ.get("AWS_REGION", "ap-south-1"),
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3


def bucket() -> str:
    return os.environ["BUCKET_NAME"]


def expiry_seconds() -> int:
    return int(os.environ["EXPIRY_SECONDS"])


def max_upload_bytes() -> int:
    return int(os.environ["MAX_UPLOAD_BYTES"])


def presign_get_seconds() -> int:
    return int(os.environ.get("PRESIGN_GET_SECONDS", "300"))


def generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def is_valid_code(code: str) -> bool:
    return bool(CODE_RE.match(code))


def sanitize_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"[^\w.\- ()]", "_", name, flags=re.UNICODE).strip(" .")
    if len(name) > MAX_FILENAME:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            name = stem[: MAX_FILENAME - len(ext) - 1] + "." + ext
        else:
            name = name[:MAX_FILENAME]
    return name


def is_expired(last_modified: Any, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    return current - last_modified.timestamp() >= expiry_seconds()


def json_response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def html_response() -> dict[str, Any]:
    config = json.dumps({"max_bytes": max_upload_bytes(), "expiry_seconds": expiry_seconds()})
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
        "body": PAGE.replace("__CONFIG__", config),
    }


def parse_body(event: dict[str, Any]) -> dict[str, Any] | None:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    if len(raw) > 4096:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def handle_upload(event: dict[str, Any]) -> dict[str, Any]:
    data = parse_body(event)
    if data is None:
        return json_response(400, {"error": "Invalid request body."})
    filename = data.get("filename")
    if not isinstance(filename, str):
        return json_response(400, {"error": "A filename is required."})
    clean = sanitize_filename(filename)
    if not clean:
        return json_response(400, {"error": "That filename is not usable."})
    limit = max_upload_bytes()
    size = data.get("size")
    if size is not None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            return json_response(400, {"error": "Invalid file size."})
        if size > limit:
            return json_response(413, {"error": "File is too large."})
    code = generate_code()
    post = s3().generate_presigned_post(
        Bucket=bucket(),
        Key=f"{code}/{clean}",
        Conditions=[["content-length-range", 1, limit]],
        ExpiresIn=POST_EXPIRES,
    )
    logger.info("upload_presigned")
    return json_response(
        200,
        {
            "code": code,
            "url": post["url"],
            "fields": post["fields"],
            "max_bytes": limit,
            "expires_in": expiry_seconds(),
        },
    )


def handle_download(code: str) -> dict[str, Any]:
    code = code.upper()
    if not is_valid_code(code):
        return json_response(404, {"error": "Not found."})
    listing = s3().list_objects_v2(Bucket=bucket(), Prefix=f"{code}/", MaxKeys=1)
    contents = listing.get("Contents") or []
    if not contents or is_expired(contents[0]["LastModified"]):
        return json_response(404, {"error": "Not found or expired."})
    key = contents[0]["Key"]
    filename = key.split("/", 1)[1]
    url = s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket(),
            "Key": key,
            "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
        ExpiresIn=presign_get_seconds(),
    )
    logger.info("download_redirect")
    return {
        "statusCode": 302,
        "headers": {"Location": url, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        "body": "",
    }


def handle_cleanup() -> dict[str, Any]:
    deleted = 0
    scanned = 0
    pending: list[dict[str, str]] = []
    paginator = s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket()):
        for obj in page.get("Contents", []):
            scanned += 1
            if is_expired(obj["LastModified"]):
                pending.append({"Key": obj["Key"]})
            if len(pending) == 1000:
                deleted += delete_batch(pending)
                pending = []
    if pending:
        deleted += delete_batch(pending)
    logger.info("cleanup scanned=%d deleted=%d", scanned, deleted)
    return {"scanned": scanned, "deleted": deleted}


def delete_batch(keys: list[dict[str, str]]) -> int:
    result = s3().delete_objects(Bucket=bucket(), Delete={"Objects": keys, "Quiet": True})
    return len(keys) - len(result.get("Errors", []))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("task") == "cleanup":
        return handle_cleanup()
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "")
    path = event.get("rawPath", "/")
    if path == "/" and method == "GET":
        return html_response()
    if path == "/upload":
        if method == "POST":
            return handle_upload(event)
        return json_response(405, {"error": "Method not allowed."})
    if method == "GET" and path.count("/") == 1 and len(path) > 1:
        return handle_download(path[1:])
    return json_response(404, {"error": "Not found."})


PAGE = r'''<!doctype html>
<html lang="en" style="color-scheme:dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>TempShare</title>
<style>
:root{--background:240 10% 3.9%;--foreground:0 0% 98%;--card:240 10% 3.9%;--muted:240 3.7% 15.9%;--muted-foreground:240 5% 64.9%;--border:240 3.7% 15.9%;--input:240 3.7% 15.9%;--primary:0 0% 98%;--primary-foreground:240 5.9% 10%;--accent:240 3.7% 15.9%;--destructive:0 62.8% 60%;--ring:240 4.9% 83.9%;--radius:.5rem}
*{box-sizing:border-box;border-color:hsl(var(--border))}
html,body{height:100%}
body{margin:0;overflow:hidden;background:hsl(var(--background));color:hsl(var(--foreground));font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}
.float{position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:10;display:flex;gap:4px;padding:4px;border:1px solid hsl(var(--border));border-radius:99px;background:hsl(var(--card)/.8);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);box-shadow:0 8px 24px rgb(0 0 0/.4)}
.stage{position:fixed;inset:0;padding:72px 12px 12px}
.zone{width:100%;height:100%;display:flex;overflow:auto;border:1px dashed hsl(var(--input));border-radius:16px;padding:24px 16px;transition:background-color .15s,border-color .15s}
.zone.idle{cursor:pointer}
.zone.idle:hover,.zone.over{background:hsl(var(--muted)/.5);border-color:hsl(var(--muted-foreground)/.6)}
.zone:focus-visible{outline:none;box-shadow:0 0 0 2px hsl(var(--background)),0 0 0 4px hsl(var(--ring))}
.inner{margin:auto;width:100%;max-width:400px;display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center}
.drop-ic{width:56px;height:56px;border-radius:99px;background:hsl(var(--muted));display:inline-flex;align-items:center;justify-content:center}
.drop-ic svg{width:26px;height:26px}
.big{font-size:1.125rem;font-weight:500}
.hint{color:hsl(var(--muted-foreground));font-size:.8125rem}
.fname{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.bar{width:100%;height:8px;background:hsl(var(--primary)/.2);border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:hsl(var(--primary));transition:width .1s}
.msg{font-size:.8125rem;color:hsl(var(--muted-foreground));min-height:1.25em}
.msg.err{color:hsl(var(--destructive))}
.msg:empty{display:none}
button{font:inherit;font-size:.875rem;font-weight:500;height:36px;padding:0 16px;border-radius:calc(var(--radius) - 2px);cursor:pointer;display:inline-flex;align-items:center;justify-content:center;white-space:nowrap;border:1px solid hsl(var(--input));background:hsl(var(--background));color:hsl(var(--foreground));box-shadow:0 1px 2px 0 rgb(0 0 0/.05);transition:background-color .15s,opacity .15s}
button:hover{background:hsl(var(--accent))}
button.primary{background:hsl(var(--primary));border-color:hsl(var(--primary));color:hsl(var(--primary-foreground))}
button.primary:hover{background:hsl(var(--primary)/.9)}
button:focus-visible,input:focus-visible{outline:none;box-shadow:0 0 0 2px hsl(var(--background)),0 0 0 4px hsl(var(--ring))}
button:disabled{opacity:.5;cursor:not-allowed;pointer-events:none}
button.tab{height:32px;padding:0 20px;border:0;box-shadow:none;background:transparent;color:hsl(var(--muted-foreground));border-radius:99px}
button.tab:hover{background:transparent;color:hsl(var(--foreground))}
button.tab[aria-selected=true]{background:hsl(var(--muted));color:hsl(var(--foreground))}
input[type=text]{font:inherit;font-size:.875rem;height:36px;width:100%;padding:0 12px;border-radius:calc(var(--radius) - 2px);border:1px solid hsl(var(--input));background:transparent;color:hsl(var(--foreground));box-shadow:0 1px 2px 0 rgb(0 0 0/.05)}
input::placeholder{color:hsl(var(--muted-foreground))}
#code-in{height:48px;font-size:1.25rem;text-align:center;text-transform:uppercase;letter-spacing:.3em;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
#code-in::placeholder{letter-spacing:normal;font-family:inherit;font-size:.875rem}
.row{display:flex;gap:8px;align-items:center;width:100%}
.row>*{min-width:0}
.grow{flex:1}
.slots{display:flex;justify-content:center;gap:8px;user-select:all}
.slot{width:44px;height:52px;display:flex;align-items:center;justify-content:center;border:1px solid hsl(var(--input));border-radius:calc(var(--radius) - 2px);background:hsl(var(--muted)/.4);font:600 1.5rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.qr svg{display:block;width:168px;height:168px;background:#fff;padding:12px;border-radius:var(--radius)}
.badge{display:inline-flex;align-items:center;border:1px solid hsl(var(--border));border-radius:99px;padding:2px 10px;font-size:.75rem;font-weight:600;font-variant-numeric:tabular-nums}
.badge.low{border-color:transparent;background:hsl(var(--destructive));color:#fff}
[hidden]{display:none!important}
@media (max-width:420px){.slot{width:38px;height:46px;font-size:1.25rem}.slots{gap:6px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<nav class="float" role="tablist" aria-label="Mode">
<button class="tab" role="tab" id="t-send" aria-selected="true" aria-controls="p-send">Send</button>
<button class="tab" role="tab" id="t-recv" aria-selected="false" aria-controls="p-recv" tabindex="-1">Receive</button>
</nav>

<div class="stage" id="p-send" role="tabpanel" aria-labelledby="t-send">
<div class="zone idle" id="zone" tabindex="0" role="button" aria-label="Choose a file to upload">
<input type="file" id="file" hidden>
<div class="inner" id="v-idle">
<span class="drop-ic"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/></svg></span>
<span class="big">Drop a file or click to browse</span>
<div class="msg" id="status" role="status" aria-live="polite"></div>
</div>
<div class="inner" id="v-busy" hidden>
<span class="fname" id="fname"></span>
<span class="hint" id="fsize"></span>
<div class="bar"><i id="bar-i"></i></div>
<span class="hint" id="pct" role="status" aria-live="polite">Preparing...</span>
<button id="cancel">Cancel</button>
</div>
<div class="inner" id="v-done" hidden>
<div class="slots" id="code" aria-label="Share code"></div>
<div class="row">
<input type="text" class="grow" id="link" readonly aria-label="Share link">
<button id="copy-link">Copy link</button>
</div>
<div class="qr" id="qr"></div>
<span class="badge" id="timer">Expires in <span id="left"></span></span>
<div class="row"><button class="grow" id="copy-code">Copy code</button><button class="primary grow" id="again">Share another</button></div>
</div>
</div>
</div>

<div class="stage" id="p-recv" role="tabpanel" aria-labelledby="t-recv" hidden>
<div class="zone">
<form class="inner" id="dl-form">
<input type="text" id="code-in" maxlength="6" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="Enter code" aria-label="Share code">
<button class="primary" type="submit" style="width:100%">Download</button>
<div class="msg" id="dl-status" role="status" aria-live="polite"></div>
</form>
</div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js" integrity="sha384-mZT2gIty7ZDdOGkxfP6joZcYdMW1Jvj9dRlfpTmaJAKKXTqzygtB22k7FLe+KZC1" crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<script>
(function(){
var CFG=__CONFIG__;
var $=function(id){return document.getElementById(id)};
var xhr=null,timerId=null;
var zone=$("zone");

function fmtBytes(n){
if(n<1024)return n+" B";
var u=["KB","MB","GB"],i=-1;
do{n/=1024;i++}while(n>=1024&&i<u.length-1);
return n.toFixed(n<10?1:0)+" "+u[i];
}
function fmtDur(s){
var h=Math.floor(s/3600),m=Math.floor(s%3600/60),r=s%60;
if(h)return h+"h "+m+"m";
return m+":"+(r<10?"0":"")+r;
}
function setStatus(el,text,isErr){el.textContent=text;el.className="msg"+(isErr?" err":"")}

function view(name){
["idle","busy","done"].forEach(function(v){$("v-"+v).hidden=v!==name});
zone.className="zone"+(name==="idle"?" idle":"");
zone.tabIndex=name==="idle"?0:-1;
if(name==="idle")zone.setAttribute("role","button");else zone.removeAttribute("role");
}

function pick(f){
setStatus($("status"),"");
if(!f)return;
if(location.protocol!=="https:"&&location.protocol!=="http:"){setStatus($("status"),"Open this page from its web address to upload.",true);return}
if(f.size<1){setStatus($("status"),"That file is empty.",true);return}
if(f.size>CFG.max_bytes){setStatus($("status"),"That file is too large. Limit is "+fmtBytes(CFG.max_bytes)+".",true);return}
upload(f);
}

function fail(text){
xhr=null;
view("idle");
setStatus($("status"),text,true);
}

function upload(f){
$("fname").textContent=f.name;
$("fsize").textContent=fmtBytes(f.size);
$("bar-i").style.width="0";
$("pct").textContent="Preparing...";
view("busy");
fetch("/upload",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({filename:f.name,size:f.size})})
.then(function(r){return r.json().catch(function(){return null}).then(function(b){
if(!r.ok)throw new Error(b&&b.error?b.error:"Something went wrong ("+r.status+").");
return b;
})})
.then(function(p){
var fd=new FormData();
Object.keys(p.fields).forEach(function(k){fd.append(k,p.fields[k])});
fd.append("file",f);
xhr=new XMLHttpRequest();
xhr.open("POST",p.url);
xhr.upload.onprogress=function(e){
if(!e.lengthComputable)return;
var pct=Math.round(e.loaded/e.total*100);
$("bar-i").style.width=pct+"%";
$("pct").textContent="Uploading "+pct+"%";
};
xhr.onload=function(){
if(xhr.status>=200&&xhr.status<300){xhr=null;done(p)}
else if(xhr.status===403||xhr.status===400){fail("Upload was rejected. The file may be too large or the upload link expired.")}
else{fail("Upload failed ("+xhr.status+").")}
};
xhr.onerror=function(){fail("Network error during upload.")};
xhr.onabort=function(){xhr=null;view("idle");setStatus($("status"),"Upload cancelled.")};
xhr.send(fd);
})
.catch(function(e){fail(e.message)});
}

function done(p){
var link=location.origin+"/"+p.code;
var slots=$("code");
slots.textContent="";
p.code.split("").forEach(function(ch){var d=document.createElement("span");d.className="slot";d.textContent=ch;slots.appendChild(d)});
$("link").value=link;
var box=$("qr");
box.textContent="";
try{
var qr=qrcode(0,"M");
qr.addData(link);
qr.make();
box.innerHTML=qr.createSvgTag({cellSize:4,margin:0,scalable:true});
var svg=box.querySelector("svg");
if(svg){svg.setAttribute("role","img");svg.setAttribute("aria-label","QR code for the share link")}
}catch(e){box.textContent=""}
view("done");
startTimer(Date.now()+p.expires_in*1000);
}

function startTimer(end){
clearInterval(timerId);
function tick(){
var s=Math.max(0,Math.round((end-Date.now())/1000));
if(s===0){
clearInterval(timerId);
$("timer").textContent="Expired";
$("timer").className="badge low";
return;
}
$("left").textContent=fmtDur(s);
$("timer").className="badge"+(s<300?" low":"");
}
$("timer").innerHTML="Expires in <span id=\"left\"></span>";
tick();
timerId=setInterval(tick,1000);
}

function copy(text,btn){
var label=btn.textContent;
function ok(){btn.textContent="Copied";setTimeout(function(){btn.textContent=label},1500)}
if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(ok,fallback)}else fallback();
function fallback(){
var t=document.createElement("textarea");
t.value=text;t.style.position="fixed";t.style.opacity="0";
document.body.appendChild(t);t.select();
try{document.execCommand("copy");ok()}catch(e){}
document.body.removeChild(t);
}
}

zone.addEventListener("click",function(){if(zone.classList.contains("idle"))$("file").click()});
zone.addEventListener("keydown",function(e){
if(zone.classList.contains("idle")&&e.target===zone&&(e.key==="Enter"||e.key===" ")){e.preventDefault();$("file").click()}
});
$("file").addEventListener("change",function(){var f=this.files[0];this.value="";pick(f)});
["dragenter","dragover"].forEach(function(e){zone.addEventListener(e,function(ev){ev.preventDefault();if(zone.classList.contains("idle"))zone.classList.add("over")})});
zone.addEventListener("dragleave",function(){zone.classList.remove("over")});
zone.addEventListener("drop",function(ev){
ev.preventDefault();
zone.classList.remove("over");
if(zone.classList.contains("idle"))pick(ev.dataTransfer.files[0]);
});
["dragover","drop"].forEach(function(e){window.addEventListener(e,function(ev){ev.preventDefault()})});

$("cancel").addEventListener("click",function(){if(xhr)xhr.abort()});
$("copy-link").addEventListener("click",function(){copy($("link").value,this)});
$("copy-code").addEventListener("click",function(){copy($("code").textContent,this)});
$("link").addEventListener("focus",function(){this.select()});
$("again").addEventListener("click",function(){
clearInterval(timerId);
setStatus($("status"),"");
view("idle");
});

function tab(which){
var send=which==="send";
$("t-send").setAttribute("aria-selected",send);
$("t-recv").setAttribute("aria-selected",!send);
$("t-send").tabIndex=send?0:-1;
$("t-recv").tabIndex=send?-1:0;
$("p-send").hidden=!send;
$("p-recv").hidden=send;
if(!send)$("code-in").focus();
}
$("t-send").addEventListener("click",function(){tab("send")});
$("t-recv").addEventListener("click",function(){tab("recv")});
[$("t-send"),$("t-recv")].forEach(function(b){b.addEventListener("keydown",function(e){
if(e.key==="ArrowLeft"||e.key==="ArrowRight"){var n=b.id==="t-send"?"recv":"send";tab(n);$("t-"+n).focus();e.preventDefault()}
})});

var ALPHA=/^[A-HJ-NP-Z2-9]{6}$/;
$("code-in").addEventListener("input",function(){this.value=this.value.toUpperCase().replace(/[^A-Z0-9]/g,"")});
$("dl-form").addEventListener("submit",function(ev){
ev.preventDefault();
var c=$("code-in").value.trim().toUpperCase(),st=$("dl-status");
if(!ALPHA.test(c)){setStatus(st,"Enter the 6-character code.",true);return}
setStatus(st,"Looking up...");
fetch("/"+c,{redirect:"manual"}).then(function(r){
if(r.type==="opaqueredirect"||r.status===302){setStatus(st,"Starting download...");location.href="/"+c}
else if(r.status===404){setStatus(st,"No file found for that code. It may have expired.",true)}
else{setStatus(st,"Could not look up that code ("+r.status+").",true)}
}).catch(function(){setStatus(st,"Network error. Try again.",true)});
});
})();
</script>
</body>
</html>
'''

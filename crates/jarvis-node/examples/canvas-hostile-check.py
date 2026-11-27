import asyncio, http.server, socket, subprocess, threading, sys, time, re
from playwright.async_api import async_playwright
REPO="/home/claude/work/jarvisdesktop"
import json
ARGS=[w["additionalBrowserArgs"] for w in json.load(open(f"{REPO}/app/src-tauri/tauri.conf.json"))["app"]["windows"] if w["label"]=="board"][0].split()
SEAL=open(f"{REPO}/crates/jarvis-node/src/canvas_seal.js").read()
POLICY=re.search(r'export const POLICY =\s*"(.*?)";', open(f"{REPO}/app/src/board/canvas.ts").read(), re.S).group(1)
hits=[]
def tcp(port, name):
    s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR,1); s.bind(("127.0.0.1",port)); s.listen(50)
    def run():
        while True:
            c,_=s.accept(); c.settimeout(0.5)
            try: d=c.recv(200)
            except Exception: d=b""
            hits.append(f"{name}: {d[:60]!r}")
            try:
                if port in (80, 47823): c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 28\r\nConnection: close\r\n\r\n<h1 id=squat>SQUATTER</h1>  ")
            except Exception: pass
            c.close()
    threading.Thread(target=run, daemon=True).start()
def udp(port, name):
    s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1",port))
    def run():
        while True:
            d,_=s.recvfrom(2000); hits.append(f"{name}: {len(d)} bytes")
    threading.Thread(target=run, daemon=True).start()
tcp(443,"outside tcp 443"); tcp(80,"outside tcp 80"); tcp(3478,"turn tcp 3478"); udp(3478,"stun udp 3478"); tcp(47823,"other canvas port 47823")
proc=subprocess.Popen([f"{REPO}/target/debug/examples/canvas-hostile","serve"], stdout=subprocess.PIPE, text=True)
URL=proc.stdout.readline().strip(); assert ":47821/" in URL, URL
APP_CSP="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src https://www.youtube.com http://127.0.0.1:47821 http://127.0.0.1:47822 http://127.0.0.1:47823; connect-src ipc: http://ipc.localhost"
def host(sealed):
    meta='<meta http-equiv="Content-Security-Policy" content="frame-src http://127.0.0.1:47821 http://127.0.0.1:47822 http://127.0.0.1:47823">' if sealed else ''
    csp=f' csp="{POLICY}"' if sealed else ''
    return f"""<!doctype html>{meta}<title>Board stand-in</title><button id=b onclick="this.dataset.n=(+this.dataset.n||0)+1">click</button>
<iframe id=f sandbox="allow-scripts" referrerpolicy="no-referrer"{csp} src="{URL}" style="width:900px;height:900px"></iframe>"""
MODE={"sealed":False}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b=host(MODE["sealed"]).encode(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Security-Policy',APP_CSP); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self,*a): pass
threading.Thread(target=http.server.HTTPServer(('127.0.0.1',8766),H).serve_forever, daemon=True).start()
async def run(p, sealed):
    MODE["sealed"]=sealed; hits.clear()
    args=["--no-sandbox","--host-resolver-rules=MAP * 127.0.0.1","--ignore-certificate-errors"]+(ARGS if sealed else [])
    b=await p.chromium.launch(channel="chrome", args=args)
    ctx=await b.new_context(viewport={"width":1000,"height":1000})
    if sealed: await ctx.add_init_script(SEAL)
    pg=await ctx.new_page()
    async def frame():
        await pg.goto("http://127.0.0.1:8766/"); 
        fr=next(f for f in pg.frames if ":4782" in f.url); await fr.wait_for_selector("#verdict[data-done]", timeout=90000); return fr
    fr=await frame()
    rows=await fr.eval_on_selector_all("#out li","els=>els.map(e=>e.textContent)")
    print(f"\n=== {'SEALED (Board arguments, seal script, narrowed frame-src, csp attribute)' if sealed else 'BASELINE (as first committed)'} ===")
    for r in rows:
        if not r.startswith("CONTAINED"): print("  ", r[:140])
    print("  verdict:", await fr.inner_text("#verdict"))
    await pg.wait_for_timeout(3000)
    print("  arrived outside during the automatic attempts:", sorted(set(h.split(':')[0] for h in hits)) or "nothing")
    for button, what in (("#leave","leave"),("#tube","youtube"),("#port","other port")):
        hits.clear(); fr=await frame(); await pg.wait_for_timeout(2500); hits.clear()
        await fr.click(button); await pg.wait_for_timeout(2500); print("     raw:", hits[:3])
        urls=[f.url[:48] for f in pg.frames][1:]
        shown=""
        try: shown=await pg.frames[1].inner_text("body", timeout=1000)
        except Exception: pass
        print(f"  {what}: frame now {urls}; arrived {sorted(set(h.split(':')[0] for h in hits)) or 'nothing'}; squatter page shown: {'SQUATTER' in shown}")
    fr=await frame(); await fr.click("#noise"); await pg.wait_for_timeout(1500)
    print("  after a click:", await fr.inner_text("#noised"))
    fr=await frame()
    try: await fr.click("#flood", timeout=1000, no_wait_after=True)
    except Exception: pass
    await pg.wait_for_timeout(500)
    try: await pg.click("#b", timeout=3000); print("  flood: the stand-in still answers a click")
    except Exception as e: print("  flood: STAND-IN FROZEN", type(e).__name__)
    await b.close()
async def main():
    async with async_playwright() as p:
        await run(p, False); await run(p, True)
try: asyncio.run(main())
finally: proc.kill()

import argparse
import asyncio
import json
import hashlib
import logging
import os
import signal
import sys
import time
from pathlib import Path

try:
    from aiohttp import web
except ImportError:
    print("请先安装 aiohttp: pip install aiohttp")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("stealer-webui")


class StealerWebUIServer:
    def __init__(self, host, port, password, data_dir, release_port=True):
        self.host = host
        self.port = port
        self.password = password
        self.data_dir = Path(data_dir)
        self.release_port = release_port

    def _session_token(self):
        return hashlib.sha256(("stealer_webui:" + self.password).encode("utf-8")).hexdigest()

    def _is_authenticated(self, request):
        return (not self.password) or request.cookies.get("stealer_webui_token") == self._session_token()

    def _safe_part(self, value):
        return bool(value) and value not in {".", ".."} and "/" not in value and "\\" not in value and "\x00" not in value

    def _create_app(self):
        app = web.Application()
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/login.html", self.handle_login)
        app.router.add_post("/api/login", self.handle_api_login)
        app.router.add_get("/api/categories", self.handle_api_categories)
        app.router.add_get("/api/users/{category}", self.handle_api_users)
        app.router.add_get("/api/user/{category}/{user_id}", self.handle_api_user_detail)
        app.router.add_static("/web", Path(__file__).parent / "web")
        return app

    async def handle_index(self, request):
        return web.FileResponse(Path(__file__).parent / "web" / "index.html")

    async def handle_login(self, request):
        raise web.HTTPFound("/")

    async def handle_api_login(self, request):
        data = await request.json()
        if data.get("password") == self.password:
            resp = web.json_response({"success": True})
            resp.set_cookie("stealer_webui_token", self._session_token(), httponly=True, samesite="Lax")
            return resp
        return web.json_response({"success": False, "error": "密码错误"})

    async def handle_api_categories(self, request):
        if not self._is_authenticated(request):
            return web.json_response({"error": "未登录"}, status=401)
        dd = self.data_dir / "data"
        if not dd.exists():
            return web.json_response([])
        cats = sorted([i.name for i in dd.iterdir() if i.is_dir() and i.name != "__pycache__"])
        return web.json_response(cats)

    async def handle_api_users(self, request):
        if not self._is_authenticated(request):
            return web.json_response({"error": "未登录"}, status=401)
        cat = request.match_info["category"]
        if not self._safe_part(cat):
            return web.json_response({"error": "非法分类名"}, status=400)
        cd = self.data_dir / "data" / cat
        if not cd.exists():
            return web.json_response([])
        users = []
        for f in cd.iterdir():
            if f.is_file() and f.suffix == ".json":
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    users.append({"id": f.stem, "display_name": d.get("display_name") or d.get("name") or f.stem, "file": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime})
                except Exception:
                    users.append({"id": f.stem, "display_name": f.stem, "file": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime})
        return web.json_response(users)

    async def handle_api_user_detail(self, request):
        if not self._is_authenticated(request):
            return web.json_response({"error": "未登录"}, status=401)
        cat = request.match_info["category"]
        uid = request.match_info["user_id"]
        if not self._safe_part(cat) or not self._safe_part(uid):
            return web.json_response({"error": "非法路径参数"}, status=400)
        uf = self.data_dir / "data" / cat / f"{uid}.json"
        if not uf.exists():
            return web.json_response({"error": "用户不存在"}, status=404)
        try:
            return web.json_response(json.loads(uf.read_text(encoding="utf-8")))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _release_port(self):
        import subprocess
        try:
            if sys.platform in ("linux", "linux2"):
                r = subprocess.run(["lsof", "-t", "-i", f":{self.port}"], capture_output=True, text=True)
                if r.stdout.strip():
                    for pid in r.stdout.strip().split("\n"):
                        try: os.kill(int(pid), signal.SIGTERM)
                        except: pass
                    time.sleep(0.5)
            elif sys.platform == "win32":
                r = subprocess.run(f'netstat -ano | findstr :{self.port}', capture_output=True, text=True, shell=True)
                if r.stdout.strip():
                    for line in r.stdout.strip().split("\n"):
                        if f":{self.port}" in line:
                            pid = line.strip().split()[-1]
                            try: subprocess.run(["taskkill", "/F", "/PID", pid], check=True)
                            except: pass
                    time.sleep(0.5)
        except Exception as e:
            logger.warning(f"释放端口失败: {e}")

    async def start(self):
        app = self._create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        try:
            await site.start()
            logger.info(f"WebUI 已启动: http://{self.host}:{self.port}")
        except OSError as e:
            if "Address already in use" in str(e) and self.release_port:
                logger.warning(f"端口 {self.port} 被占用，尝试释放...")
                await self._release_port()
                await site.start()
                logger.info(f"WebUI 已启动: http://{self.host}:{self.port}")
            else:
                raise
        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await site.stop()
        await runner.cleanup()
        logger.info("WebUI 已停止")


def main():
    parser = argparse.ArgumentParser(description="Stealer 独立 WebUI")
    parser.add_argument("--host", default=os.environ.get("STEALER_WEBUI_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("STEALER_WEBUI_PORT", "9191")))
    parser.add_argument("--token", default=os.environ.get("STEALER_WEBUI_PASSWORD", ""))
    parser.add_argument("--data-dir", default=os.environ.get("STEALER_DATA_DIR", ""))
    parser.add_argument("--no-release-port", action="store_true")
    args = parser.parse_args()

    data_dir = None
    if args.data_dir:
        data_dir = Path(args.data_dir)
        if not data_dir.exists():
            logger.error(f"数据目录不存在: {data_dir}")
            sys.exit(1)
    else:
        for c in [Path("data/plugin_data/astrbot_plugin_stealer"), Path.home()/"AstrBot/data/plugin_data/astrbot_plugin_stealer", Path("/AstrBot/data/plugin_data/astrbot_plugin_stealer")]:
            if c.exists():
                data_dir = c
                break
        if not data_dir:
            logger.error("未找到数据目录，请通过 --data-dir 或 STEALER_DATA_DIR 指定")
            sys.exit(1)

    logger.info(f"数据目录: {data_dir}")
    server = StealerWebUIServer(args.host, args.port, args.token, data_dir, release_port=not args.no_release_port)
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
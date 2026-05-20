
import asyncio
import base64
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from aiohttp import web
except ImportError:
    print("错误：请先安装 aiohttp")
    print(" pip install aiohttp")
    sys.exit(1)

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.config import AstrBotConfig
except ImportError:
    AstrBotConfig = dict

PLUGIN_NAME = "astrbot_plugin_stealer"
ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

_active_webui_server: Optional["WebUIRunner"] = None
_active_webui_lock = threading.Lock()


def _shutdown_active_webui(timeout: float = 5.0) -> bool:
    global _active_webui_server
    with _active_webui_lock:
        if _active_webui_server is None:
            return False
        server = _active_webui_server
        _active_webui_server = None
    try:
        if server.loop and server.loop.is_running():
            asyncio.run_coroutine_threadsafe(server._stop_server(), server.loop).result(timeout=timeout)
        return True
    except Exception as e:
        logger.warning(f"[StealerWebUI] 关闭旧 WebUI 实例失败: {e}")
        return False


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value).replace("，", ",").replace("、", ",").split(",") if x.strip()]


def _norm_scope(value: Any) -> str:
    raw = str(value or "public").strip().lower()
    return "local" if raw in {"local", "private", "scoped"} else "public"


def _mime(path: Path) -> str:
    return {".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".gif":"image/gif",".webp":"image/webp",".bmp":"image/bmp"}.get(path.suffix.lower(), "image/png")


class StealerDataStore:
    def __init__(self, data_dir: Path, protect_original_data: bool = True, allow_destructive_operations: bool = False, backup_on_write: bool = True):
        self.data_dir = Path(data_dir).resolve()
        self.protect_original_data = bool(protect_original_data)
        self.allow_destructive_operations = bool(allow_destructive_operations)
        self.backup_on_write = bool(backup_on_write)
        self.backup_dir = self.data_dir / ".stealer_webui_backups"
        self.categories_dir = self.data_dir / "categories"
        self.cache_dir = self.data_dir / "cache"
        self.db_path = self.data_dir / "emoji.db"
        self.categories_path = self.data_dir / "categories.json"
        self.category_info_path = self.data_dir / "category_info.json"
        self.categories_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _read_json(self, path: Path, default):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[StealerWebUI] 读取 JSON 失败 {path}: {e}")
        return default

    def _assert_inside_data_dir(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(self.data_dir)
        except ValueError:
            raise PermissionError(f"拒绝访问 Stealer 数据目录外路径: {resolved}")
        return resolved

    def _ensure_safe_filename(self, name: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(name))
        return safe[:120] or "unknown"

    def _backup_file(self, path: Path):
        path = self._assert_inside_data_dir(path)
        if not self.backup_on_write or not path.exists() or not path.is_file():
            return
        rel = path.relative_to(self.data_dir)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / ts / rel
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)

    def _deny_destructive(self):
        if self.protect_original_data and not self.allow_destructive_operations:
            raise PermissionError("已启用数据保护：拒绝删除、移动或替换原 Stealer 文件。若确需执行，请显式开启 allow_destructive_operations。")

    def _write_json(self, path: Path, data: Any):
        path = self._assert_inside_data_dir(path)
        self._backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def get_category_keys(self) -> list[str]:
        raw = self._read_json(self.categories_path, [])
        keys = [str(x).strip() for x in raw if str(x).strip()] if isinstance(raw, list) else []
        for p in self.categories_dir.iterdir() if self.categories_dir.exists() else []:
            if p.is_dir() and p.name not in keys:
                keys.append(p.name)
        return keys

    def get_category_info(self) -> list[dict[str, str]]:
        info = self._read_json(self.category_info_path, {})
        result = []
        for key in self.get_category_keys():
            item = info.get(key, {}) if isinstance(info, dict) else {}
            result.append({"key": key, "name": str(item.get("name") or key), "desc": str(item.get("desc") or "")})
        return result

    def save_categories(self, items: list[Any]):
        if self.protect_original_data and not self.allow_destructive_operations:
            raise PermissionError("已启用数据保护：拒绝改写 Stealer 分类配置。")
        keys=[]; info=self._read_json(self.category_info_path,{})
        if not isinstance(info, dict): info={}
        for item in items:
            if isinstance(item, dict):
                key=str(item.get("key","")).strip()
                if key and key not in keys:
                    keys.append(key)
                    info[key]={"name":str(item.get("name") or key),"desc":str(item.get("desc") or "")}
            else:
                key=str(item).strip()
                if key and key not in keys: keys.append(key)
        if not keys:
            raise ValueError("分类列表无效")
        self._write_json(self.categories_path, keys)
        self._write_json(self.category_info_path, info)
        for key in keys:
            (self.categories_dir / key).mkdir(parents=True, exist_ok=True)
        return keys

    def _db_rows(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        try:
            conn=sqlite3.connect(str(self.db_path)); conn.row_factory=sqlite3.Row
            rows=[dict(r) for r in conn.execute("SELECT * FROM emoji").fetchall()]
            paths=[r["path"] for r in rows]
            tag_map={p:[] for p in paths}; scene_map={p:[] for p in paths}
            if paths:
                ph=",".join("?"*len(paths))
                for r in conn.execute(f"SELECT path, tag FROM emoji_tag WHERE path IN ({ph})", paths): tag_map[r["path"]].append(r["tag"])
                for r in conn.execute(f"SELECT path, scene FROM emoji_scene WHERE path IN ({ph})", paths): scene_map[r["path"]].append(r["scene"])
            conn.close()
            for r in rows:
                r["tags"]=tag_map.get(r["path"],[])
                r["scenes"]=scene_map.get(r["path"],[])
            return rows
        except Exception as e:
            logger.warning(f"[StealerWebUI] 读取 emoji.db 失败，回退 JSON/目录: {e}")
            return []

    def _json_index(self) -> dict[str, Any]:
        merged={}
        for p in [self.cache_dir/"index_cache.json", self.data_dir/"index.json", self.data_dir/"image_index.json", self.data_dir/"cache"/"index.json"]:
            data=self._read_json(p,{})
            if isinstance(data,dict): merged.update(data)
        return merged

    def _scan_files(self) -> list[dict[str, Any]]:
        rows=[]
        for cat_dir in self.categories_dir.iterdir() if self.categories_dir.exists() else []:
            if not cat_dir.is_dir(): continue
            for f in cat_dir.iterdir():
                if f.is_file() and f.suffix.lower() in ALLOWED_EXTS:
                    try: h=hashlib.sha256(f.read_bytes()).hexdigest()
                    except Exception: h=f.stem
                    rows.append({"path":str(f),"hash":h,"category":cat_dir.name,"desc":"","tags":[],"scenes":[],"scope_mode":"public","origin_target":"","created_at":int(f.stat().st_mtime)})
        return rows

    def load_index(self) -> dict[str, dict[str, Any]]:
        rows=self._db_rows()
        if rows:
            return {r["path"]: r for r in rows if Path(str(r.get("path",""))).exists()}
        idx=self._json_index()
        if idx:
            return {p:dict(m, path=p) for p,m in idx.items() if isinstance(m,dict) and Path(p).exists()}
        return {r["path"]:r for r in self._scan_files()}

    def save_index_json(self, index: dict[str, dict[str, Any]]):
        self._write_json(self.cache_dir/"index_cache.json", {p:{k:v for k,v in m.items() if k!="path"} for p,m in index.items()})

    def list_images(self, page:int, size:int, category:str|None, q:str, sort:str) -> dict:
        index=self.load_index(); images=[]; counts={}
        q=(q or "").lower()
        for p,m in index.items():
            if not Path(p).exists(): continue
            cat=str(m.get("category") or Path(p).parent.name or "unknown")
            counts[cat]=counts.get(cat,0)+1
            item={"hash":str(m.get("hash") or Path(p).stem),"category":cat,"tags":_split_csv(m.get("tags",[])),"desc":str(m.get("desc") or ""),"scenes":_split_csv(m.get("scenes",[])),"scope_mode":_norm_scope(m.get("scope_mode")),"origin_target":str(m.get("origin_target") or ""),"created_at":int(m.get("created_at") or Path(p).stat().st_mtime)}
            if category and cat!=category: continue
            if q and not (q in item["desc"].lower() or any(q in x.lower() for x in item["tags"]+item["scenes"])): continue
            images.append(item)
        images.sort(key=lambda x:(x.get("created_at",0),x.get("hash","")), reverse=(sort!="oldest"))
        total=len(images); start=(max(1,page)-1)*size
        cat_info={c["key"]:c for c in self.get_category_info()}
        cats=[{"key":k,"name":cat_info.get(k,{}).get("name",k),"count":v} for k,v in counts.items()]
        cats.sort(key=lambda x:x["count"], reverse=True)
        return {"success":True,"total":total,"page":page,"size":size,"images":images[start:start+size],"categories":cats}

    def find_by_hash(self, h:str):
        for p,m in self.load_index().items():
            if str(m.get("hash") or Path(p).stem)==h:
                return p,m
        return None,None

    def update_image(self, h:str, data:dict):
        index=self.load_index(); target=None; meta=None
        for p,m in index.items():
            if str(m.get("hash") or Path(p).stem)==h:
                target=p; meta=m; break
        if not target or not meta: return False, "Image not found"
        if "tags" in data: meta["tags"]=_split_csv(data.get("tags"))
        if "desc" in data: meta["desc"]=str(data.get("desc") or "")
        if "scenes" in data or "scene" in data: meta["scenes"]=_split_csv(data.get("scenes", data.get("scene")))
        if "scope_mode" in data: meta["scope_mode"]=_norm_scope(data.get("scope_mode"))
        new_cat=str(data.get("category") or meta.get("category") or "unknown")
        if new_cat != str(meta.get("category")):
            self._deny_destructive()
            dst_dir=self._assert_inside_data_dir(self.categories_dir/self._ensure_safe_filename(new_cat)); dst_dir.mkdir(parents=True, exist_ok=True)
            old=self._assert_inside_data_dir(Path(target)); new=self._assert_inside_data_dir(dst_dir/old.name)
            shutil.move(str(old), str(new)); del index[target]; target=str(new); meta["path"]=target; meta["category"]=new_cat
        index[target]=meta; self.save_index_json(index); return True, ""

    def delete_hashes(self, hashes:set[str]) -> int:
        self._deny_destructive()
        index=self.load_index(); deleted=0
        for p,m in list(index.items()):
            if str(m.get("hash") or Path(p).stem) in hashes:
                target=self._assert_inside_data_dir(Path(p))
                if target.exists():
                    quarantine = self.backup_dir / "deleted_files" / datetime.now().strftime("%Y%m%d_%H%M%S") / target.relative_to(self.data_dir)
                    quarantine.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(quarantine))
                    deleted+=1
                index.pop(p,None)
        self.save_index_json(index); return deleted

    def move_hashes(self, hashes:set[str], category:str) -> int:
        self._deny_destructive()
        index=self.load_index(); moved=0; dst_dir=self._assert_inside_data_dir(self.categories_dir/self._ensure_safe_filename(category)); dst_dir.mkdir(parents=True, exist_ok=True)
        for p,m in list(index.items()):
            if str(m.get("hash") or Path(p).stem) in hashes:
                old=self._assert_inside_data_dir(Path(p))
                if old.exists():
                    new=self._assert_inside_data_dir(dst_dir/old.name); shutil.move(str(old), str(new)); index.pop(p,None); m["path"]=str(new); m["category"]=category; index[str(new)]=m; moved+=1
        self.save_index_json(index); return moved

    def update_scope(self, hashes:set[str], scope:str) -> tuple[int,int]:
        index=self.load_index(); updated=0; skipped=0
        for p,m in index.items():
            if str(m.get("hash") or Path(p).stem) in hashes:
                if scope=="local" and not str(m.get("origin_target") or "").strip(): skipped+=1; continue
                m["scope_mode"]=scope; updated+=1
        self.save_index_json(index); return updated, skipped


class WebUIRunner:
    def __init__(self, host:str, port:int, password:str, stealer_data_dir:Path, release_occupied_port:bool=True, protect_original_data: bool=True, allow_destructive_operations: bool=False, backup_on_write: bool=True):
        self.host=host; self.port=port; self.password=password; self.store=StealerDataStore(stealer_data_dir, protect_original_data=protect_original_data, allow_destructive_operations=allow_destructive_operations, backup_on_write=backup_on_write); self.release_occupied_port=release_occupied_port
        self.loop=None; self.runner=None; self.site=None; self.thread=None; self._started=threading.Event()

    def _token(self): return hashlib.sha256(self.password.encode()).hexdigest() if self.password else ""
    def _auth(self, request): return (not self.password) or request.cookies.get("stealer_webui_token")==self._token()

    def _create_app(self):
        app=web.Application(client_max_size=128*1024**2)
        for path,handler,methods in [
            ("/",self.handle_index,["GET"]),("/login.html",self.handle_login,["GET"]),("/api/login",self.handle_login_api,["POST"]),
            ("/api/images",self.handle_images,["GET"]),("/api/image-data",self.handle_image_data,["GET"]),("/api/serve-image",self.handle_serve_image,["GET"]),
            ("/api/stats",self.handle_stats,["GET"]),("/api/categories",self.handle_categories,["GET","POST"]),("/api/categories/delete",self.handle_delete_category,["POST"]),
            ("/api/emotions",self.handle_emotions,["GET"]),("/api/health",self.handle_health,["GET"]),("/api/images/update",self.handle_update,["POST"]),
            ("/api/images/delete",self.handle_delete,["POST","DELETE"]),("/api/images/batch-delete",self.handle_batch_delete,["POST"]),("/api/images/batch-move",self.handle_batch_move,["POST"]),("/api/images/batch-scope",self.handle_batch_scope,["POST"]),
            ("/api/images/upload",self.handle_upload,["POST"]),("/api/images/batch-upload",self.handle_batch_upload,["POST"]),("/api/images/batch-upload-status",self.handle_batch_status,["GET"]),("/api/analyze",self.handle_analyze,["POST"]),
        ]: app.router.add_route("*" if len(methods)>1 else methods[0], path, handler)
        app.router.add_static("/web", Path(__file__).parent/"web")
        return app

    def _need_auth(self, request):
        if not self._auth(request): return _json_response({"success":False,"error":"未登录"},401)
        return None

    async def handle_index(self, request):
        if self.password and not self._auth(request): raise web.HTTPFound("/login.html")
        return web.FileResponse(Path(__file__).parent/"web"/"index.html")
    async def handle_login(self, request): return web.FileResponse(Path(__file__).parent/"web"/"login.html")
    async def handle_login_api(self, request):
        data=await request.json()
        if data.get("password")==self.password:
            r=_json_response({"success":True}); r.set_cookie("stealer_webui_token", self._token(), httponly=True, samesite="Lax"); return r
        return _json_response({"success":False,"error":"密码错误"})
    async def handle_images(self, request):
        if (r:=self._need_auth(request)): return r
        q=request.query
        return _json_response(self.store.list_images(int(q.get("page",1)), int(q.get("size",50)), q.get("category") or None, q.get("q",""), q.get("sort","newest")))
    async def handle_image_data(self, request):
        if (r:=self._need_auth(request)): return r
        h=request.query.get("hash",""); p,_=self.store.find_by_hash(h)
        if not p: return _json_response({"success":False,"error":"图片未找到"})
        path=Path(p); data=base64.b64encode(path.read_bytes()).decode(); return _json_response({"success":True,"hash":h,"url":f"data:{_mime(path)};base64,{data}"})
    async def handle_serve_image(self, request):
        if (r:=self._need_auth(request)): return r
        p=Path(request.query.get("path",""))
        try: p.resolve().relative_to(self.store.data_dir)
        except Exception: return _json_response({"success":False,"error":"路径非法"},403)
        return web.FileResponse(p) if p.exists() else _json_response({"success":False,"error":"文件不存在"},404)
    async def handle_stats(self, request):
        if (r:=self._need_auth(request)): return r
        idx=self.store.load_index(); today=datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp(); return _json_response({"success":True,"stats":{"total":len(idx),"categories":len(self.store.get_category_keys()),"today":sum(1 for m in idx.values() if int(m.get("created_at",0) or 0)>=today)}})
    async def handle_categories(self, request):
        if (r:=self._need_auth(request)): return r
        if request.method=="POST":
            try:
                data=await request.json(); keys=self.store.save_categories(data.get("categories",[])); return _json_response({"success":True,"categories":keys})
            except PermissionError as e:
                return _json_response({"success":False,"error":str(e)}, 403)
        cats={k:0 for k in self.store.get_category_keys()}
        for m in self.store.load_index().values(): cats[str(m.get("category","unknown"))]=cats.get(str(m.get("category","unknown")),0)+1
        return _json_response({"success":True,"categories":cats})
    async def handle_emotions(self, request):
        if (r:=self._need_auth(request)): return r
        return _json_response({"success":True,"emotions":self.store.get_category_info()})
    async def handle_health(self, request): return _json_response({"success":True,"status":"ok","service":"emoji-manager-webui"})
    async def handle_update(self, request):
        if (r:=self._need_auth(request)): return r
        try:
            data=await request.json(); ok,err=self.store.update_image(str(data.get("hash","")), data); return _json_response({"success":ok,"error":err})
        except PermissionError as e:
            return _json_response({"success":False,"error":str(e)}, 403)
    async def handle_delete(self, request):
        if (r:=self._need_auth(request)): return r
        try:
            data=await request.json(); n=self.store.delete_hashes({str(data.get("hash",""))}); return _json_response({"success":n>0,"count":n,"error":"图片未找到" if n==0 else ""})
        except PermissionError as e:
            return _json_response({"success":False,"count":0,"error":str(e)}, 403)
    async def handle_batch_delete(self, request):
        if (r:=self._need_auth(request)): return r
        try:
            data=await request.json(); return _json_response({"success":True,"count":self.store.delete_hashes(set(map(str,data.get("hashes",[]))))})
        except PermissionError as e:
            return _json_response({"success":False,"count":0,"error":str(e)}, 403)
    async def handle_batch_move(self, request):
        if (r:=self._need_auth(request)): return r
        try:
            data=await request.json(); return _json_response({"success":True,"count":self.store.move_hashes(set(map(str,data.get("hashes",[]))), str(data.get("category","unknown")))})
        except PermissionError as e:
            return _json_response({"success":False,"count":0,"error":str(e)}, 403)
    async def handle_batch_scope(self, request):
        if (r:=self._need_auth(request)): return r
        data=await request.json(); u,s=self.store.update_scope(set(map(str,data.get("hashes",[]))), _norm_scope(data.get("scope_mode"))); return _json_response({"success":True,"count":u,"skipped":s})
    async def handle_upload(self, request): return _json_response({"success":False,"error":"独立补丁暂不支持单图上传，请使用目标插件原生 WebUI 或批量导入待后续适配"})
    async def handle_batch_upload(self, request): return _json_response({"success":False,"error":"独立补丁暂不支持批量上传"})
    async def handle_batch_status(self, request): return _json_response({"success":False,"error":"任务不存在或独立补丁未启用上传"})
    async def handle_analyze(self, request): return _json_response({"success":False,"error":"独立补丁无法调用目标插件 VLM 服务"})
    async def handle_delete_category(self, request):
        if (r:=self._need_auth(request)): return r
        try:
            self.store._deny_destructive()
            data=await request.json(); key=str(data.get("key","")); cats=[c for c in self.store.get_category_info() if c["key"]!=key]; self.store.save_categories(cats); target=self.store._assert_inside_data_dir(self.store.categories_dir/self.store._ensure_safe_filename(key)); shutil.rmtree(target, ignore_errors=True); return _json_response({"success":True,"deleted":key,"categories":[c["key"] for c in cats]})
        except PermissionError as e:
            return _json_response({"success":False,"error":str(e)}, 403)

    def _is_addr_in_use(self, e: OSError) -> bool:
        return getattr(e, "errno", None) == 98 or "address already in use" in str(e).lower()

    async def _release_port(self):
        import subprocess
        try:
            r=subprocess.run(["lsof","-t","-i",f":{self.port}"],capture_output=True,text=True)
            pids=[pid.strip() for pid in r.stdout.strip().splitlines() if pid.strip()]
            for pid in pids:
                os.kill(int(pid), signal.SIGTERM)
            if pids:
                logger.warning(f"[StealerWebUI] 端口 {self.port} 被占用，已尝试终止进程: {', '.join(pids)}")
                time.sleep(.8)
            return bool(pids)
        except Exception as e:
            logger.warning(f"[StealerWebUI] 释放端口失败: {e}")
            return False

    async def _start_server(self):
        self.runner=web.AppRunner(self._create_app()); await self.runner.setup()
        self.site=web.TCPSite(self.runner,self.host,self.port)
        try:
            await self.site.start()
        except OSError as e:
            if not self._is_addr_in_use(e):
                raise
            if self.release_occupied_port:
                logger.warning(f"[StealerWebUI] 目标端口 {self.port} 被占用，尝试释放后继续绑定同一端口")
                await self._release_port()
                try:
                    await self.site.start()
                except OSError as e2:
                    if self._is_addr_in_use(e2):
                        raise OSError(f"目标端口 {self.port} 仍被占用。请关闭占用进程，或在配置中修改 webui_port。插件不会自动打开其他端口。") from e2
                    raise
            else:
                raise OSError(f"目标端口 {self.port} 已被占用。请关闭占用进程，或在配置中修改 webui_port。插件不会自动打开其他端口。") from e
        logger.info(f"[StealerWebUI] WebUI 已启动: http://{self.host}:{self.port}"); self._started.set()
        while True: await asyncio.sleep(3600)
    async def _stop_server(self):
        if self.site: await self.site.stop()
        if self.runner: await self.runner.cleanup()
        logger.info("[StealerWebUI] WebUI 已停止")
    def start_in_thread(self):
        def run():
            self.loop=asyncio.new_event_loop(); asyncio.set_event_loop(self.loop)
            try: self.loop.run_until_complete(self._start_server())
            except Exception as e: logger.error(f"[StealerWebUI] WebUI 运行出错: {e}", exc_info=True)
        self.thread=threading.Thread(target=run,daemon=True); self.thread.start(); self._started.wait(timeout=10); return self._started.is_set()


@register("astrbot_plugin_stealer_webui", "ERX399", "Stealer WebUI 独立版", "0.2.5")
class StealerWebUIStandalonePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context); self.config=config; self._webui_runner=None; _shutdown_active_webui()
        data_dir=self._resolve_stealer_data_dir()
        if not data_dir or not data_dir.exists(): logger.error("[StealerWebUI] 无法找到 Stealer 数据目录，WebUI 未启动"); return
        host=config.get("webui_host","0.0.0.0"); port=int(config.get("webui_port",9191)); password=config.get("webui_password","") or os.environ.get("STEALER_WEBUI_PASSWORD","")
        protect_original_data=bool(config.get("protect_original_data", True))
        allow_destructive_operations=bool(config.get("allow_destructive_operations", False))
        backup_on_write=bool(config.get("backup_on_write", True))
        self._webui_runner=WebUIRunner(host,port,password,Path(data_dir),bool(config.get("release_occupied_port",True)),protect_original_data=protect_original_data,allow_destructive_operations=allow_destructive_operations,backup_on_write=backup_on_write)
        if self._webui_runner.start_in_thread():
            global _active_webui_server; _active_webui_server=self._webui_runner
    def _resolve_stealer_data_dir(self):
        configured=self.config.get("stealer_data_dir","")
        if configured and Path(configured).exists(): return Path(configured)
        try:
            from astrbot.api.star import StarTools
            p=Path(StarTools.get_data_dir(PLUGIN_NAME))
            if p.exists(): return p
            cur=Path(StarTools.get_data_dir()); sib=cur.parent/PLUGIN_NAME
            if sib.exists(): return sib
        except Exception as e: logger.warning(f"[StealerWebUI] 自动推断目录失败: {e}")
        for p in [Path("data/plugin_data")/PLUGIN_NAME, Path.home()/"AstrBot/data/plugin_data"/PLUGIN_NAME]:
            if p.exists(): return p
        return None
    async def terminate(self):
        global _active_webui_server
        with _active_webui_lock:
            if _active_webui_server == self._webui_runner: _active_webui_server=None
        if self._webui_runner and self._webui_runner.loop:
            try: asyncio.run_coroutine_threadsafe(self._webui_runner._stop_server(), self._webui_runner.loop).result(timeout=5)
            except Exception: pass

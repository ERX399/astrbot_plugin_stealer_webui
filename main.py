
import asyncio
import base64
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import threading
import time
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

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
    def __init__(self, data_dir: Path, protect_original_data: bool = False, allow_destructive_operations: bool = True, backup_on_write: bool = True):
        self.data_dir = Path(data_dir).resolve()
        # WebUI 是 astrbot_plugin_stealer 的补丁；fallback 数据层直接按原插件数据文件工作。
        # 不再用“保护原数据”阻断读取/删除/移动/改索引，否则拿不到原插件实例时 WebUI 会失效。
        self.protect_original_data = False
        self.allow_destructive_operations = True
        self.backup_on_write = bool(backup_on_write)
        self.backup_dir = self.data_dir / ".stealer_webui_backups"
        self.categories_dir = self.data_dir / "categories"
        self.cache_dir = self.data_dir / "cache"
        self.db_path = self.data_dir / "emoji.db"
        self.db_paths = [self.data_dir / "emoji.db", self.cache_dir / "emoji.db"]
        self.categories_path = self.data_dir / "categories.json"
        self.category_info_path = self.data_dir / "category_info.json"
        # 不在初始化时创建目录；只读/写入时按需创建，避免错误路径下生成空数据目录。

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
        return

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
        db_path = next((p for p in getattr(self, "db_paths", [self.db_path]) if Path(p).exists()), None)
        if not db_path:
            return []
        try:
            conn=sqlite3.connect(str(db_path)); conn.row_factory=sqlite3.Row
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
        for p in [
            self.cache_dir/"index_cache.json",
            self.data_dir/"index.json",
            self.data_dir/"image_index.json",
            self.data_dir/"cache"/"index.json",
            self.data_dir/"cache"/"index_cache.json",
        ]:
            data=self._read_json(p,{})
            if isinstance(data,dict):
                merged.update(data)
        return merged

    def _resolve_image_path(self, raw_path: Any, meta: dict[str, Any] | None = None) -> Path:
        text = str(raw_path or "").strip()
        if not text and meta:
            text = str(meta.get("path") or meta.get("file") or meta.get("filename") or "").strip()
        p = Path(text)

        candidates: list[Path] = []

        # 原版数据库可能保存另一台机器/容器中的绝对路径：
        # /root/AstrBot/data/plugin_data/astrbot_plugin_stealer/categories/...
        # 在补丁 WebUI 中应重映射到当前 data_dir。
        if p.is_absolute():
            candidates.append(p)
            parts = list(p.parts)
            if "astrbot_plugin_stealer" in parts:
                idx = parts.index("astrbot_plugin_stealer")
                rel = Path(*parts[idx + 1:]) if idx + 1 < len(parts) else Path()
                if str(rel):
                    candidates.append(self.data_dir / rel)
            if "categories" in parts:
                idx = parts.index("categories")
                rel = Path(*parts[idx:]) if idx < len(parts) else Path()
                if str(rel):
                    candidates.append(self.data_dir / rel)
        else:
            # 原插件旧数据可能保存 categories/xxx/a.webp、xxx/a.webp 或仅文件名
            candidates.extend([
                self.data_dir / p,
                self.categories_dir / p,
            ])

        if meta:
            cat = str(meta.get("category") or "").strip()
            if cat:
                candidates.append(self.categories_dir / cat / p.name)

        for c in candidates:
            if c.exists():
                return c
        return candidates[0] if candidates else (self.data_dir / p)

    def _normalize_index_record(self, key: str, meta: Any) -> tuple[str, dict[str, Any]] | None:
        if not isinstance(meta, dict):
            return None
        p = self._resolve_image_path(key, meta)
        if not p.exists():
            alt = self._resolve_image_path(meta.get("path"), meta)
            if alt.exists():
                p = alt
        if not p.exists():
            return None
        item = dict(meta)
        item["path"] = str(p)
        if not item.get("hash"):
            item["hash"] = p.stem
        if not item.get("category"):
            item["category"] = p.parent.name
        return str(p), item

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
            result={}
            for r in rows:
                norm=self._normalize_index_record(str(r.get("path") or ""), r)
                if norm:
                    result[norm[0]]=norm[1]
            if result:
                return result
        idx=self._json_index()
        if idx:
            result={}
            for p,m in idx.items():
                norm=self._normalize_index_record(str(p), m)
                if norm:
                    result[norm[0]]=norm[1]
            if result:
                return result
        return {r["path"]:r for r in self._scan_files()}

    def _sync_db_from_index(self, index: dict[str, dict[str, Any]]) -> None:
        """按原插件 database_service.sync_index 思路，把完整 index 同步到 emoji.db。

        这样 fallback 模式下 update/move/scope/delete 不会只改 JSON index，
        还会同步 emoji / emoji_tag / emoji_scene，避免数据残留或不一致。
        """
        db_path = next((p for p in getattr(self, "db_paths", [self.db_path]) if Path(p).exists()), None)
        if not db_path:
            return

        desired: dict[str, dict[str, Any]] = {}
        for raw_path, meta in (index or {}).items():
            if not isinstance(meta, dict):
                continue
            norm = self._normalize_index_record(str(raw_path), meta)
            if not norm:
                continue
            desired[norm[0]] = norm[1]

        try:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("BEGIN")
                existing = {
                    str(row[0])
                    for row in conn.execute("SELECT path FROM emoji").fetchall()
                }
                desired_paths = set(desired.keys())

                for stale_path in existing - desired_paths:
                    conn.execute("DELETE FROM emoji_tag WHERE path = ?", (stale_path,))
                    conn.execute("DELETE FROM emoji_scene WHERE path = ?", (stale_path,))
                    conn.execute("DELETE FROM emoji WHERE path = ?", (stale_path,))

                now = int(time.time())
                for path, meta in desired.items():
                    p = Path(path)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO emoji
                        (path, hash, phash, category, desc, source, origin_target,
                         scope_mode, created_at, use_count, last_used_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            path,
                            str(meta.get("hash") or p.stem),
                            meta.get("phash"),
                            str(meta.get("category") or p.parent.name or "unknown"),
                            str(meta.get("desc") or ""),
                            meta.get("source"),
                            str(meta.get("origin_target") or ""),
                            _norm_scope(meta.get("scope_mode")),
                            int(meta.get("created_at") or (p.stat().st_mtime if p.exists() else now)),
                            int(meta.get("use_count") or 0),
                            int(meta.get("last_used_at") or 0),
                        ),
                    )

                    conn.execute("DELETE FROM emoji_tag WHERE path = ?", (path,))
                    conn.execute("DELETE FROM emoji_scene WHERE path = ?", (path,))

                    for tag in _split_csv(meta.get("tags", [])):
                        conn.execute(
                            "INSERT OR IGNORE INTO emoji_tag (path, tag) VALUES (?, ?)",
                            (path, tag),
                        )
                    for scene in _split_csv(meta.get("scenes", meta.get("scene", []))):
                        conn.execute(
                            "INSERT OR IGNORE INTO emoji_scene (path, scene) VALUES (?, ?)",
                            (path, scene),
                        )

                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"[StealerWebUI] 同步 emoji.db 失败: {e}")

    def save_index_json(self, index: dict[str, dict[str, Any]]):
        self._write_json(self.cache_dir/"index_cache.json", {p:{k:v for k,v in m.items() if k!="path"} for p,m in index.items()})
        self._sync_db_from_index(index)

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

    def _delete_from_db(self, removed: list[tuple[str, str]]) -> None:
        """同步清理原插件 sqlite 数据库，避免只删文件/索引导致残留。"""
        if not removed:
            return
        db_path = next((p for p in getattr(self, "db_paths", [self.db_path]) if Path(p).exists()), None)
        if not db_path:
            return
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("BEGIN")
                for old_path, img_hash in removed:
                    # DB 里可能保存旧绝对路径；同时按 hash 清理更稳。
                    conn.execute(
                        "DELETE FROM emoji_tag WHERE path = ? OR path IN (SELECT path FROM emoji WHERE hash = ?)",
                        (old_path, img_hash),
                    )
                    conn.execute(
                        "DELETE FROM emoji_scene WHERE path = ? OR path IN (SELECT path FROM emoji WHERE hash = ?)",
                        (old_path, img_hash),
                    )
                    conn.execute("DELETE FROM emoji WHERE path = ? OR hash = ?", (old_path, img_hash))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"[StealerWebUI] 清理 emoji.db 删除残留失败: {e}")

    def _add_blacklist_hashes(self, hashes: set[str]) -> None:
        if not hashes:
            return
        path = self.cache_dir / "blacklist_cache.json"
        data = self._read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        now = int(time.time())
        for h in hashes:
            if h:
                data[str(h)] = now
        self._write_json(path, data)

    def delete_hashes(self, hashes:set[str], blacklist: bool = False) -> int:
        self._deny_destructive()
        hashes = {str(h).strip() for h in hashes if str(h).strip()}
        if not hashes:
            return 0
        index=self.load_index(); deleted=0; removed=[]
        for p,m in list(index.items()):
            img_hash = str(m.get("hash") or Path(p).stem)
            if img_hash in hashes:
                target=self._assert_inside_data_dir(Path(p))
                old_db_path = str(m.get("path") or p)
                if target.exists():
                    quarantine = self.backup_dir / "deleted_files" / datetime.now().strftime("%Y%m%d_%H%M%S") / target.relative_to(self.data_dir)
                    quarantine.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(quarantine))
                    deleted+=1
                else:
                    # 文件已不存在也要清理索引/数据库残留。
                    deleted+=1
                removed.append((old_db_path, img_hash))
                index.pop(p,None)
        self.save_index_json(index)
        self._delete_from_db(removed)
        if blacklist:
            self._add_blacklist_hashes({h for _, h in removed})
        return deleted


    def persist_upload(
        self,
        content: bytes,
        ext: str,
        category: str = "",
        tags: list[str] | None = None,
        desc: str = "",
        scenes: list[str] | None = None,
        file_hash: str | None = None,
    ) -> dict:
        self._deny_destructive()
        ext = (ext or ".png").lower()
        if ext not in ALLOWED_EXTS:
            raise ValueError(f"不支持的文件类型: {ext}")
        if not content:
            raise ValueError("文件内容为空")

        category = str(category or "").strip() or (self.get_category_keys()[0] if self.get_category_keys() else "unknown")
        img_hash = file_hash or hashlib.sha256(content).hexdigest()

        existing_path, existing_meta = self.find_by_hash(img_hash)
        if existing_path and Path(existing_path).exists():
            return {
                "hash": img_hash,
                "category": str((existing_meta or {}).get("category") or category),
                "path": existing_path,
                "duplicate": True,
            }

        ts = int(time.time())
        filename = f"{ts}_{uuid.uuid4().hex[:8]}{ext}"
        dst_dir = self._assert_inside_data_dir(self.categories_dir / self._ensure_safe_filename(category))
        dst_dir.mkdir(parents=True, exist_ok=True)
        file_path = self._assert_inside_data_dir(dst_dir / filename)
        file_path.write_bytes(content)

        index = self.load_index()
        meta = {
            "path": str(file_path),
            "hash": img_hash,
            "category": category,
            "desc": str(desc or ""),
            "tags": _split_csv(tags or []),
            "scenes": _split_csv(scenes or []),
            "scope_mode": "public",
            "origin_target": "",
            "created_at": ts,
            "use_count": 0,
            "last_used_at": 0,
        }
        index[str(file_path)] = meta
        self.save_index_json(index)
        return {"hash": img_hash, "category": category, "path": str(file_path), "duplicate": False}

    def delete_category(self, key: str) -> dict:
        key = str(key or "").strip()
        if not key:
            return {"success": False, "error": "分类Key无效"}
        keys = self.get_category_keys()
        if key not in keys:
            return {"success": False, "error": "分类不存在"}
        if len(keys) <= 1:
            return {"success": False, "error": "至少需要保留1个分类"}

        index = self.load_index()
        hashes = {
            str(m.get("hash") or Path(p).stem)
            for p, m in index.items()
            if isinstance(m, dict) and str(m.get("category") or Path(p).parent.name) == key
        }
        deleted = self.delete_hashes(hashes) if hashes else 0

        updated = [k for k in keys if k != key]
        info = self._read_json(self.category_info_path, {})
        if isinstance(info, dict):
            info.pop(key, None)
        else:
            info = {}

        self._write_json(self.categories_path, updated)
        self._write_json(self.category_info_path, info)

        cat_dir = self._assert_inside_data_dir(self.categories_dir / self._ensure_safe_filename(key))
        try:
            if cat_dir.exists():
                shutil.rmtree(cat_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"[StealerWebUI] 删除分类目录失败 {cat_dir}: {e}")

        # delete_hashes 已同步过 index/db；这里再确保分类配置删除后没有残留分类目录索引。
        self.save_index_json(self.load_index())
        return {"success": True, "deleted": key, "categories": updated, "deleted_files": deleted}

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


class OriginalStealerBridge:
    """最小桥接原版 astrbot_plugin_stealer 实例，补丁层写操作统一走这里。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    async def load_index(self) -> dict[str, Any]:
        if hasattr(self.plugin, "_load_index"):
            return await self.plugin._load_index()
        db = getattr(self.plugin, "db_service", None)
        if db and hasattr(db, "get_index_cache_readonly"):
            return db.get_index_cache_readonly() or {}
        cache = getattr(self.plugin, "cache_service", None)
        if cache and hasattr(cache, "get_index_cache_readonly"):
            return cache.get_index_cache_readonly() or {}
        return {}

    async def save_index(self, index: dict[str, Any]) -> None:
        if hasattr(self.plugin, "_save_index"):
            await self.plugin._save_index(index)
            return
        db = getattr(self.plugin, "db_service", None)
        if db and hasattr(db, "sync_index"):
            await db.sync_index(index)
        cache = getattr(self.plugin, "cache_service", None)
        if cache and hasattr(cache, "set_cache"):
            await cache.set_cache("index_cache", index, persist=False)

    def category_info(self) -> list[dict[str, str]]:
        cfg = getattr(self.plugin, "plugin_config", None)
        if cfg and hasattr(cfg, "get_category_info"):
            return cfg.get_category_info()
        return [{"key": str(c), "name": str(c), "desc": ""} for c in getattr(self.plugin, "categories", []) or []]

    def category_keys(self) -> list[str]:
        cfg = getattr(self.plugin, "plugin_config", None)
        if cfg and hasattr(cfg, "categories"):
            return [str(c) for c in (cfg.categories or [])]
        return [str(c) for c in getattr(self.plugin, "categories", []) or []]

    def _build_image_item(self, path: str, meta: dict[str, Any]) -> dict[str, Any]:
        p = Path(path)
        return {
            "hash": str(meta.get("hash") or p.stem),
            "category": str(meta.get("category") or p.parent.name or "unknown"),
            "tags": _split_csv(meta.get("tags", [])),
            "desc": str(meta.get("desc") or ""),
            "scenes": _split_csv(meta.get("scenes", meta.get("scene", []))),
            "scope_mode": _norm_scope(meta.get("scope_mode")),
            "origin_target": str(meta.get("origin_target") or ""),
            "created_at": int(meta.get("created_at") or (p.stat().st_mtime if p.exists() else 0)),
        }

    async def list_images(self, page:int, size:int, category:str|None, q:str, sort:str) -> dict:
        index = await self.load_index()
        images = []
        counts: dict[str, int] = {}
        q = (q or "").lower()
        for p, m in index.items():
            if not isinstance(m, dict) or not Path(p).exists():
                continue
            item = self._build_image_item(str(p), m)
            cat = item["category"]
            counts[cat] = counts.get(cat, 0) + 1
            if category and cat != category:
                continue
            haystack = " ".join([item["desc"], *map(str, item["tags"]), *map(str, item["scenes"])]).lower()
            if q and q not in haystack:
                continue
            images.append(item)
        images.sort(key=lambda x:(x.get("created_at",0),x.get("hash","")), reverse=(sort!="oldest"))
        total=len(images); start=(max(1,page)-1)*size
        cat_info={c["key"]:c for c in self.category_info()}
        cats=[{"key":k,"name":cat_info.get(k,{}).get("name",k),"count":v} for k,v in counts.items()]
        cats.sort(key=lambda x:x["count"], reverse=True)
        return {"success":True,"total":total,"page":page,"size":size,"images":images[start:start+size],"categories":cats}

    async def find_by_hash(self, h:str):
        for p,m in (await self.load_index()).items():
            if isinstance(m, dict) and str(m.get("hash") or Path(p).stem)==h:
                return p,m
        return None,None

    async def stats(self) -> dict:
        idx = await self.load_index()
        today=datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        return {"success":True,"stats":{"total":len(idx),"categories":len(self.category_keys()),"today":sum(1 for m in idx.values() if isinstance(m, dict) and int(m.get("created_at",0) or 0)>=today)}}

    async def categories(self) -> dict:
        cats={k:0 for k in self.category_keys()}
        for m in (await self.load_index()).values():
            if isinstance(m, dict):
                cat=str(m.get("category","unknown"))
                cats[cat]=cats.get(cat,0)+1
        return {"success":True,"categories":cats}

    async def update_image(self, img_hash: str, data: dict) -> tuple[bool, str]:
        index = dict(await self.load_index())
        target = None
        meta = None
        for p, m in index.items():
            if isinstance(m, dict) and str(m.get("hash") or Path(p).stem) == img_hash:
                target, meta = p, m
                break
        if not target or not meta:
            return False, "Image not found"

        if "tags" in data:
            meta["tags"] = _split_csv(data.get("tags"))
        if "desc" in data:
            meta["desc"] = str(data.get("desc") or "")
        if "scenes" in data or "scene" in data:
            meta["scenes"] = _split_csv(data.get("scenes", data.get("scene")))
        if "scope_mode" in data:
            scope = _norm_scope(data.get("scope_mode"))
            if scope == "local" and not str(meta.get("origin_target") or "").strip():
                return False, "Origin target missing"
            meta["scope_mode"] = scope

        new_cat = str(data.get("category") or meta.get("category") or "unknown")
        if new_cat != str(meta.get("category")):
            old = Path(target)
            if not old.exists():
                return False, "Source file not found"
            cfg = getattr(self.plugin, "plugin_config", None)
            dst_dir = cfg.ensure_category_dir(new_cat) if cfg and hasattr(cfg, "ensure_category_dir") else old.parent.parent / new_cat
            dst_dir.mkdir(parents=True, exist_ok=True)
            new = dst_dir / old.name
            await asyncio.to_thread(shutil.move, str(old), str(new))
            index.pop(target, None)
            target = str(new)
            meta["path"] = target
            meta["category"] = new_cat
        index[target] = meta
        await self.save_index(index)
        return True, ""

    async def delete_hashes(self, hashes: set[str], blacklist: bool = False) -> int:
        hashes = {str(h).strip() for h in hashes if str(h).strip()}
        if not hashes:
            return 0

        index = dict(await self.load_index())
        removed: list[tuple[str, str]] = []
        for p, m in list(index.items()):
            h = str(m.get("hash") or Path(p).stem) if isinstance(m, dict) else Path(p).stem
            if h in hashes:
                removed.append((p, h))
                index.pop(p, None)

        # 先保存/同步索引，让原插件 db_service 能清理 emoji / emoji_tag / emoji_scene。
        await self.save_index(index)

        # 再显式尝试同步一次 DB/cache，兼容原插件不同版本。
        db = getattr(self.plugin, "db_service", None)
        cache = getattr(self.plugin, "cache_service", None)
        try:
            if db and hasattr(db, "sync_index"):
                await db.sync_index(index)
            elif db and hasattr(db, "save_index"):
                await db.save_index(index)
        except Exception as e:
            logger.warning(f"[StealerWebUI] 原版数据库同步删除索引失败: {e}")

        try:
            if cache and hasattr(cache, "set_cache"):
                await cache.set_cache("index_cache", index, persist=False)
        except Exception as e:
            logger.warning(f"[StealerWebUI] 原版缓存同步删除索引失败: {e}")

        deleted = 0
        ips = getattr(self.plugin, "image_processor_service", None)
        for p, h in removed:
            try:
                if hasattr(self.plugin, "_safe_remove_file"):
                    await self.plugin._safe_remove_file(p)
                else:
                    await asyncio.to_thread(Path(p).unlink, missing_ok=True)
                deleted += 1
            except Exception as e:
                logger.warning(f"[StealerWebUI] 原版删除调用失败 {p}: {e}")

            if blacklist and cache and hasattr(cache, "set"):
                await cache.set("blacklist_cache", h, int(time.time()), persist=True)

            # 清理原插件内存缓存，避免删除后仍可从缓存里看到/命中。
            if cache:
                try:
                    caches = getattr(cache, "_caches", None)
                    lock = getattr(cache, "_lock", None)
                    def _purge():
                        if isinstance(caches, dict):
                            for name in ("image_cache", "text_cache", "desc_cache", "bm25_cache"):
                                c = caches.get(name)
                                if hasattr(c, "pop"):
                                    c.pop(h, None)
                                    c.pop(p, None)
                    if lock:
                        with lock:
                            _purge()
                    else:
                        _purge()
                except Exception as e:
                    logger.debug(f"[StealerWebUI] 清理原版内存缓存失败: {e}")

            if ips and hasattr(ips, "invalidate_cache"):
                ips.invalidate_cache(h)

        return deleted

    async def move_hashes(self, hashes: set[str], category: str) -> int:
        index = dict(await self.load_index())
        cfg = getattr(self.plugin, "plugin_config", None)
        dst_dir = cfg.ensure_category_dir(category) if cfg and hasattr(cfg, "ensure_category_dir") else None
        moved = 0
        for p, m in list(index.items()):
            if not isinstance(m, dict) or str(m.get("hash") or Path(p).stem) not in hashes:
                continue
            old = Path(p)
            if not old.exists():
                continue
            target_dir = dst_dir or old.parent.parent / category
            target_dir.mkdir(parents=True, exist_ok=True)
            new = target_dir / old.name
            await asyncio.to_thread(shutil.move, str(old), str(new))
            index.pop(p, None)
            m["path"] = str(new)
            m["category"] = category
            index[str(new)] = m
            moved += 1
        await self.save_index(index)
        return moved

    async def update_scope(self, hashes: set[str], scope: str) -> tuple[int, int]:
        index = dict(await self.load_index())
        updated = skipped = 0
        for m in index.values():
            if not isinstance(m, dict) or str(m.get("hash") or "") not in hashes:
                continue
            if scope == "local" and not str(m.get("origin_target") or "").strip():
                skipped += 1
                continue
            m["scope_mode"] = scope
            updated += 1
        await self.save_index(index)
        return updated, skipped


    async def persist_upload(
        self,
        content: bytes,
        ext: str,
        category: str = "",
        tags: list[str] | None = None,
        desc: str = "",
        scenes: list[str] | None = None,
        file_hash: str | None = None,
    ) -> dict:
        ext = (ext or ".png").lower()
        if ext not in ALLOWED_EXTS:
            raise ValueError(f"不支持的文件类型: {ext}")
        if not content:
            raise ValueError("文件内容为空")

        cache = getattr(self.plugin, "cache_service", None)
        img_hash = file_hash or (cache.compute_hash(content) if cache and hasattr(cache, "compute_hash") else hashlib.sha256(content).hexdigest())

        p, meta = await self.find_by_hash(img_hash)
        if p and Path(p).exists():
            return {
                "hash": img_hash,
                "category": str((meta or {}).get("category") or category or "unknown"),
                "path": p,
                "duplicate": True,
            }

        cfg = getattr(self.plugin, "plugin_config", None)
        keys = self.category_keys()
        final_cat = str(category or "").strip() or (keys[0] if keys else "unknown")
        if cfg and hasattr(cfg, "ensure_category_dir"):
            dst_dir = cfg.ensure_category_dir(final_cat)
        else:
            base = Path(getattr(self.plugin, "base_dir", "")) if getattr(self.plugin, "base_dir", None) else None
            dst_dir = (base / "categories" / final_cat) if base else Path.cwd() / "categories" / final_cat
            dst_dir.mkdir(parents=True, exist_ok=True)

        ts = int(time.time())
        filename = f"{ts}_{uuid.uuid4().hex[:8]}{ext}"
        file_path = dst_dir / filename
        await asyncio.to_thread(file_path.write_bytes, content)

        index = dict(await self.load_index())
        item = {
            "path": str(file_path),
            "hash": img_hash,
            "category": final_cat,
            "desc": str(desc or ""),
            "tags": _split_csv(tags or []),
            "scenes": _split_csv(scenes or []),
            "scope_mode": "public",
            "origin_target": "",
            "created_at": ts,
            "use_count": 0,
            "last_used_at": 0,
        }
        index[str(file_path)] = item
        await self.save_index(index)
        return {"hash": img_hash, "category": final_cat, "path": str(file_path), "duplicate": False}

    async def analyze_image(self, *, img_hash: str = "", img_base64: str = "") -> dict:
        proc = getattr(self.plugin, "image_processor_service", None)
        if not proc:
            return {"success": False, "error": "图片处理服务不可用"}

        file_path = None
        tmp_file = None
        if img_hash:
            p, _ = await self.find_by_hash(img_hash)
            if p and Path(p).is_file():
                file_path = p

        if not file_path and img_base64:
            b64_data = img_base64
            ext = ".png"
            if "," in b64_data:
                header, b64_data = b64_data.split(",", 1)
                header = header.lower()
                if "jpeg" in header or "jpg" in header:
                    ext = ".jpg"
                elif "gif" in header:
                    ext = ".gif"
                elif "webp" in header:
                    ext = ".webp"
                elif "bmp" in header:
                    ext = ".bmp"
            content = base64.b64decode(b64_data)
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            try:
                tmp.write(content)
                tmp.close()
                file_path = tmp.name
                tmp_file = tmp.name
            except Exception:
                try:
                    tmp.close()
                except Exception:
                    pass
                raise

        if not file_path:
            return {"success": False, "error": "缺少 hash 或 base64 图片数据"}

        try:
            cfg = getattr(self.plugin, "plugin_config", None)
            categories = list(getattr(cfg, "categories", []) or getattr(self.plugin, "categories", []) or [])
            cat, tags, desc, _, scenes = await proc.classify_image(
                event=None,
                file_path=file_path,
                categories=categories,
                content_filtration=False,
            )
            if cat == getattr(proc, "CATEGORY_FILTERED", None):
                return {"success": False, "error": "图片内容审核不通过"}
            if not cat:
                return {"success": False, "error": "无法识别图片分类"}
            return {"success": True, "category": cat, "tags": tags or [], "description": desc or "", "desc": desc or "", "scenes": scenes or []}
        finally:
            if tmp_file:
                try:
                    os.unlink(tmp_file)
                except Exception:
                    pass

    async def delete_category(self, key: str) -> dict:
        key = str(key or "").strip()
        if not key:
            return {"success": False, "error": "分类Key无效"}

        cfg = getattr(self.plugin, "plugin_config", None)
        cur_cats = list(getattr(cfg, "categories", []) or getattr(self.plugin, "categories", []) or [])
        cur_cats = [str(c) for c in cur_cats]
        if key not in cur_cats:
            return {"success": False, "error": "分类不存在"}
        if len(cur_cats) <= 1:
            return {"success": False, "error": "至少需要保留1个分类"}

        index = dict(await self.load_index())
        hashes = {
            str(m.get("hash") or Path(p).stem)
            for p, m in index.items()
            if isinstance(m, dict) and str(m.get("category") or "") == key
        }
        deleted = await self.delete_hashes(hashes) if hashes else 0

        updated = [c for c in cur_cats if c != key]
        if hasattr(self.plugin, "_update_config_from_dict"):
            self.plugin._update_config_from_dict({"categories": updated})
        elif cfg is not None:
            cfg.categories = updated
            if hasattr(self.plugin, "categories"):
                self.plugin.categories = updated

        if cfg is not None and hasattr(cfg, "category_info"):
            try:
                if key in getattr(cfg, "category_info", {}):
                    del cfg.category_info[key]
                if hasattr(cfg, "save_category_info"):
                    cfg.save_category_info()
            except Exception as e:
                logger.warning(f"[StealerWebUI] 保存分类信息失败: {e}")

        base_dir = Path(getattr(self.plugin, "base_dir", "")) if getattr(self.plugin, "base_dir", None) else None
        cat_dir = base_dir / "categories" / key if base_dir else None
        try:
            if cat_dir and cat_dir.exists():
                await asyncio.to_thread(shutil.rmtree, cat_dir, True)
        except Exception as e:
            logger.warning(f"[StealerWebUI] 删除原版分类目录失败 {cat_dir}: {e}")

        return {"success": True, "deleted": key, "categories": updated, "deleted_files": deleted}



class WebUIRunner:
    def __init__(self, host:str, port:int, password:str, stealer_data_dir:Path, release_occupied_port:bool=True, protect_original_data: bool=True, allow_destructive_operations: bool=False, backup_on_write: bool=True, source_plugin: Any | None=None):
        self.host=host; self.port=port; self.password=password; self.source_plugin=source_plugin; self.bridge=OriginalStealerBridge(source_plugin) if source_plugin else None; self.store=StealerDataStore(stealer_data_dir, protect_original_data=protect_original_data, allow_destructive_operations=allow_destructive_operations, backup_on_write=backup_on_write); self.release_occupied_port=release_occupied_port
        if self.bridge:
            logger.info("[StealerWebUI] 已连接原版 astrbot_plugin_stealer 实例，写操作将通过原版服务执行")
        else:
            logger.warning("[StealerWebUI] 未找到原版 astrbot_plugin_stealer 实例，将使用数据文件 fallback 模式执行可支持的读写操作")
        self.loop=None; self.runner=None; self.site=None; self.thread=None; self._started=threading.Event(); self.batch_upload_tasks={}

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
        return web.FileResponse(Path(__file__).parent/"web"/"index.html")
    async def handle_login(self, request): raise web.HTTPFound("/")
    async def handle_login_api(self, request):
        data=await request.json()
        if data.get("password")==self.password:
            r=_json_response({"success":True}); r.set_cookie("stealer_webui_token", self._token(), httponly=True, samesite="Lax"); return r
        return _json_response({"success":False,"error":"密码错误"})
    async def handle_images(self, request):
        if (r:=self._need_auth(request)): return r
        q=request.query
        if self.bridge:
            return _json_response(await self.bridge.list_images(int(q.get("page",1)), int(q.get("size",50)), q.get("category") or None, q.get("q",""), q.get("sort","newest")))
        return _json_response(self.store.list_images(int(q.get("page",1)), int(q.get("size",50)), q.get("category") or None, q.get("q",""), q.get("sort","newest")))

    async def handle_image_data(self, request):
        if (r:=self._need_auth(request)): return r
        h=request.query.get("hash","")
        if self.bridge:
            p,_=await self.bridge.find_by_hash(h)
        else:
            p,_=self.store.find_by_hash(h)
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
        if self.bridge:
            return _json_response(await self.bridge.stats())
        idx=self.store.load_index(); today=datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp(); return _json_response({"success":True,"stats":{"total":len(idx),"categories":len(self.store.get_category_keys()),"today":sum(1 for m in idx.values() if int(m.get("created_at",0) or 0)>=today)}})
    async def handle_categories(self, request):
        if (r:=self._need_auth(request)): return r
        if request.method=="POST":
            data=await request.json()
            cats=data.get("categories", data if isinstance(data, list) else [])
            return _json_response({"success":True,"categories":self.store.save_categories(cats)})
        if self.bridge:
            return _json_response(await self.bridge.categories())
        cats={k:0 for k in self.store.get_category_keys()}
        for m in self.store.load_index().values(): cats[str(m.get("category","unknown"))]=cats.get(str(m.get("category","unknown")),0)+1
        return _json_response({"success":True,"categories":cats})
    async def handle_emotions(self, request):
        if (r:=self._need_auth(request)): return r
        if self.bridge:
            return _json_response({"success":True,"emotions":self.bridge.category_info()})
        return _json_response({"success":True,"emotions":self.store.get_category_info()})
    async def handle_health(self, request): return _json_response({"success":True,"status":"ok","service":"emoji-manager-webui"})
    async def handle_update(self, request):
        if (r:=self._need_auth(request)): return r
        data=await request.json()
        if self.bridge:
            ok,err=await self.bridge.update_image(str(data.get("hash","")), data)
        else:
            ok,err=self.store.update_image(str(data.get("hash","")), data)
        return _json_response({"success":ok,"error":err})

    async def handle_delete(self, request):
        if (r:=self._need_auth(request)): return r
        data=await request.json()
        if self.bridge:
            n=await self.bridge.delete_hashes({str(data.get("hash",""))}, bool(data.get("blacklist", False)))
        else:
            n=self.store.delete_hashes({str(data.get("hash",""))}, bool(data.get("blacklist", False)))
        return _json_response({"success":n>0,"count":n,"error":"图片未找到" if n==0 else ""})

    async def handle_batch_delete(self, request):
        if (r:=self._need_auth(request)): return r
        data=await request.json()
        hashes=set(map(str,data.get("hashes",[])))
        blacklist=bool(data.get("blacklist", False))
        n=await self.bridge.delete_hashes(hashes, blacklist) if self.bridge else self.store.delete_hashes(hashes, blacklist)
        return _json_response({"success":True,"count":n})

    async def handle_batch_move(self, request):
        if (r:=self._need_auth(request)): return r
        data=await request.json()
        hashes=set(map(str,data.get("hashes",[])))
        cat=str(data.get("category","unknown"))
        n=await self.bridge.move_hashes(hashes, cat) if self.bridge else self.store.move_hashes(hashes, cat)
        return _json_response({"success":True,"count":n})

    async def handle_batch_scope(self, request):
        if (r:=self._need_auth(request)): return r
        data=await request.json()
        hashes=set(map(str,data.get("hashes",[])))
        if self.bridge:
            u,skipped=await self.bridge.update_scope(hashes, _norm_scope(data.get("scope_mode")))
        else:
            u,skipped=self.store.update_scope(hashes, _norm_scope(data.get("scope_mode")))
        return _json_response({"success":True,"count":u,"skipped":skipped})
    async def _read_upload_request(self, request) -> tuple[list[dict], str, bool]:
        files_data: list[dict] = []
        category = ""
        auto_analyze = False

        ctype = (request.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            data = await request.json()
            category = str(data.get("category") or data.get("emotion") or "").strip()
            auto_analyze = str(data.get("auto_analyze", data.get("autoAnalyze", "false"))).lower() == "true"
            for fi in data.get("_files", []):
                b64 = str(fi.get("base64", ""))
                if "," in b64:
                    b64 = b64.split(",", 1)[1]
                content = base64.b64decode(b64)
                ext = Path(fi.get("name", "upload.png")).suffix.lower() or ".png"
                if ext in ALLOWED_EXTS and content:
                    files_data.append({"filename": fi.get("name", "upload.png"), "content": content, "ext": ext, "hash": hashlib.sha256(content).hexdigest()})
            return files_data, category, auto_analyze

        reader = await request.multipart()
        async for part in reader:
            if part.name in {"category", "emotion"}:
                category = (await part.text()).strip()
                continue
            if part.name in {"auto_analyze", "autoAnalyze"}:
                auto_analyze = (await part.text()).strip().lower() == "true"
                continue
            if part.filename:
                ext = Path(part.filename or "upload.png").suffix.lower() or ".png"
                content = await part.read(decode=False)
                if ext in ALLOWED_EXTS and content:
                    files_data.append({"filename": part.filename or "upload.png", "content": content, "ext": ext, "hash": hashlib.sha256(content).hexdigest()})
        return files_data, category, auto_analyze

    async def handle_upload(self, request):
        if (r:=self._need_auth(request)): return r
        try:
            files_data, category, _ = await self._read_upload_request(request)
            if not files_data:
                return _json_response({"success": False, "error": "没有上传有效的图片文件"})
            fd = files_data[0]
            if self.bridge:
                img = await self.bridge.persist_upload(fd["content"], fd["ext"], category, file_hash=fd["hash"])
            else:
                img = self.store.persist_upload(fd["content"], fd["ext"], category, file_hash=fd["hash"])
            return _json_response({"success": True, "image": img, "hash": img["hash"], "category": img.get("category"), "duplicate": bool(img.get("duplicate"))})
        except Exception as e:
            logger.error(f"[StealerWebUI] 上传图片失败: {e}", exc_info=True)
            return _json_response({"success": False, "error": str(e)})

    async def handle_batch_upload(self, request):
        if (r:=self._need_auth(request)): return r
        try:
            files_data, category, auto_analyze = await self._read_upload_request(request)
            if not files_data:
                return _json_response({"success": False, "error": "没有上传有效的图片文件"})
            fallback = category or ((self.bridge.category_keys()[0] if self.bridge and self.bridge.category_keys() else None) or (self.store.get_category_keys()[0] if self.store.get_category_keys() else "unknown"))
            task_id = str(uuid.uuid4())
            self.batch_upload_tasks[task_id] = {"status": "processing", "total": len(files_data), "processed": 0, "success": 0, "failed": 0, "results": []}
            asyncio.create_task(self._process_batch_upload(task_id, files_data, category, auto_analyze, fallback))
            return _json_response({"success": True, "task_id": task_id, "total": len(files_data)})
        except Exception as e:
            logger.error(f"[StealerWebUI] 批量上传失败: {e}", exc_info=True)
            return _json_response({"success": False, "error": str(e)})

    async def _process_batch_upload(self, task_id: str, files_data: list[dict], category: str, auto_analyze: bool, fallback: str):
        task = self.batch_upload_tasks.get(task_id)
        if not task:
            return
        try:
            for fd in files_data:
                try:
                    final_cat = category or fallback
                    tags: list[str] = []
                    desc = ""
                    scenes: list[str] = []

                    if auto_analyze and self.bridge:
                        b64 = "data:" + _mime(Path("x" + fd["ext"])) + ";base64," + base64.b64encode(fd["content"]).decode()
                        analyzed = await self.bridge.analyze_image(img_base64=b64)
                        if analyzed.get("success"):
                            final_cat = str(analyzed.get("category") or final_cat)
                            tags = _split_csv(analyzed.get("tags", []))
                            desc = str(analyzed.get("description") or analyzed.get("desc") or "")
                            scenes = _split_csv(analyzed.get("scenes", []))

                    if self.bridge:
                        img = await self.bridge.persist_upload(fd["content"], fd["ext"], final_cat, tags=tags, desc=desc, scenes=scenes, file_hash=fd["hash"])
                    else:
                        img = self.store.persist_upload(fd["content"], fd["ext"], final_cat, tags=tags, desc=desc, scenes=scenes, file_hash=fd["hash"])
                    task["results"].append({"hash": img["hash"], "category": img.get("category"), "success": True, "duplicate": bool(img.get("duplicate"))})
                    task["success"] += 1
                except Exception as e:
                    logger.error(f"[StealerWebUI] 处理上传文件 {fd.get('filename')} 失败: {e}")
                    task["results"].append({"filename": fd.get("filename"), "success": False, "error": str(e)})
                    task["failed"] += 1
                task["processed"] += 1
            task["status"] = "completed"
        except Exception as e:
            task["status"] = "failed"
            task["error"] = str(e)

    async def handle_batch_status(self, request):
        if (r:=self._need_auth(request)): return r
        task_id = request.query.get("task_id", "").strip()
        task = self.batch_upload_tasks.get(task_id)
        if not task:
            return _json_response({"success": False, "error": "任务不存在或已过期"})
        return _json_response({
            "success": True,
            "task_id": task_id,
            "status": task.get("status"),
            "total": task.get("total", 0),
            "processed": task.get("processed", 0),
            "success_count": task.get("success", 0),
            "failed_count": task.get("failed", 0),
            "error": task.get("error", ""),
            "results": task.get("results", []),
        })

    async def handle_analyze(self, request):
        if (r:=self._need_auth(request)): return r
        try:
            data = await request.json()
            img_hash = str(data.get("hash", "") or "").strip()
            img_base64 = str(data.get("base64", "") or "").strip()
            if not self.bridge:
                return _json_response({"success": False, "error": "图片处理服务不可用"})
            return _json_response(await self.bridge.analyze_image(img_hash=img_hash, img_base64=img_base64))
        except Exception as e:
            logger.error(f"[StealerWebUI] VLM分析失败: {e}", exc_info=True)
            return _json_response({"success": False, "error": f"分析失败: {e}"})
    async def handle_delete_category(self, request):
        if (r:=self._need_auth(request)): return r
        data = await request.json()
        key = str(data.get("key", "")).strip()
        if self.bridge:
            return _json_response(await self.bridge.delete_category(key))
        return _json_response(self.store.delete_category(key))

    def _is_addr_in_use(self, e: OSError) -> bool:
        return getattr(e, "errno", None) == 98 or "address already in use" in str(e).lower()

    def _find_available_port(self, start: int, end: int) -> int | None:
        """从 [start, end) 范围找一个可用端口，返回 None 表示全部占用"""
        import socket
        for p in range(start, end):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind((self.host, p))
                    return p
                except OSError:
                    continue
        return None

    async def _start_server(self):
        self.runner=web.AppRunner(self._create_app()); await self.runner.setup()
        self.site=web.TCPSite(self.runner,self.host,self.port)
        try:
            await self.site.start()
        except OSError as e:
            if not self._is_addr_in_use(e):
                raise
            if self.release_occupied_port:
                # 不杀进程，自动找可用端口
                new_port = self._find_available_port(self.port + 1, self.port + 100)
                if new_port is None:
                    raise OSError(f"端口 {self.port} 被占用，且 {self.port+1}-{self.port+99} 范围内无可用端口。请在配置中修改 webui_port。") from e
                logger.warning(f"[StealerWebUI] 端口 {self.port} 被占用，自动切换到 {new_port}")
                self.port = new_port
                self.site=web.TCPSite(self.runner,self.host,self.port)
                await self.site.start()
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
        source_plugin=self._resolve_stealer_plugin(context)
        data_dir=Path(getattr(source_plugin, "base_dir")) if source_plugin and getattr(source_plugin, "base_dir", None) else self._resolve_stealer_data_dir()
        if not data_dir or not data_dir.exists(): logger.error("[StealerWebUI] 无法找到 Stealer 数据目录，WebUI 未启动"); return
        host=config.get("webui_host","0.0.0.0"); port=int(config.get("webui_port",9191)); password=config.get("webui_password","") or os.environ.get("STEALER_WEBUI_PASSWORD","")
        protect_original_data=bool(config.get("protect_original_data", False))
        allow_destructive_operations=bool(config.get("allow_destructive_operations", True))
        backup_on_write=bool(config.get("backup_on_write", True))
        self._webui_runner=WebUIRunner(host,port,password,Path(data_dir),bool(config.get("release_occupied_port",True)),protect_original_data=protect_original_data,allow_destructive_operations=allow_destructive_operations,backup_on_write=backup_on_write,source_plugin=source_plugin)
        if self._webui_runner.start_in_thread():
            global _active_webui_server; _active_webui_server=self._webui_runner
    def _looks_like_stealer_plugin(self, obj: Any) -> bool:
        if obj is self or obj is None:
            return False
        return bool(
            getattr(obj, "plugin_config", None) is not None
            and getattr(obj, "cache_service", None) is not None
            and (hasattr(obj, "_load_index") or hasattr(obj, "db_service"))
        )

    def _resolve_stealer_plugin(self, context: Context):
        seen: set[int] = set()
        found = []

        def walk(obj: Any, depth: int = 0):
            if obj is None or depth > 4:
                return
            oid = id(obj)
            if oid in seen:
                return
            seen.add(oid)
            if self._looks_like_stealer_plugin(obj):
                found.append(obj)
                return
            if isinstance(obj, dict):
                for v in obj.values():
                    walk(v, depth + 1)
                return
            if isinstance(obj, (list, tuple, set)):
                for v in obj:
                    walk(v, depth + 1)
                return
            for name in ("stars", "plugins", "star_map", "plugin_map", "loaded_plugins", "_stars", "_plugins", "_star_map", "_plugin_map", "star_manager", "plugin_manager"):
                try:
                    if hasattr(obj, name):
                        walk(getattr(obj, name), depth + 1)
                except Exception:
                    pass
            try:
                if hasattr(obj, "__dict__"):
                    for k, v in vars(obj).items():
                        if "stealer" in str(k).lower() or k in {"context", "star_manager", "plugin_manager"}:
                            walk(v, depth + 1)
            except Exception:
                pass

        walk(context)
        if found:
            obj = found[0]
            logger.info(f"[StealerWebUI] 找到原版 Stealer 插件实例: {obj.__class__.__module__}.{obj.__class__.__name__}")
            return obj
        logger.warning("[StealerWebUI] 未找到原版 astrbot_plugin_stealer 实例，将使用数据文件 fallback 模式")
        return None

    def _resolve_stealer_data_dir(self):
        configured=self.config.get("stealer_data_dir","")
        if configured and Path(configured).exists(): return Path(configured)
        try:
            from astrbot.api.star import StarTools
            for name in ("astrbot_plugin_stealer", PLUGIN_NAME):
                p=Path(StarTools.get_data_dir(name))
                if p.exists() and ((p/"categories").exists() or (p/"cache").exists() or (p/"emoji.db").exists()):
                    return p
            cur=Path(StarTools.get_data_dir())
            for name in ("astrbot_plugin_stealer", PLUGIN_NAME):
                sib=cur.parent/name
                if sib.exists() and ((sib/"categories").exists() or (sib/"cache").exists() or (sib/"emoji.db").exists()):
                    return sib
        except Exception as e: logger.warning(f"[StealerWebUI] 自动推断目录失败: {e}")
        for name in ("astrbot_plugin_stealer", PLUGIN_NAME):
            for p in [Path("data/plugin_data")/name, Path.home()/"AstrBot/data/plugin_data"/name]:
                if p.exists() and ((p/"categories").exists() or (p/"cache").exists() or (p/"emoji.db").exists()):
                    return p
        return None
    async def terminate(self):
        global _active_webui_server
        with _active_webui_lock:
            if _active_webui_server == self._webui_runner: _active_webui_server=None
        if self._webui_runner and self._webui_runner.loop:
            try: asyncio.run_coroutine_threadsafe(self._webui_runner._stop_server(), self._webui_runner.loop).result(timeout=5)
            except Exception: pass

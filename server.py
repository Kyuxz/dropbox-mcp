#!/usr/bin/env python3
"""
Dropbox MCP Server
==================
Remote MCP server that gives Claude full access to your Dropbox account
via the Model Context Protocol (Streamable HTTP transport).

Required environment variables — set ONE of these two options:

  Option A  (short-lived token from App Console, easiest to start):
    DROPBOX_ACCESS_TOKEN=<your token>

  Option B  (auto-refreshing, recommended for permanent deployments):
    DROPBOX_APP_KEY=<your app key>
    DROPBOX_APP_SECRET=<your app secret>
    DROPBOX_REFRESH_TOKEN=<offline refresh token>
    → Run get_refresh_token.py once to obtain the refresh token.

Optional:
    PORT=8000   (HTTP port, default 8000)
"""

import os
import base64
import difflib
import fnmatch
import concurrent.futures
from typing import Any
import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import (
    FileMetadata,
    FolderMetadata,
    WriteMode,
    SearchOptions,
    SearchOrderBy,
)
from mcp.server.fastmcp import FastMCP

# ─── Server setup ─────────────────────────────────────────────────────────────

mcp = FastMCP(name="dropbox", host="0.0.0.0")


# ─── Dropbox client ───────────────────────────────────────────────────────────

def _dbx() -> dropbox.Dropbox:
    """Return an authenticated Dropbox client, auto-refreshing when possible."""
    app_key     = os.environ.get("DROPBOX_APP_KEY", "")
    app_secret  = os.environ.get("DROPBOX_APP_SECRET", "")
    refresh_tok = os.environ.get("DROPBOX_REFRESH_TOKEN", "")
    access_tok  = os.environ.get("DROPBOX_ACCESS_TOKEN", "")

    if app_key and app_secret and refresh_tok:
        # Preferred: auto-refreshes using app credentials + offline refresh token
        return dropbox.Dropbox(
            app_key=app_key,
            app_secret=app_secret,
            oauth2_refresh_token=refresh_tok,
        )
    elif access_tok:
        return dropbox.Dropbox(access_tok)
    else:
        raise EnvironmentError(
            "No Dropbox credentials found.\n"
            "Set DROPBOX_ACCESS_TOKEN  or  "
            "DROPBOX_APP_KEY + DROPBOX_APP_SECRET + DROPBOX_REFRESH_TOKEN"
        )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _norm(path: str) -> str:
    """Normalise a path for the Dropbox API (root = empty string)."""
    if not path or path.strip("/") == "":
        return ""
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/")


def _human(b: int | float) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


# ─── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def list_files(path: str = "", recursive: bool = False) -> str:
    """
    List files and folders inside a Dropbox folder.
    Use path="" or path="/" for the root. Supports recursive listing.
    """
    dbx = _dbx()
    try:
        r = dbx.files_list_folder(_norm(path), recursive=recursive)
        entries: list[Any] = list(r.entries)
        while r.has_more:
            r = dbx.files_list_folder_continue(r.cursor)
            entries.extend(r.entries)
        if not entries:
            return f"'{path or '/'}' is empty."
        lines = []
        for e in sorted(entries, key=lambda x: (isinstance(x, FileMetadata), x.name.lower())):
            if isinstance(e, FolderMetadata):
                lines.append(f"📁  {e.path_display}/")
            elif isinstance(e, FileMetadata):
                ts = e.client_modified.strftime("%Y-%m-%d %H:%M")
                lines.append(f"📄  {e.path_display}  [{_human(e.size)}, {ts}]")
        return f"{len(entries)} item(s) in '{path or '/'}' :\n" + "\n".join(lines)
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def get_metadata(path: str) -> str:
    """Return detailed metadata for a file or folder (size, dates, ID, rev)."""
    dbx = _dbx()
    try:
        m = dbx.files_get_metadata(_norm(path))
        if isinstance(m, FileMetadata):
            return (
                f"Type    : file\n"
                f"Name    : {m.name}\n"
                f"Path    : {m.path_display}\n"
                f"Size    : {_human(m.size)}\n"
                f"Modified: {m.client_modified}\n"
                f"ID      : {m.id}\n"
                f"Rev     : {m.rev}"
            )
        return (
            f"Type : folder\n"
            f"Name : {m.name}\n"
            f"Path : {m.path_display}\n"
            f"ID   : {m.id}"
        )
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def read_file(path: str) -> str:
    """
    Download and return the content of a file.
    Text/code files are returned as plain text.
    Binary files (images, PDFs, etc.) are returned as a base64 string.
    """
    dbx = _dbx()
    try:
        _, resp = dbx.files_download(_norm(path))
        raw = resp.content
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            b64 = base64.b64encode(raw).decode("ascii")
            return f"[binary file — base64]\n{b64}"
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False}
)
def write_file(path: str, content: str, overwrite: bool = True) -> str:
    """
    Create or update a text file in Dropbox.
    path      : destination path, e.g. /notes/todo.txt
    content   : UTF-8 text to write
    overwrite : replace if file exists (default True); False = auto-rename
    """
    dbx = _dbx()
    try:
        mode = WriteMode.overwrite if overwrite else WriteMode.add
        m = dbx.files_upload(
            content.encode("utf-8"),
            _norm(path),
            mode=mode,
            autorename=not overwrite,
        )
        return f"✅ Saved: {m.path_display}  ({_human(m.size)})"
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False}
)
def create_folder(path: str) -> str:
    """Create a new folder at the given Dropbox path."""
    dbx = _dbx()
    try:
        m = dbx.files_create_folder_v2(_norm(path))
        return f"✅ Created folder: {m.metadata.path_display}"
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": True}
)
def delete(path: str) -> str:
    """
    Permanently delete a file or folder.
    ⚠️  This cannot be undone (though Dropbox version history may help).
    """
    dbx = _dbx()
    try:
        m = dbx.files_delete_v2(_norm(path))
        return f"🗑️  Deleted: {m.metadata.path_display}"
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False}
)
def move(from_path: str, to_path: str, overwrite: bool = False) -> str:
    """Move a file or folder to a new location in Dropbox."""
    dbx = _dbx()
    try:
        m = dbx.files_move_v2(
            _norm(from_path),
            _norm(to_path),
            allow_overwrite=overwrite,
        )
        return f"✅ Moved → {m.metadata.path_display}"
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False}
)
def copy(from_path: str, to_path: str, overwrite: bool = False) -> str:
    """Copy a file or folder to a new location in Dropbox."""
    dbx = _dbx()
    try:
        m = dbx.files_copy_v2(
            _norm(from_path),
            _norm(to_path),
            allow_overwrite=overwrite,
        )
        return f"✅ Copied → {m.metadata.path_display}"
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def search(query: str, path: str = "", max_results: int = 25) -> str:
    """
    Search for files and folders by name or content.
    query       : search string
    path        : scope search to this folder (empty = all of Dropbox)
    max_results : up to 100 (default 25)
    """
    dbx = _dbx()
    try:
        opts = SearchOptions(
            path=_norm(path) or None,
            max_results=min(max_results, 100),
            order_by=SearchOrderBy.last_modified_time,
        )
        r = dbx.files_search_v2(query, options=opts)
        if not r.matches:
            return f"No results for '{query}'."
        lines = [f"{len(r.matches)} result(s) for '{query}':"]
        for match in r.matches:
            meta = match.metadata.get_metadata()
            icon = "📁" if isinstance(meta, FolderMetadata) else "📄"
            lines.append(f"  {icon}  {meta.path_display}")
        return "\n".join(lines)
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def get_storage_info() -> str:
    """Show how much Dropbox storage you have used and how much is free."""
    dbx = _dbx()
    try:
        u = dbx.users_get_space_usage()
        used  = u.used
        alloc = u.allocation
        if alloc.is_individual():
            total = alloc.get_individual().allocated
        elif alloc.is_team():
            total = alloc.get_team().allocated
        else:
            total = 0
        pct   = (used / total * 100) if total else 0
        return (
            f"Used : {_human(used)}  ({pct:.1f} %)\n"
            f"Free : {_human(total - used)}\n"
            f"Total: {_human(total)}"
        )
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def get_account_info() -> str:
    """Return the name, email, and plan of the connected Dropbox account."""
    dbx = _dbx()
    try:
        a = dbx.users_get_current_account()
        return (
            f"Name  : {a.name.display_name}\n"
            f"Email : {a.email}\n"
            f"Plan  : {a.account_type._tag}\n"
            f"Locale: {a.locale}\n"
            f"ID    : {a.account_id}"
        )
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def get_file_versions(path: str, limit: int = 10) -> str:
    """
    List previous versions of a file (Dropbox version history).
    path  : file path
    limit : max versions to return (default 10)
    """
    dbx = _dbx()
    try:
        r = dbx.files_list_revisions(_norm(path), limit=limit)
        if not r.entries:
            return "No version history found."
        lines = [f"Version history for {path}:"]
        for i, rev in enumerate(r.entries, 1):
            ts = rev.client_modified.strftime("%Y-%m-%d %H:%M")
            lines.append(f"  {i}. rev={rev.rev}  size={_human(rev.size)}  modified={ts}")
        return "\n".join(lines)
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def read_multiple_files(paths: list[str], max_chars_per_file: int = 2000) -> str:
    """
    Read many files in ONE call, fetched in parallel (16 threads). Token-cheap:
    each file gets truncated to max_chars_per_file (default 2000), not dumped whole.
    Binary files are skipped (size noted only). One bad path won't kill the rest.
    Use this for scanning dozens/hundreds of vault notes fast and cheap.
    """
    def _fetch(p: str) -> str:
        dbx = _dbx()
        try:
            _, resp = dbx.files_download(_norm(p))
            raw = resp.content
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return f"=== {p} ===\n[binary, {_human(len(raw))}] skipped\n"
            if len(text) > max_chars_per_file:
                text = text[:max_chars_per_file] + f"\n…[truncated, {len(text)} chars total]"
            return f"=== {p} ===\n{text}\n"
        except ApiError as e:
            return f"=== {p} ===\n[error] {e}\n"

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(_fetch, paths))
    return "\n".join(results)


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def directory_tree(path: str = "") -> str:
    """Recursive indented tree of a Dropbox folder. No JSON bloat, cheap on tokens."""
    dbx = _dbx()
    try:
        r = dbx.files_list_folder(_norm(path), recursive=True)
        entries: list[Any] = list(r.entries)
        while r.has_more:
            r = dbx.files_list_folder_continue(r.cursor)
            entries.extend(r.entries)
        if not entries:
            return f"'{path or '/'}' is empty."
        entries.sort(key=lambda e: e.path_lower)
        lines = []
        for e in entries:
            depth = e.path_display.strip("/").count("/")
            tag = "📁" if isinstance(e, FolderMetadata) else "📄"
            lines.append("  " * depth + f"{tag} {e.name}")
        return "\n".join(lines)
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def list_directory_with_sizes(path: str = "", sort_by: str = "name") -> str:
    """List ONE folder (non-recursive), sorted by 'name' or 'size'."""
    dbx = _dbx()
    try:
        r = dbx.files_list_folder(_norm(path), recursive=False)
        entries: list[Any] = list(r.entries)
        while r.has_more:
            r = dbx.files_list_folder_continue(r.cursor)
            entries.extend(r.entries)
        if not entries:
            return f"'{path or '/'}' is empty."
        if sort_by == "size":
            entries.sort(key=lambda e: getattr(e, "size", 0), reverse=True)
        else:
            entries.sort(key=lambda e: e.name.lower())
        lines = []
        for e in entries:
            if isinstance(e, FolderMetadata):
                lines.append(f"📁  {e.name}/  [DIR]")
            else:
                lines.append(f"📄  {e.name}  [{_human(e.size)}]")
        return "\n".join(lines)
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def search_files(path: str = "", pattern: str = "*", max_results: int = 200) -> str:
    """
    Recursive filename glob match (pattern e.g. '*.md'). Client-side exact glob,
    not Dropbox's fuzzy content search (use `search` tool for that).
    """
    dbx = _dbx()
    try:
        r = dbx.files_list_folder(_norm(path), recursive=True)
        entries: list[Any] = list(r.entries)
        while r.has_more:
            r = dbx.files_list_folder_continue(r.cursor)
            entries.extend(r.entries)
        hits = [e for e in entries if fnmatch.fnmatch(e.name.lower(), pattern.lower())][:max_results]
        if not hits:
            return f"No matches for '{pattern}' in '{path or '/'}'."
        lines = [f"{len(hits)} match(es):"]
        for e in hits:
            tag = "📁" if isinstance(e, FolderMetadata) else "📄"
            lines.append(f"  {tag} {e.path_display}")
        return "\n".join(lines)
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
def get_file_info(path: str) -> str:
    """Low-level stat: size, both timestamps, rev, content hash, downloadable flag."""
    dbx = _dbx()
    try:
        m = dbx.files_get_metadata(_norm(path))
        if isinstance(m, FileMetadata):
            return (
                f"Path        : {m.path_display}\n"
                f"Size        : {_human(m.size)} ({m.size} bytes)\n"
                f"Modified    : {m.client_modified}\n"
                f"Server mod  : {m.server_modified}\n"
                f"Rev         : {m.rev}\n"
                f"Content hash: {m.content_hash}\n"
                f"Downloadable: {m.is_downloadable}"
            )
        return f"Path: {m.path_display}\nType: folder\nID  : {m.id}"
    except ApiError as e:
        return f"Error: {e}"


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False}
)
def bulk_edit_files(edits: list[dict], dry_run: bool = False) -> str:
    """
    Edit MANY files at once, in parallel (8 threads) — the 'bulk edit' tool.
    edits = [{"path": "/a.md", "edits": [{"oldText": "...", "newText": "..."}]}, ...]
    Each oldText must match exactly once per file or that file errors out (others still run).
    dry_run=True returns unified diffs without saving anything.
    """
    def _apply(item: dict) -> str:
        p = item["path"]
        dbx = _dbx()
        try:
            _, resp = dbx.files_download(_norm(p))
            original = resp.content.decode("utf-8")
        except ApiError as e:
            return f"=== {p} ===\n[error reading] {e}\n"
        except UnicodeDecodeError:
            return f"=== {p} ===\n[binary, skipped]\n"

        text = original
        for ed in item.get("edits", []):
            old, new = ed["oldText"], ed["newText"]
            if text.count(old) != 1:
                return f"=== {p} ===\n[error] oldText not found exactly once ({text.count(old)} matches)\n"
            text = text.replace(old, new, 1)

        diff = "\n".join(difflib.unified_diff(
            original.splitlines(), text.splitlines(),
            fromfile=p, tofile=p, lineterm=""
        ))

        if dry_run:
            return f"=== {p} (dry run) ===\n{diff or 'no change'}\n"

        try:
            dbx.files_upload(text.encode("utf-8"), _norm(p), mode=WriteMode.overwrite)
            return f"=== {p} (saved) ===\n{diff or 'no change'}\n"
        except ApiError as e:
            return f"=== {p} ===\n[error writing] {e}\n"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_apply, edits))
    return "\n".join(results)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        return JSONResponse({"status": "ok", "server": "dropbox-mcp"})

    port = int(os.environ.get("PORT", 8000))
    print(f"🟢  Dropbox MCP server running on port {port}")
    print(f"    Connector URL → http://0.0.0.0:{port}/mcp")

    asgi_app = mcp.streamable_http_app()
    uvicorn.run(asgi_app, host="0.0.0.0", port=port)

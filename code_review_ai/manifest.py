"""Per-file manifest: rel_path -> (mtime, size, file_hash), persisted in the
`files` table. Used to detect changed/added/deleted files for incremental
node/edge updates without re-parsing everything."""

import hashlib


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read(conn) -> dict[str, tuple[float, int, str]]:
    return {r["path"]: (r["mtime"], r["size"], r["file_hash"])
            for r in conn.execute("SELECT path,mtime,size,file_hash FROM files")}


def update(conn, entries: dict[str, tuple[float, int, str]]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO files(path,mtime,size,file_hash) VALUES(?,?,?,?)",
        [(path, mtime, size, file_hash)
         for path, (mtime, size, file_hash) in entries.items()])


def remove(conn, paths: list[str]) -> None:
    if paths:
        conn.executemany("DELETE FROM files WHERE path=?", [(p,) for p in paths])

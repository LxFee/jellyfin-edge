"""Node-owned bounded disk cache. Complete objects only; no media mount required."""
import asyncio
import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path


class DiskCache:
    def __init__(self, root, max_bytes):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes = max_bytes
        self.db = sqlite3.connect(self.root / 'index.sqlite')
        self.db.execute('CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, size INTEGER NOT NULL, used REAL NOT NULL)')
        self.db.commit()
        self.locks = {}
        self.generation = 0
        self.inventory = 0
        self.hits = self.misses = 0
        # Recover a commit interrupted between the rename and index transaction.
        for p in self.root.glob('*.blob'):
            if len(p.stem) == 64 and not p.is_symlink():
                self.db.execute('INSERT OR IGNORE INTO objects VALUES (?,?,?)', (p.stem, p.stat().st_size, p.stat().st_mtime))
        for p in self.root.glob('*.tmp'):
            p.unlink(missing_ok=True)
        self.db.commit()
        self._trim()

    @staticmethod
    def digest(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def contains(self, key, expected=None):
        ident = self.digest(key)
        row = self.db.execute('SELECT size FROM objects WHERE id=?', (ident,)).fetchone()
        p = self.root / (ident + '.blob')
        return bool(row and p.is_file() and (expected is None or row[0] == expected) and p.stat().st_size == row[0])

    def read(self, key, expected=None):
        ident = self.digest(key)
        if not self.contains(key, expected):
            return None
        try:
            data = (self.root / (ident + '.blob')).read_bytes()
        except FileNotFoundError:
            return None
        self.db.execute('UPDATE objects SET used=? WHERE id=?', (time.time(), ident))
        self.db.commit()
        self.hits += 1
        return data

    async def get(self, key, fetch, expected=None, limit=64 * 1024 * 1024):
        lock, users = self.locks.get(key, (asyncio.Lock(), 0))
        self.locks[key] = (lock, users + 1)
        try:
            async with lock:
                hit = self.read(key, expected)
                if hit is not None:
                    return hit
                self.misses += 1
                generation = self.generation
                data = await fetch()
                if not isinstance(data, bytes) or len(data) > limit or (expected is not None and len(data) != expected):
                    raise ValueError('incomplete or oversized cache fill')
                if generation == self.generation and len(data) <= self.max_bytes:
                    ident = self.digest(key)
                    temporary = self.root / (ident + '.' + secrets.token_hex(8) + '.tmp')
                    try:
                        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, self.root / (ident + '.blob'))
                        self.db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?)', (ident, len(data), time.time()))
                        self.db.commit()
                        self.inventory += 1
                        self._trim()
                    finally:
                        temporary.unlink(missing_ok=True)
                return data
        finally:
            _, users = self.locks[key]
            if users == 1:
                del self.locks[key]
            else:
                self.locks[key] = (lock, users - 1)

    def _trim(self):
        total = self.db.execute('SELECT COALESCE(SUM(size),0) FROM objects').fetchone()[0]
        if total <= self.max_bytes:
            return
        for ident, size in self.db.execute('SELECT id,size FROM objects ORDER BY used,id').fetchall():
            if total <= self.max_bytes:
                break
            (self.root / (ident + '.blob')).unlink(missing_ok=True)
            self.db.execute('DELETE FROM objects WHERE id=?', (ident,))
            self.inventory += 1
            total -= size
        self.db.commit()

    def clear(self):
        self.generation += 1
        self.inventory += 1
        for ident, in self.db.execute('SELECT id FROM objects').fetchall():
            (self.root / (ident + '.blob')).unlink(missing_ok=True)
        self.db.execute('DELETE FROM objects')
        self.db.commit()

    def configure(self, max_bytes):
        self.max_bytes = max_bytes
        self._trim()

    def close(self):
        self.db.close()

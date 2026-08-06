"""
aic_archive.py — Copilot のローカル利用実績を「追記専用」で永続化する。

背景
----
`~/.copilot/session-store.db` は Copilot 本体が管理する DB で、保持期間や
ユーザー操作で中身が消える。ダッシュボードがこれを直接集計すると、
DB を消した時点で過去の消費履歴も失われてしまう。

そこで本モジュールが、ライブ DB から読み取った行を独立した
アーカイブ DB へマージ（追記）する。以後の集計はアーカイブ側を参照する。

    session-store.db ──(read-only)──▶ archive.db ──▶ usage.json ──▶ index.html
       消えてよい                       消してはいけない資産

キー設計（重要）
----------------
assistant_usage_events.id は INTEGER PRIMARY KEY AUTOINCREMENT。
DB を作り直すと 1 から振り直されるため、id 単独をキーにすると
別世代の別イベントが既存行を上書き破壊する。

session_id (TEXT UUID) を足しても万全ではない。セッションを復元・再開すると
同じ session_id が新しい世代の DB に現れうるからで、そのとき採番済みの id と
衝突すると過去の実績が黙って書き潰される（復旧不能）。

そこで **(session_id, id, created_at) の複合キー** を採用する。
created_at はイベント生成時刻でありミリ秒精度・不変。

  - 同一 DB を再スキャン             → 3 つとも一致 → 冪等
  - DB 再作成後に id が再利用された  → created_at が違う → 別行として保存

内容ハッシュ方式は、行が僅かに更新されただけで別キーになり二重計上を
招くため採用しない（不変な identity 列だけを使うのが要点）。

created_at が取れない行は安全にキーを作れないため quarantine_events に隔離し、
集計から除外したうえで件数を報告する（黙って捨てない）。

このモジュールは archive.db の実績行に対して DELETE / DROP を一切実行しない。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3

# ライブ DB から取り込む列。assistant_usage_events のスキーマに対応する。
EVENT_COLUMNS = [
    "id", "session_id", "turn_index", "agent_id", "parent_tool_call_id", "model",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "total_nano_aiu", "request_multiplier", "duration_ms",
    "time_to_first_token_ms", "inter_token_latency_ms", "initiator", "api_endpoint",
    "reasoning_effort", "finish_reason", "content_filter_triggered",
    "token_details_json", "created_at",
]

# キーを構成する不変列。ここ以外は「更新されうる属性」として扱う。
IDENTITY_COLUMNS = ("session_id", "id", "created_at")

SESSION_COLUMNS = ["id", "cwd", "repository", "branch", "summary", "created_at", "updated_at", "host_type"]

_EVENT_BODY = """
    session_id              TEXT    NOT NULL,
    id                      INTEGER NOT NULL,
    created_at              TEXT    NOT NULL,
    turn_index              INTEGER,
    agent_id                TEXT,
    parent_tool_call_id     TEXT,
    model                   TEXT,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    cache_read_tokens       INTEGER,
    cache_write_tokens      INTEGER,
    reasoning_tokens        INTEGER,
    total_nano_aiu          INTEGER,
    request_multiplier      REAL,
    duration_ms             INTEGER,
    time_to_first_token_ms  INTEGER,
    inter_token_latency_ms  INTEGER,
    initiator               TEXT,
    api_endpoint            TEXT,
    reasoning_effort        TEXT,
    finish_reason           TEXT,
    content_filter_triggered INTEGER,
    token_details_json      TEXT,
    origin                  TEXT,
    first_archived_at       TEXT    NOT NULL,
    last_seen_at            TEXT    NOT NULL,
    PRIMARY KEY (session_id, id, created_at)
"""

ARCHIVE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS usage_events ({_EVENT_BODY});
CREATE INDEX IF NOT EXISTS ix_usage_created  ON usage_events(created_at);
CREATE INDEX IF NOT EXISTS ix_usage_model    ON usage_events(model);
CREATE INDEX IF NOT EXISTS ix_usage_session  ON usage_events(session_id);

-- created_at が取れず安全にキー化できなかった行。捨てずに保管だけする。
CREATE TABLE IF NOT EXISTS quarantine_events (
    quarantined_at TEXT NOT NULL,
    source_path    TEXT,
    reason         TEXT,
    row_json       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    cwd               TEXT,
    repository        TEXT,
    branch            TEXT,
    summary           TEXT,
    created_at        TEXT,
    updated_at        TEXT,
    host_type         TEXT,
    origin            TEXT,
    first_archived_at TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL
);

-- 収集の実行ログ。成功も失敗も必ず 1 行残す。連続性の判定に使う。
CREATE TABLE IF NOT EXISTS collect_runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at            TEXT NOT NULL,
    status            TEXT NOT NULL,          -- ok / source_missing / source_locked / error
    source_path       TEXT,
    source_ident      TEXT,                   -- 同一ファイル世代かの判定用
    source_rows       INTEGER,
    inserted          INTEGER,
    changed           INTEGER,
    existing          INTEGER,
    quarantined       INTEGER,
    live_min_created  TEXT,
    live_max_created  TEXT,
    live_min_id       INTEGER,
    live_max_id       INTEGER,
    archive_max_created_before TEXT,
    origin            TEXT,                   -- NULL = このマシン
    note              TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_dt() -> datetime:
    """ran_at と比較するための naive UTC。ran_at は tz 情報なしで保存されている。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm_ts(v):
    """created_at を比較可能な形（UTC / ミリ秒 / Z 終端）へ正規化する。

    ライブ DB は ISO8601 文字列だが、将来 epoch ミリ秒やオフセット付きが
    混ざっても大小比較が壊れないようにしておく。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        sec = v / 1000.0 if v > 10_000_000_000 else float(v)
        return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    s = str(v).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s  # 解釈できないものはそのまま（少なくとも同値比較は成立する）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------- 排他制御
class ProcessLock:
    """手動実行とタスクスケジューラ実行の同時走行を防ぐ。

    SQLite 自体は WAL で壊れないが、統計値の取り違えや usage.js の
    半端な上書きを避けたいのでアプリ側でも直列化する。
    """

    STALE_SECONDS = 900

    def __init__(self, path: Path):
        self.path = Path(path)
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(60):  # 最大 30 秒待つ
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {utcnow()}".encode())
                os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue
                if age > self.STALE_SECONDS:
                    try:      # 異常終了した実行の置き土産とみなして奪う
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                time.sleep(0.5)
        raise TimeoutError(f"他の収集プロセスが実行中です: {self.path}")

    def __exit__(self, *exc):
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


# --------------------------------------------------------------------------- ライブ DB
def source_identity(path: Path):
    """ファイル世代の識別子。作り直されると変わる。

    st_size は普通の書き込みでも増減するので識別子に含めない。
    含めるとファイルが育っただけで「作り直された」と誤判定し、
    ギャップを不当に high へ格上げしてしまう。

    Windows では st_ino / st_dev が 0 になる環境があるため、その場合は
    None を返し、呼び出し側で「世代を判定できない」として扱う。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    ino = getattr(st, "st_ino", 0)
    dev = getattr(st, "st_dev", 0)
    if not ino:
        return None
    return f"{dev}:{ino}"


# 一時的で再試行に意味があるのはロック競合だけ。
# スキーマ変更（no such table 等）は何度試しても直らないので即座に区別する。
_RETRYABLE = ("database is locked", "database table is locked", "database schema is locked")


def _is_retryable(exc: sqlite3.Error) -> bool:
    if isinstance(exc, sqlite3.OperationalError):
        return any(m in str(exc).lower() for m in _RETRYABLE)
    return False


class UnsupportedSchema(Exception):
    """ライブ DB のスキーマが想定と違う（Copilot 側の変更が疑われる）。"""


def connect_readonly(path: Path, attempts: int = 5, probe: str = "assistant_usage_events"):
    """ライブ DB を読み取り専用で開く。開けなければ None を返す。

    Copilot が書き込み中でロックされることがあるので指数バックオフで再試行する。
    db/-wal/-shm を個別にコピーする方式は採らない。逐次コピーの間に
    チェックポイントが走ると、異なる時点のファイルが混ざった不整合な
    スナップショットになりうるため。

    probe は「開けたか」を確かめるために触るテーブル名。参照先の DB ごとに
    期待するテーブルが違うので呼び出し側から差し替えられるようにしてある。
    """
    delay = 0.5
    last = None
    for i in range(attempts):
        con = None
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
            con.execute("PRAGMA query_only=ON")
            con.execute(f'SELECT 1 FROM "{probe}" LIMIT 1')
            return con
        except sqlite3.Error as e:
            if con is not None:
                con.close()
            last = e
            if not _is_retryable(e):
                # 再試行しても直らない類。ロック扱いにすると誤った対処を促すので分ける。
                raise UnsupportedSchema(str(e)) from e
            if i < attempts - 1:
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
    print(f"[warn] ライブ DB がロックされています（{attempts} 回試行）: {last}", file=sys.stderr)
    return None


def _meta_get(con: sqlite3.Connection, key: str):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def open_archive(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path.as_posix(), timeout=60, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")   # 途中で電源断してもアーカイブを壊さない
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA busy_timeout=60000")
    con.executescript(ARCHIVE_SCHEMA)
    migrate(con)
    # このアーカイブが「どのマシンのものか」を一度だけ刻む。
    # 他マシンへ持ち込んで --merge-archive したときの既定ラベルになる。
    if not _meta_get(con, "machine_label"):
        _meta_set(con, "machine_label", _hostname())
    return con


def _hostname() -> str:
    import socket
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def check_integrity(con: sqlite3.Connection) -> str:
    return con.execute("PRAGMA integrity_check").fetchone()[0]


# --------------------------------------------------------------------------- マイグレーション
_COLLECT_RUN_COLUMNS = {
    "status": "TEXT", "source_ident": "TEXT", "changed": "INTEGER", "existing": "INTEGER",
    "quarantined": "INTEGER", "live_min_id": "INTEGER", "live_max_id": "INTEGER",
    "updated": "INTEGER",   # v1 の列名。読み出し互換のため残す
    "origin": "TEXT",
    # v4: スケジュール実行か手作業かの区別。NULL = この列より前の行（不明）。
    # 収集間隔を実測するときに手作業ぶんを混ぜないために要る。列名を trigger に
    # しないのは SQLite の予約語で、引用符なしの ALTER / INSERT が落ちるため。
    "run_trigger": "TEXT",
}


def _ensure_columns(con: sqlite3.Connection, table: str, spec: dict) -> None:
    """旧バージョンで作られたテーブルに不足列を足す（既存データは触らない）。"""
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, typ in spec.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def migrate(con: sqlite3.Connection) -> None:
    """schema_version 1 (PK: session_id,id) → 2 (PK: session_id,id,created_at)。

    旧テーブルは DROP せず usage_events_v1 として残す（追記専用の原則）。
    """
    _ensure_columns(con, "collect_runs", _COLLECT_RUN_COLUMNS)
    con.execute("UPDATE collect_runs SET status='ok' WHERE status IS NULL")
    # v3: 他マシンのアーカイブを取り込めるようにする。NULL = このマシン。
    # 既存行は NULL のままでよい（このマシンで集めたものだから）。
    _ensure_columns(con, "usage_events", {"origin": "TEXT"})
    _ensure_columns(con, "sessions", {"origin": "TEXT"})

    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    ver = int(row[0]) if row else 1
    pk_cols = [r[1] for r in con.execute("PRAGMA table_info(usage_events)") if r[5]]
    if pk_cols == ["session_id", "id", "created_at"]:
        if ver < SCHEMA_VERSION:
            con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
        return

    print(f"[info] アーカイブを schema v{ver} → v{SCHEMA_VERSION} へ移行します（既存行は保持）")
    old_cols = [r[1] for r in con.execute("PRAGMA table_info(usage_events)")]
    rest = [c for c in old_cols if c != "created_at"]

    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(f"CREATE TABLE usage_events_v2 ({_EVENT_BODY})")
        con.execute(
            f"""INSERT OR IGNORE INTO usage_events_v2 (created_at, {', '.join(rest)})
                SELECT COALESCE(created_at, ''), {', '.join(rest)} FROM usage_events"""
        )
        moved = con.execute("SELECT COUNT(*) FROM usage_events_v2").fetchone()[0]
        con.execute("ALTER TABLE usage_events RENAME TO usage_events_v1")
        con.execute("ALTER TABLE usage_events_v2 RENAME TO usage_events")
        con.execute("CREATE INDEX IF NOT EXISTS ix_usage_created ON usage_events(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_usage_model   ON usage_events(model)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_usage_session ON usage_events(session_id)")
        con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    print(f"[info] 移行完了: {moved:,} 行（旧テーブルは usage_events_v1 として保全）")


# --------------------------------------------------------------------------- マージ
def _log_run(arc: sqlite3.Connection, **kw) -> None:
    cols = ", ".join(kw)
    marks = ", ".join("?" * len(kw))
    arc.execute(f"INSERT INTO collect_runs ({cols}) VALUES ({marks})", tuple(kw.values()))


def merge(live: sqlite3.Connection, arc: sqlite3.Connection, source_path: str,
          run_trigger: str = "manual") -> dict:
    now = utcnow()
    live.row_factory = sqlite3.Row

    # 読み取りは 1 トランザクションで完結させ、一貫したスナップショットを見る
    live.execute("BEGIN")
    live_session_cols = {r[1] for r in live.execute("PRAGMA table_info(sessions)")}
    scols = [c for c in SESSION_COLUMNS if c in live_session_cols]
    srows = [dict(r) for r in live.execute(f"SELECT {', '.join(scols)} FROM sessions")] if scols else []

    live_event_cols = {r[1] for r in live.execute("PRAGMA table_info(assistant_usage_events)")}
    ecols = [c for c in EVENT_COLUMNS if c in live_event_cols]
    erows = [dict(r) for r in live.execute(f"SELECT {', '.join(ecols)} FROM assistant_usage_events")]
    live.execute("COMMIT")

    good, bad = [], []
    for r in erows:
        ts = _norm_ts(r.get("created_at"))
        if ts is None or r.get("session_id") is None or r.get("id") is None:
            bad.append(r)
        else:
            r["created_at"] = ts
            good.append(r)

    ident = source_identity(Path(source_path))

    # ---- ここから書き込み。統計を正しく取るため最初に write lock を取る ----
    arc.execute("BEGIN IMMEDIATE")
    try:
        # 欠測判定の基準はこのマシン分だけで取る。他マシンの新しいイベントが
        # 混ざると、ローカルの本当の欠測が「既に記録済み」に見えて隠れてしまう。
        before_max = arc.execute(
            "SELECT MAX(created_at) FROM usage_events WHERE origin IS NULL").fetchone()[0]
        before_count = arc.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]

        if scols and srows:
            supd = ", ".join(f"{c}=excluded.{c}" for c in scols if c != "id")
            swhere = " OR ".join(f"sessions.{c} IS NOT excluded.{c}" for c in scols if c != "id")
            arc.executemany(
                f"""INSERT INTO sessions ({', '.join(scols)}, first_archived_at, last_seen_at)
                    VALUES ({', '.join('?' * (len(scols) + 2))})
                    ON CONFLICT(id) DO UPDATE SET {supd}, last_seen_at=excluded.last_seen_at
                    WHERE {swhere}""",
                [tuple(r[c] for c in scols) + (now, now) for r in srows],
            )

        attr_cols = [c for c in ecols if c not in IDENTITY_COLUMNS]
        eupd = ", ".join(f"{c}=excluded.{c}" for c in attr_cols)
        ewhere = " OR ".join(f"usage_events.{c} IS NOT excluded.{c}" for c in attr_cols)
        # 実質同一の行は書き換えない。毎時の全行 UPDATE による WAL 肥大を避ける。
        sql = (
            f"INSERT INTO usage_events ({', '.join(ecols)}, first_archived_at, last_seen_at) "
            f"VALUES ({', '.join('?' * (len(ecols) + 2))}) "
            f"ON CONFLICT(session_id, id, created_at) DO UPDATE SET "
            f"{eupd}, last_seen_at=excluded.last_seen_at WHERE {ewhere}"
        )
        t0 = arc.total_changes
        arc.executemany(sql, [tuple(r[c] for c in ecols) + (now, now) for r in good])
        touched = arc.total_changes - t0

        after_count = arc.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        inserted = after_count - before_count
        changed = max(touched - inserted, 0)
        existing = len(good) - inserted - changed

        for r in bad:
            arc.execute(
                "INSERT INTO quarantine_events (quarantined_at, source_path, reason, row_json)"
                " VALUES (?,?,?,?)",
                (now, source_path, "missing identity column (session_id/id/created_at)",
                 json.dumps(r, ensure_ascii=False, default=str)),
            )

        lo = min((r["created_at"] for r in good), default=None)
        hi = max((r["created_at"] for r in good), default=None)
        ids = [r["id"] for r in good]

        _log_run(
            arc, ran_at=now, status="ok", source_path=source_path, source_ident=ident,
            source_rows=len(erows), inserted=inserted, changed=changed, existing=existing,
            quarantined=len(bad), live_min_created=lo, live_max_created=hi,
            live_min_id=min(ids, default=None), live_max_id=max(ids, default=None),
            archive_max_created_before=before_max,
            run_trigger=run_trigger,
        )
        arc.execute("COMMIT")
    except Exception:
        arc.execute("ROLLBACK")
        raise

    return {
        "source_rows": len(erows), "inserted": inserted, "changed": changed,
        "existing": existing, "quarantined": len(bad), "archive_rows": after_count,
        "live_min": lo, "live_max": hi, "archive_max_before": before_max,
    }


def log_failed_run(arc: sqlite3.Connection, status: str, source_path: str, note: str = "",
                   run_trigger: str = "manual") -> None:
    """取り込めなかった実行も必ず残す。空白期間の判定に必要。"""
    arc.execute("BEGIN IMMEDIATE")
    try:
        before_max = arc.execute(
            "SELECT MAX(created_at) FROM usage_events WHERE origin IS NULL").fetchone()[0]
        _log_run(arc, ran_at=utcnow(), status=status, source_path=source_path,
                 source_ident=source_identity(Path(source_path)),
                 archive_max_created_before=before_max, note=note,
                 run_trigger=run_trigger)
        arc.execute("COMMIT")
    except Exception:
        arc.execute("ROLLBACK")
        raise


def merge_archive(dst: sqlite3.Connection, src_path: Path, origin: str = "") -> dict:
    """別マシンのアーカイブを取り込む（追記のみ）。

    二重計上が起きない理由: usage_events の主キーは (session_id, id, created_at)
    で origin を含まない。同じイベントが両方に居れば INSERT OR IGNORE で
    片方だけが残る。つまり同じアーカイブを何度取り込んでも合計は増えない。

    既存行の UPDATE / DELETE は一切しない。取り込んだ行だけに origin を付ける。

    取り込み元が既に別マシン分を含んでいる場合（A ← B ← C）、その origin は
    そのまま保つ。上書きすると C の実績が B のものに化け、マシン別内訳も
    欠測判定も壊れる。新しいラベルを付けるのは origin が NULL の行
    （= 取り込み元自身が集めた行）だけ。
    """
    if not src_path.exists():
        raise FileNotFoundError(f"アーカイブが見つかりません: {src_path}")
    dst_path = Path(dst.execute("PRAGMA database_list").fetchone()[2])
    same = src_path.resolve() == dst_path.resolve()
    if not same:
        try:
            same = os.path.samefile(src_path, dst_path)   # ハードリンク / 別名パス
        except OSError:
            pass
    if same:
        raise ValueError("取り込み元と取り込み先が同じファイルです")

    src = connect_readonly(src_path, probe="usage_events")
    try:
        src.row_factory = sqlite3.Row
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "usage_events" not in tables:
            raise ValueError(f"アーカイブ形式ではありません: {src_path}")

        label = origin.strip()
        if not label and "meta" in tables:
            row = src.execute("SELECT value FROM meta WHERE key='machine_label'").fetchone()
            label = (row[0] if row else "") or ""
        label = label or src_path.stem
        if label == (_meta_get(dst, "machine_label") or ""):
            # 同名だと「このマシン」と見分けが付かない。取り違えを避けて止める。
            raise ValueError(
                f"origin '{label}' はこのマシンのラベルと同じです。--origin で別名を指定してください"
            )

        # 取り込み元も 1 トランザクションで読む。読んでいる最中に相手が
        # 収集を回すと、イベントと収集ログが別スナップショットになりかねない。
        src.execute("BEGIN")
        try:
            dst_ecols = {r[1] for r in dst.execute("PRAGMA table_info(usage_events)")}
            src_ecols = [r[1] for r in src.execute("PRAGMA table_info(usage_events)")]
            missing = [c for c in ("session_id", "id", "created_at", "total_nano_aiu")
                       if c not in src_ecols]
            if missing:
                raise ValueError(
                    f"取り込み元に必要な列がありません: {', '.join(missing)} ({src_path})"
                )
            has_src_origin = "origin" in src_ecols
            ecols = [c for c in src_ecols if c in dst_ecols and c != "origin"]
            sel = ecols + (["origin"] if has_src_origin else [])
            erows = [tuple(r[c] for c in ecols) + ((r["origin"] if has_src_origin else None) or label,)
                     for r in src.execute(f"SELECT {', '.join(sel)} FROM usage_events")]

            srows, scols = [], []
            if "sessions" in tables:
                dst_scols = {r[1] for r in dst.execute("PRAGMA table_info(sessions)")}
                src_scols = [r[1] for r in src.execute("PRAGMA table_info(sessions)")]
                has_so = "origin" in src_scols
                scols = [c for c in src_scols if c in dst_scols and c != "origin"]
                if scols:
                    ssel = scols + (["origin"] if has_so else [])
                    srows = [tuple(r[c] for c in scols) + ((r["origin"] if has_so else None) or label,)
                             for r in src.execute(f"SELECT {', '.join(ssel)} FROM sessions")]

            rruns, rcols = [], []
            has_runs = "collect_runs" in tables
            if has_runs:
                dst_rcols = {r[1] for r in dst.execute("PRAGMA table_info(collect_runs)")}
                src_rcols = [r[1] for r in src.execute("PRAGMA table_info(collect_runs)")]
                # run_id は AUTOINCREMENT なので持ち込むと衝突する。採番し直す。
                rcols = [c for c in src_rcols if c in dst_rcols and c not in ("run_id", "origin")]
                rsel = rcols + (["origin"] if "origin" in src_rcols else [])
                rruns = [dict(r) for r in src.execute(f"SELECT {', '.join(rsel)} FROM collect_runs")]

            quarantined = 0
            if "quarantine_events" in tables:
                quarantined = src.execute("SELECT COUNT(*) FROM quarantine_events").fetchone()[0]
        finally:
            src.execute("COMMIT")
    finally:
        src.close()

    dst.execute("BEGIN IMMEDIATE")
    try:
        before = dst.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        sess_before = dst.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        if erows:
            dst.executemany(
                f"INSERT OR IGNORE INTO usage_events ({', '.join(ecols)}, origin) "
                f"VALUES ({', '.join('?' * (len(ecols) + 1))})",
                erows,
            )
        if srows:
            dst.executemany(
                f"INSERT OR IGNORE INTO sessions ({', '.join(scols)}, origin) "
                f"VALUES ({', '.join('?' * (len(scols) + 1))})",
                srows,
            )

        runs_added = 0
        origins_seen = set()
        for r in rruns:
            # 取り込み元が持ち込んだ origin をそのまま尊重する（A ← B ← C）。
            org = r.get("origin") or label
            origins_seen.add(org)
            dup = dst.execute(
                "SELECT 1 FROM collect_runs WHERE origin IS ? AND ran_at IS ? "
                "AND status IS ? AND source_path IS ? LIMIT 1",
                (org, r.get("ran_at"), r.get("status"), r.get("source_path")),
            ).fetchone()
            if dup:
                continue
            cols = [c for c in rcols if c in r]
            dst.execute(
                f"INSERT INTO collect_runs ({', '.join(cols)}, origin) "
                f"VALUES ({', '.join('?' * (len(cols) + 1))})",
                tuple(r[c] for c in cols) + (org,),
            )
            runs_added += 1

        after = dst.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        sess_after = dst.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        _meta_set(dst, f"merged_from:{label}", utcnow())
        dst.execute("COMMIT")
    except Exception:
        dst.execute("ROLLBACK")
        raise

    inserted = after - before
    return {
        "origin": label, "source_events": len(erows), "inserted": inserted,
        "skipped": len(erows) - inserted, "sessions": sess_after - sess_before,
        "runs_added": runs_added, "archive_rows": after,
        "has_runs": has_runs, "source_quarantined": quarantined,
        "origins": sorted(origins_seen | {o for o in
                          (r[-1] for r in erows) if o}),
    }


# --------------------------------------------------------------------------- 連続性の判定
def detect_gaps(arc: sqlite3.Connection) -> list:
    """記録が途切れた「可能性のある」期間を返す。

    重要な限界: イベントの時刻だけでは「DB が消された」のか
    「単に使っていなかった」のかを断定できない。そこで
      - ライブ DB のファイル世代が変わった（作り直された）
      - 前回アーカイブの最新より新しい所からしかライブ DB に残っていない
    の 2 つを手掛かりに *疑い* として報告する。確定した欠測とは呼ばない。

    confidence:
      high … ソースが作り直され、かつ時刻も不連続（取りこぼしの可能性が高い）
      low  … 時刻が不連続なだけ（単に使っていなかった可能性が高い）

    複数マシンのアーカイブを取り込んでいる場合、origin を混ぜて時系列に
    並べると source_ident が毎行入れ替わり、全実行が「作り直された」と
    誤判定される。必ず origin ごとに独立して判定する。
    """
    runs = arc.execute(
        """SELECT run_id, ran_at, status, source_ident, live_min_created,
                  archive_max_created_before, origin
           FROM collect_runs ORDER BY run_id"""
    ).fetchall()

    by_origin = {}
    for r in runs:
        by_origin.setdefault(r[6], []).append(r)

    out = []
    for origin, group in by_origin.items():
        out.extend(_gaps_for_origin(arc, origin, group))
    out.sort(key=lambda g: (g["from"], g["origin"] or ""))
    return out


def _gaps_for_origin(arc: sqlite3.Connection, origin, group: list) -> list:
    gaps = []
    prev_ident = None
    seen_first = False
    for _rid, ran_at, status, ident, live_min, arc_max, _org in group:
        replaced = bool(prev_ident and ident and ident != prev_ident)
        if ident:
            prev_ident = ident
        if status != "ok":
            continue
        if not seen_first:
            seen_first = True          # 初回は比較対象がないので判定しない
            continue
        if not live_min or not arc_max:
            continue
        lo, hi = _norm_ts(arc_max), _norm_ts(live_min)
        if not lo or not hi or hi <= lo:
            continue
        covered = arc.execute(
            """SELECT COUNT(*) FROM usage_events
               WHERE created_at > ? AND created_at < ? AND origin IS ?""",
            (lo, hi, origin),
        ).fetchone()[0]
        gaps.append({
            "from": lo, "to": hi, "detected_at": ran_at, "origin": origin,
            "confidence": "high" if replaced else "low",
            "source_replaced": replaced,
            "archived_in_range": covered,
        })

    merged = []
    for g in gaps:                      # 連続する重複を畳む
        if merged and g["from"] <= merged[-1]["to"]:
            last = merged[-1]
            last["to"] = max(last["to"], g["to"])
            last["archived_in_range"] += g["archived_in_range"]
            if g["source_replaced"]:
                last["source_replaced"] = True
                last["confidence"] = "high"
        else:
            merged.append(dict(g))
    return merged


def machines(arc: sqlite3.Connection) -> list:
    """origin ごとのイベント数・期間・最終収集時刻。

    取り込んだ他マシンの分は「その時点のスナップショット」でしかない。
    以後もそのマシンは消費し続けるので、最終収集がいつだったかを必ず
    添える。これが無いと、古いスナップショットを最新の全体像だと
    誤解させることになる。
    """
    rows = arc.execute(
        """SELECT origin, COUNT(*), MIN(created_at), MAX(created_at),
                  ROUND(SUM(COALESCE(total_nano_aiu,0)) / 1e9, 1)
           FROM usage_events GROUP BY origin ORDER BY 2 DESC"""
    ).fetchall()
    label = _meta_get(arc, "machine_label")
    out = []
    for r in rows:
        last = arc.execute(
            "SELECT ran_at, status FROM collect_runs WHERE origin IS ? "
            "ORDER BY run_id DESC LIMIT 1", (r[0],)
        ).fetchone()
        last_ok = arc.execute(
            "SELECT MAX(ran_at) FROM collect_runs WHERE origin IS ? AND status='ok'", (r[0],)
        ).fetchone()[0]
        out.append({
            "origin": r[0] or label or "this machine", "is_local": r[0] is None,
            "events": r[1], "since": r[2], "until": r[3], "aic": r[4] or 0.0,
            "last_run_at": last[0] if last else None,
            "last_run_status": last[1] if last else None,
            "last_ok_at": last_ok,
            "merged_at": _meta_get(arc, f"merged_from:{r[0]}") if r[0] else None,
        })
    return out


def coverage(arc: sqlite3.Connection) -> dict:
    row = arc.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM usage_events").fetchone()
    runs = arc.execute("SELECT COUNT(*) FROM collect_runs WHERE origin IS NULL").fetchone()[0]
    failed = arc.execute(
        "SELECT COUNT(*) FROM collect_runs WHERE origin IS NULL AND status<>'ok'").fetchone()[0]
    qn = arc.execute("SELECT COUNT(*) FROM quarantine_events").fetchone()[0]
    # 「直近の収集」は必ずこのマシンの実行を指す。取り込んだ他マシンの
    # 実行ログが後から入ると、ローカルの失敗を隠したり、成功しているのに
    # 失敗と表示したりしてしまう。
    last = arc.execute(
        "SELECT ran_at, status FROM collect_runs WHERE origin IS NULL "
        "ORDER BY run_id DESC LIMIT 1").fetchone()
    local = arc.execute(
        "SELECT MIN(created_at) FROM usage_events WHERE origin IS NULL").fetchone()[0]
    # 「データがどこまで新しいか」は最後に成功した取り込みで決まる。
    # 集計は取り込みが失敗しても走るので、出力の生成時刻とは別物。
    last_ok = arc.execute(
        "SELECT MAX(ran_at) FROM collect_runs WHERE origin IS NULL AND status='ok'").fetchone()[0]
    return {
        "events": row[0], "since": row[1], "until": row[2],
        "local_since": local,
        "runs": runs, "failed_runs": failed, "quarantined": qn,
        "last_run_at": last[0] if last else None,
        "last_run_status": last[1] if last else None,
        "last_ok_at": last_ok,
        "gaps": detect_gaps(arc),
        "machines": machines(arc),
    }


# --------------------------------------------------------------------------- エクスポート
def collect_cadence(arc: sqlite3.Connection, samples: int = 12):
    """このマシンの収集が実際に何分おきに走っているか（間隔の中央値）。

    ダッシュボードの「集計が古い」判定に使う。固定のしきい値だと収集間隔を
    変えた瞬間に必ずどちらかが誤る。1 時間おきなら 90 分経過は異常だが、
    3 時間おきなら 90 分は正常。実測すればどちらの設定でも正しく言える。

    スケジュール実行だけを見る。手作業の実行は間隔がばらばらで、しかも
    調べものをしている最中に固まって走るので、混ぜると測りたい「普段の
    間隔」ではなく「いま何回叩いたか」を測ってしまう。

    run_trigger が付く前の行しかない場合（この列より古いアーカイブ、または
    スケジュールを更新していない人）は、全実行にフォールバックする。粗いが、
    何も出さないよりは「集計が古い」の判定に使える。ただし混ざった値を
    「自動収集の間隔」と名乗らせてはいけないので、source で区別できるようにする。

    中央値を使うのは、それでも手動実行の連打や PC を落としていた期間が
    混ざりうるため。平均は後者ひとつで簡単に壊れる。

    失敗した実行も数える。測りたいのは「タスクが何分おきに起動するか」で
    あって「何分おきに成功したか」ではない。成功だけを見ると、毎時タスクが
    一回おきに失敗しているときに「2 時間おきの設定なのだ」と学習してしまい、
    障害に合わせて鮮度のしきい値まで緩む。取り込めているかどうかは
    coverage() の last_ok_at で別に見る。

    返り値: {"minutes": float, "source": ...} または None
      source は次の 3 つ。UI はこれで名乗り方を変える。
        "scheduled" — スケジュール実行だけから測れている。間隔を名乗ってよい
        "measuring" — 自動収集は動いているが標本がまだ足りない
        "unknown"   — スケジュール実行の記録が無い、または古い（未登録 / 旧タスク / 停止）
    """
    # 旧アーカイブには run_trigger が無い。--no-archive は読み取り専用で開くので
    # _ensure_columns() が走らず、問い合わせると no such column で落ちる。
    cols = {r[1] for r in arc.execute("PRAGMA table_info(collect_runs)")}
    has_trigger = "run_trigger" in cols

    def _rows(where: str) -> list:
        rows = arc.execute(
            "SELECT ran_at FROM collect_runs "
            f"WHERE origin IS NULL AND {where} "
            "ORDER BY ran_at DESC LIMIT ?", (samples + 1,)
        ).fetchall()
        times = []
        for (ts,) in rows:
            try:
                times.append(datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                continue
        return times

    def _gaps(times: list, lo: float) -> list:
        out = []
        for a, b in zip(times, times[1:]):
            mins = (a - b).total_seconds() / 60.0
            if lo <= mins <= 1440.0:      # 連打と長期停止を除く
                out.append(mins)
        return out

    def _median(gaps: list) -> float:
        gaps.sort()
        mid = len(gaps) // 2
        return round(gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2, 1)

    sched = []
    if has_trigger:
        # スケジュール実行に連打は無いので下限は 1 分未満でよい。
        # タスク側は 1 分間隔を許しているのに 2 分で切ると永久に測れなくなる。
        sched = _rows("run_trigger = 'scheduled'")
        gaps = _gaps(sched, 0.5)
        if len(gaps) >= 3:
            minutes = _median(gaps)
            # 止めた / 作り直したタスクの間隔を「いまの自動収集」と言い続けない。
            stale_after = max(3 * minutes, 1440.0)
            age = (utcnow_dt() - sched[0]).total_seconds() / 60.0
            if age <= stale_after:
                return {"minutes": minutes, "source": "scheduled"}

    # ここに来るのは、標本が足りないか、最後のスケジュール実行が古い場合。
    # 「登録し直した直後で溜まっている最中」と「そもそも自動収集が動いて
    # いない」は、利用者にとって次にやることが違うので区別する。
    fresh_sched = bool(sched) and (utcnow_dt() - sched[0]).total_seconds() <= 86400.0
    source = "measuring" if fresh_sched else "unknown"

    gaps = _gaps(_rows("1 = 1"), 2.0)
    if len(gaps) < 3:
        return None
    return {"minutes": _median(gaps), "source": source}


def export_csv(arc: sqlite3.Connection, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    cur = arc.execute("SELECT * FROM usage_events ORDER BY created_at")
    names = [d[0] for d in cur.description]
    n = 0
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(names)
        for row in cur:
            w.writerow(row)
            n += 1
    os.replace(tmp, out)
    return n


def backup(arc_path: Path, dest_dir: Path) -> Path:
    """SQLite のオンラインバックアップ API で安全に複製する。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"archive-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(arc_path.as_posix())
    dst = sqlite3.connect(dest.as_posix())
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dest


# --------------------------------------------------------------------------- 設定
def reconcile(arc: sqlite3.Connection, app_path: Path) -> dict:
    """Copilot App が自前で持つ集計 (data.db) とアーカイブを突き合わせる。

    アーカイブの入力元 (session-store.db) と data.db は別々に書かれるので、
    片方だけに存在するセッションは収集の取りこぼしを示す独立した証拠になる。
    data.db は Copilot App の内部データであり、存在も列構成も保証されない。
    見つからない・列が無い場合は「検算できない」として素直に諦める。
    """
    if not app_path.exists():
        return {"available": False, "reason": f"{app_path} が見つかりません"}

    try:
        app = connect_readonly(app_path, probe="sessions")
    except UnsupportedSchema as exc:
        return {"available": False, "reason": f"読み取れません: {exc}"}
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"読み取れません: {exc}"}
    if app is None:
        return {"available": False, "reason": "ロックされていて読み取れませんでした"}

    try:
        cols = {r[1] for r in app.execute("PRAGMA table_info(sessions)")}
        if not cols:
            return {"available": False, "reason": "sessions テーブルがありません"}
        if "total_nano_aiu" not in cols:
            return {"available": False,
                    "reason": "sessions.total_nano_aiu がありません（App のバージョン差）"}

        title_col = "title" if "title" in cols else "NULL"
        created_col = "created_at" if "created_at" in cols else "NULL"
        rows = app.execute(
            f"SELECT id, COALESCE(total_nano_aiu, 0), {created_col}, {title_col} FROM sessions"
        ).fetchall()
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"クエリに失敗しました: {exc}"}
    finally:
        app.close()

    # data.db はこのマシンの Copilot App が書くもの。突き合わせ相手は
    # このマシンで集めた分だけに限る。他マシンのイベントを混ぜると、
    # 相手側の古い/新しい記録が範囲判定と in_flight 判定を狂わせる。
    arc_lo = arc.execute(
        "SELECT MIN(created_at) FROM usage_events WHERE origin IS NULL").fetchone()[0]
    arc_hi = arc.execute(
        "SELECT MAX(created_at) FROM usage_events WHERE origin IS NULL").fetchone()[0]

    # 「アーカイブに無い＝取りこぼし」と言えるのは、収集を実際に回し始めた
    # 後に始まったセッションだけ。初回収集は、その時点でライブ DB に残って
    # いた分をまとめて取り込むので、アーカイブの最古イベントは収集開始より
    # ずっと古くなる。その区間の欠落は、ライブ DB 側が初回収集より前に
    # 刈り取っていた可能性と区別できないため、証拠にならない。
    try:
        started = arc.execute(
            "SELECT MIN(ran_at) FROM collect_runs WHERE status = 'ok' AND origin IS NULL"
        ).fetchone()[0]
    except sqlite3.Error:
        started = None
    covered_from = started or arc_lo

    matched, missing = [], []
    for sid, nano, created, title in rows:
        aic = (nano or 0) / 1e9
        got = arc.execute(
            "SELECT COALESCE(SUM(total_nano_aiu), 0) / 1e9, COUNT(*), MAX(created_at) "
            "FROM usage_events WHERE session_id = ? AND origin IS NULL",
            (sid,),
        ).fetchone()
        if got[1]:
            # 最後に収集した時点まで動いていたセッションは、収集後も消費が
            # 増え続けるので「アーカイブが少ない」のは当たり前。取りこぼしと
            # 区別できないと毎回誤警告になるため、進行中として扱う。
            live = bool(arc_hi and got[2] and got[2] >= arc_hi[:13])
            matched.append({"id": sid, "app_aic": aic, "archive_aic": got[0],
                            "delta": got[0] - aic, "created_at": created,
                            "title": title, "in_flight": live})
        else:
            # 収集開始より前に始まったセッションは「取りこぼし」ではなく範囲外。
            before = bool(covered_from and created and str(created) < covered_from)
            missing.append({"id": sid, "app_aic": aic, "created_at": created,
                            "title": title, "before_archive": before})

    # アーカイブが App より多いのは正常。アーカイブは sub-agent / compaction を
    # 含む生の消費イベントを全部数えるのに対し、App 側のセッション合計は
    # そのセッションが直接回した分しか持たないことがあるため。
    # 逆にアーカイブの方が少ない場合だけが「取りこぼし」の証拠になる。
    short = sorted((m for m in matched if m["delta"] <= -1.0 and not m["in_flight"]),
                   key=lambda m: m["delta"])
    in_flight = [m for m in matched if m["in_flight"] and m["delta"] <= -1.0]

    return {
        "available": True,
        "app_db": str(app_path),
        "app_sessions": len(rows),
        "app_aic": sum(r[1] or 0 for r in rows) / 1e9,
        "matched": matched,
        "missing": missing,
        "missing_in_range": [m for m in missing if not m["before_archive"]],
        "short": short,
        "in_flight": in_flight,
        "archive_start": arc_lo,
        "covered_from": covered_from,
    }


def print_reconcile(rep: dict) -> None:
    if not rep.get("available"):
        print(f"[skip] 検算できません: {rep.get('reason')}")
        print("       これは異常ではありません。data.db は Copilot App の内部データで、")
        print("       CLI 単体で使っている場合は存在しません。")
        return

    matched, missing = rep["matched"], rep["missing"]
    in_range, short = rep["missing_in_range"], rep["short"]
    arc_total = sum(m["archive_aic"] for m in matched)
    app_total = sum(m["app_aic"] for m in matched)

    print(f"検算元: {rep['app_db']}")
    print(f"  App 側 {rep['app_sessions']} セッション / 合計 {rep['app_aic']:,.1f} AIC")
    print(f"  アーカイブと一致  : {len(matched)} セッション"
          f"（App {app_total:,.0f} AIC / アーカイブ {arc_total:,.0f} AIC）")
    print(f"  アーカイブに無い  : {len(missing)} セッション"
          f"（うち収集開始前 {len(missing) - len(in_range)} 件）")
    if rep.get("covered_from"):
        print(f"  取りこぼし判定の対象は {str(rep['covered_from'])[:16]} 以降のセッションです。")
        print("  （それ以前は初回収集時にライブ DB へ残っていた分だけなので、"
              "無くても取りこぼしとは断定できません）")

    problems = bool(short or in_range)

    if short:
        print(f"\n  [warn] アーカイブの方が少ないセッション: {len(short)} 件")
        print("         これは収集の取りこぼしを示します。")
        for m in short[:10]:
            print(f"    {str(m['created_at'])[:16]}  App {m['app_aic']:>9,.0f}"
                  f" / archive {m['archive_aic']:>9,.0f}  差 {m['delta']:+,.0f}")
        if len(short) > 10:
            print(f"    ... 他 {len(short) - 10} 件")

    if in_range:
        print(f"\n  [warn] 収集開始後なのに 1 件も記録が無いセッション: {len(in_range)} 件")
        for m in sorted(in_range, key=lambda m: m["app_aic"], reverse=True)[:10]:
            t = (m["title"] or "")[:34]
            print(f"    {str(m['created_at'])[:16]}  {m['app_aic']:>9,.0f} AIC  {t}")
        print("    直近のセッションは、まだ収集していないだけの可能性があります。")
        print("    python aic_collect.py を実行してから再確認してください。")

    if not problems:
        print("\n  [ok] 取りこぼしの兆候はありません。")

    if rep.get("in_flight"):
        print(f"\n  進行中とみなしたセッション: {len(rep['in_flight'])} 件")
        print("         最後の収集時点でまだ動いていたため、その後の消費が"
              "アーカイブに入っていないだけです。")

    if arc_total > app_total:
        print(f"\n  参考: アーカイブが App より {arc_total - app_total:,.0f} AIC 多く出ています。")
        print("        アーカイブは sub-agent / compaction の消費も個別に数えるのに対し、")
        print("        App 側のセッション合計はそれを含まないことがあるためで、異常ではありません。")

    print("\n  注意: この検算は CLI 実行分どうしの突き合わせです。")
    print("        Copilot Coding Agent / Code Review はサーバー側で動くため、")
    print("        どちらの DB にも消費が記録されず、検算対象外です。")


def load_config(root: Path) -> dict:
    cfg_path = root / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    # config.local.json は gitignore 済み。共有する config.json を汚さずに
    # 自分の環境固有のパスだけを上書きするために使う。
    local_path = root / "config.local.json"
    if local_path.exists():
        cfg.update(json.loads(local_path.read_text(encoding="utf-8")))
    # 優先順位: 環境変数 > config.local.json > config.json > 既定値
    # 既定値は ~/.copilot-aic/ に置く。~/.copilot 配下だと Copilot 側の
    # 掃除に巻き込まれてアーカイブごと消えかねないため、意図的に分ける。
    env = os.environ.get("AIC_ARCHIVE_DB")
    if env:
        cfg["archive_db"] = env
    # config.json に null が書かれている場合も既定値へ倒す
    if not cfg.get("archive_db"):
        cfg["archive_db"] = str(Path.home() / ".copilot-aic" / "archive.db")
    if not cfg.get("source_db"):
        cfg["source_db"] = str(Path.home() / ".copilot" / "session-store.db")
    # Copilot App が自前で集計している DB。検算にだけ使う（--reconcile）。
    if not cfg.get("app_db"):
        cfg["app_db"] = str(Path.home() / ".copilot" / "data.db")
    cfg["archive_db"] = os.path.expandvars(os.path.expanduser(cfg["archive_db"]))
    cfg["app_db"] = os.path.expandvars(os.path.expanduser(cfg["app_db"]))
    return cfg


def ingest(src_path: Path, arc: sqlite3.Connection, quiet: bool = False,
           run_trigger: str = "manual"):
    """ライブ DB → アーカイブ。失敗しても例外を投げず、実行ログだけ残す。"""
    if not src_path.exists():
        if not quiet:
            print(f"[warn] ライブ DB が見つかりません: {src_path}")
            print("       アーカイブ済みのデータのみで集計します。")
        log_failed_run(arc, "source_missing", str(src_path), run_trigger=run_trigger)
        return None

    try:
        live = connect_readonly(src_path)
    except UnsupportedSchema as e:
        # Copilot 側の内部スキーマは非公開で、予告なく変わりうる。
        # ロック扱いにすると「待てば直る」という誤った期待を与えるので分ける。
        if not quiet:
            print(f"[warn] ライブ DB のスキーマが想定と異なります: {e}", file=sys.stderr)
            print("       Copilot 側の内部形式が変わった可能性があります。", file=sys.stderr)
            print("       アーカイブ済みのデータは無事です。ツールの更新を確認してください。", file=sys.stderr)
        log_failed_run(arc, "unsupported_schema", str(src_path), str(e)[:500], run_trigger)
        return None

    if live is None:
        if not quiet:
            print("[warn] ライブ DB がロックされています。今回の取り込みは見送ります。")
        log_failed_run(arc, "source_locked", str(src_path), "read-only connect failed", run_trigger)
        return None
    try:
        return merge(live, arc, str(src_path), run_trigger)
    except Exception as e:      # 取り込み失敗でも集計は継続させる
        log_failed_run(arc, "error", str(src_path), repr(e)[:500], run_trigger)
        print(f"[warn] 取り込みに失敗しました: {e}", file=sys.stderr)
        return None
    finally:
        live.close()


def main() -> int:
    root = Path(__file__).resolve().parent
    cfg = load_config(root)

    ap = argparse.ArgumentParser(description="Copilot 利用実績を追記専用アーカイブへ取り込む")
    ap.add_argument("--source", default=cfg["source_db"], help="ライブ DB のパス")
    ap.add_argument("--archive", default=cfg["archive_db"], help="アーカイブ DB のパス")
    ap.add_argument("--export-csv", metavar="PATH", help="アーカイブ全件を CSV 出力して終了")
    ap.add_argument("--backup-to", metavar="DIR", help="アーカイブを複製して終了")
    ap.add_argument("--stats", action="store_true", help="アーカイブの統計だけ表示して終了")
    ap.add_argument("--reconcile", action="store_true",
                    help="Copilot App の集計 (data.db) と突き合わせて取りこぼしを検出し終了")
    ap.add_argument("--app-db", default=cfg["app_db"],
                    help="--reconcile が参照する Copilot App の DB")
    ap.add_argument("--merge-archive", metavar="PATH",
                    help="別マシンのアーカイブ DB を取り込む（追記のみ）")
    ap.add_argument("--origin", default="",
                    help="--merge-archive で付けるマシン名（既定: 取り込み元の machine_label）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    arc_path = Path(args.archive)
    src_path = Path(args.source)

    # 読み取り専用の操作でアーカイブを新規作成してはいけない。
    # パスを間違えたまま --backup-to が「成功」すると、空 DB を保存して
    # 履歴を守れたと誤解させることになる。
    # --merge-archive も同様に、取り込み先を間違えたまま空 DB を作らせない。
    needs_existing_archive = bool(args.export_csv or args.backup_to or args.stats
                                  or args.reconcile or args.merge_archive)
    if needs_existing_archive and not arc_path.exists():
        print(f"[error] アーカイブ DB がありません: {arc_path}", file=sys.stderr)
        print("        パスが正しいか確認してください。", file=sys.stderr)
        print("        まだ作成していない場合は、先に python aic_archive.py を実行してください。",
              file=sys.stderr)
        return 1

    arc = open_archive(arc_path)
    try:
        ok = check_integrity(arc)
        if ok != "ok":
            print(f"[FATAL] アーカイブ DB が破損しています: {arc_path}\n  integrity_check => {ok}",
                  file=sys.stderr)
            return 2

        if args.export_csv:
            n = export_csv(arc, Path(args.export_csv))
            print(f"[ok] CSV 出力: {args.export_csv} ({n:,} 行)")
            return 0

        if args.backup_to:
            dest = backup(arc_path, Path(args.backup_to))
            print(f"[ok] バックアップ: {dest}")
            return 0

        if args.reconcile:
            rep = reconcile(arc, Path(args.app_db))
            print_reconcile(rep)
            # 検算できない・範囲外の欠落は異常ではないので exit 0 のまま。
            # 取りこぼしの兆候があるときだけ 3 で知らせる（1/2 は既存の意味と衝突させない）。
            suspect = bool(rep.get("missing_in_range") or rep.get("short"))
            return 3 if suspect else 0

        if args.stats:
            cov = coverage(arc)
            aic = arc.execute("SELECT COALESCE(SUM(total_nano_aiu),0) FROM usage_events").fetchone()[0] / 1e9
            print(f"アーカイブ: {arc_path}")
            print(f"  {cov['events']:,} イベント / {cov['since']} 〜 {cov['until']} / 合計 {aic:,.1f} AIC")
            print(f"  収集 {cov['runs']} 回（失敗 {cov['failed_runs']}）"
                  f" / 最終 {cov['last_run_at']} [{cov['last_run_status']}]")
            if cov["quarantined"]:
                print(f"  [!] 隔離 {cov['quarantined']} 行（created_at 欠損）")
            if len(cov["machines"]) > 1:
                print("  マシン別:")
                for m in cov["machines"]:
                    print(f"    {m['origin']:<20} {m['events']:>8,} イベント / {m['aic']:>10,.1f} AIC")
            for g in cov["gaps"]:
                who = f" [{g['origin']}]" if g.get("origin") else ""
                print(f"  [gap:{g['confidence']}]{who} {g['from']} 〜 {g['to']}")
            return 0

        if args.merge_archive:
            with ProcessLock(arc_path.with_suffix(".lock")):
                res = merge_archive(arc, Path(args.merge_archive), args.origin)
            print(f"[ok] 取り込み元 '{res['origin']}': 新規 {res['inserted']:,} / "
                  f"重複 {res['skipped']:,}（元 {res['source_events']:,} 行）")
            print(f"     セッション {res['sessions']:,} / 収集ログ {res['runs_added']:,} 行を追加")
            if len(res["origins"]) > 1:
                print(f"     取り込んだマシン: {', '.join(res['origins'])}")
            if not res["has_runs"]:
                print("     [!] 取り込み元に collect_runs がありません。"
                      "そのマシン分は欠測を判定できません。")
            if res["source_quarantined"]:
                print(f"     [!] 取り込み元に隔離行が {res['source_quarantined']:,} 件あります"
                      "（集計対象外なので取り込んでいません）")
            print(f"     アーカイブ累計: {res['archive_rows']:,} イベント")
            print("     注意: 取り込んだのはその時点のスナップショットです。"
                  "相手のマシンはこの後も消費し続けます。")
            return 0

        with ProcessLock(arc_path.with_suffix(".lock")):
            st = ingest(src_path, arc, args.quiet)

        if st and not args.quiet:
            print(f"[ok] 取り込み: 新規 {st['inserted']:,} / 変更 {st['changed']:,} "
                  f"/ 既存 {st['existing']:,}（ライブ {st['source_rows']:,} 行）")
            print(f"     アーカイブ累計: {st['archive_rows']:,} イベント → {arc_path}")
            if st["quarantined"]:
                print(f"     [!] 隔離 {st['quarantined']} 行")
            for g in detect_gaps(arc):
                print(f"     [gap:{g['confidence']}] {g['from']} 〜 {g['to']} の記録がありません")
        return 0
    finally:
        arc.close()


if __name__ == "__main__":
    raise SystemExit(main())

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

SCHEMA_VERSION = 2

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


def connect_readonly(path: Path, attempts: int = 5):
    """ライブ DB を読み取り専用で開く。開けなければ None を返す。

    Copilot が書き込み中でロックされることがあるので指数バックオフで再試行する。
    db/-wal/-shm を個別にコピーする方式は採らない。逐次コピーの間に
    チェックポイントが走ると、異なる時点のファイルが混ざった不整合な
    スナップショットになりうるため。
    """
    delay = 0.5
    last = None
    for i in range(attempts):
        con = None
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
            con.execute("PRAGMA query_only=ON")
            con.execute("SELECT 1 FROM assistant_usage_events LIMIT 1")
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


def open_archive(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path.as_posix(), timeout=60, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")   # 途中で電源断してもアーカイブを壊さない
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA busy_timeout=60000")
    con.executescript(ARCHIVE_SCHEMA)
    migrate(con)
    return con


def check_integrity(con: sqlite3.Connection) -> str:
    return con.execute("PRAGMA integrity_check").fetchone()[0]


# --------------------------------------------------------------------------- マイグレーション
_COLLECT_RUN_COLUMNS = {
    "status": "TEXT", "source_ident": "TEXT", "changed": "INTEGER", "existing": "INTEGER",
    "quarantined": "INTEGER", "live_min_id": "INTEGER", "live_max_id": "INTEGER",
    "updated": "INTEGER",   # v1 の列名。読み出し互換のため残す
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


def merge(live: sqlite3.Connection, arc: sqlite3.Connection, source_path: str) -> dict:
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
        before_max = arc.execute("SELECT MAX(created_at) FROM usage_events").fetchone()[0]
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


def log_failed_run(arc: sqlite3.Connection, status: str, source_path: str, note: str = "") -> None:
    """取り込めなかった実行も必ず残す。空白期間の判定に必要。"""
    arc.execute("BEGIN IMMEDIATE")
    try:
        before_max = arc.execute("SELECT MAX(created_at) FROM usage_events").fetchone()[0]
        _log_run(arc, ran_at=utcnow(), status=status, source_path=source_path,
                 source_ident=source_identity(Path(source_path)),
                 archive_max_created_before=before_max, note=note)
        arc.execute("COMMIT")
    except Exception:
        arc.execute("ROLLBACK")
        raise


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
    """
    runs = arc.execute(
        """SELECT run_id, ran_at, status, source_ident, live_min_created,
                  archive_max_created_before
           FROM collect_runs ORDER BY run_id"""
    ).fetchall()

    gaps = []
    prev_ident = None
    seen_first = False
    for _rid, ran_at, status, ident, live_min, arc_max in runs:
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
            "SELECT COUNT(*) FROM usage_events WHERE created_at > ? AND created_at < ?", (lo, hi)
        ).fetchone()[0]
        gaps.append({
            "from": lo, "to": hi, "detected_at": ran_at,
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


def coverage(arc: sqlite3.Connection) -> dict:
    row = arc.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM usage_events").fetchone()
    runs = arc.execute("SELECT COUNT(*) FROM collect_runs").fetchone()[0]
    failed = arc.execute("SELECT COUNT(*) FROM collect_runs WHERE status<>'ok'").fetchone()[0]
    qn = arc.execute("SELECT COUNT(*) FROM quarantine_events").fetchone()[0]
    last = arc.execute("SELECT ran_at, status FROM collect_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    return {
        "events": row[0], "since": row[1], "until": row[2],
        "runs": runs, "failed_runs": failed, "quarantined": qn,
        "last_run_at": last[0] if last else None,
        "last_run_status": last[1] if last else None,
        "gaps": detect_gaps(arc),
    }


# --------------------------------------------------------------------------- エクスポート
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
    cfg["archive_db"] = os.path.expandvars(os.path.expanduser(cfg["archive_db"]))
    return cfg


def ingest(src_path: Path, arc: sqlite3.Connection, quiet: bool = False):
    """ライブ DB → アーカイブ。失敗しても例外を投げず、実行ログだけ残す。"""
    if not src_path.exists():
        if not quiet:
            print(f"[warn] ライブ DB が見つかりません: {src_path}")
            print("       アーカイブ済みのデータのみで集計します。")
        log_failed_run(arc, "source_missing", str(src_path))
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
        log_failed_run(arc, "unsupported_schema", str(src_path), str(e)[:500])
        return None

    if live is None:
        if not quiet:
            print("[warn] ライブ DB がロックされています。今回の取り込みは見送ります。")
        log_failed_run(arc, "source_locked", str(src_path), "read-only connect failed")
        return None
    try:
        return merge(live, arc, str(src_path))
    except Exception as e:      # 取り込み失敗でも集計は継続させる
        log_failed_run(arc, "error", str(src_path), repr(e)[:500])
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
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    arc_path = Path(args.archive)
    src_path = Path(args.source)

    # 読み取り専用の操作でアーカイブを新規作成してはいけない。
    # パスを間違えたまま --backup-to が「成功」すると、空 DB を保存して
    # 履歴を守れたと誤解させることになる。
    read_only_mode = bool(args.export_csv or args.backup_to or args.stats)
    if read_only_mode and not arc_path.exists():
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

        if args.stats:
            cov = coverage(arc)
            aic = arc.execute("SELECT COALESCE(SUM(total_nano_aiu),0) FROM usage_events").fetchone()[0] / 1e9
            print(f"アーカイブ: {arc_path}")
            print(f"  {cov['events']:,} イベント / {cov['since']} 〜 {cov['until']} / 合計 {aic:,.1f} AIC")
            print(f"  収集 {cov['runs']} 回（失敗 {cov['failed_runs']}）"
                  f" / 最終 {cov['last_run_at']} [{cov['last_run_status']}]")
            if cov["quarantined"]:
                print(f"  [!] 隔離 {cov['quarantined']} 行（created_at 欠損）")
            for g in cov["gaps"]:
                print(f"  [gap:{g['confidence']}] {g['from']} 〜 {g['to']}")
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

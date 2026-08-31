import hashlib
import secrets
import string
from contextlib import contextmanager

from psycopg import Error as PsycopgError, InterfaceError, OperationalError
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from evidence import (
    EvidenceIntegrityError,
    build_evidence_snapshot,
    canonicalize_evidence,
    evidence_hash,
    load_evidence_snapshot,
)
from secret_redaction import (
    install_postgres_pool_log_redaction,
    safe_exception_type,
)


install_postgres_pool_log_redaction()


CASE_STATUSES = {
    "COLLECTING",
    "READY_FOR_MAP",
    "MAP_READY",
    "MEDIATING",
    "PAUSED",
    "ARBITRATION_PENDING",
    "ARBITRATING",
    "CLOSED",
}
CHECKPOINT_ARTIFACT_KINDS = {
    "JUDGMENT_NORMAL",
    "JUDGMENT_SWAPPED",
    "META_JUDGMENT",
}
PUBLIC_ARTIFACT_KINDS = {"DISPUTE_MAP", "FINAL_JUDGMENT"}
EVIDENCE_ARTIFACT_KINDS = {"ARBITRATION_EVIDENCE"}
ARTIFACT_KINDS = (
    PUBLIC_ARTIFACT_KINDS
    | CHECKPOINT_ARTIFACT_KINDS
    | EVIDENCE_ARTIFACT_KINDS
)
MESSAGE_SENDERS = {"A", "B", "JUDGE", "SYSTEM"}
ROLES = {"A", "B"}
NOTIFICATION_EVENT_TYPES = {
    "ARBITRATION_ACCEPTED",
    "ARBITRATION_DECLINED",
}
MESSAGE_ALLOWED_STATUSES = {"MAP_READY", "MEDIATING", "ARBITRATION_PENDING"}
ARBITRATION_REQUEST_ALLOWED_STATUSES = {"MAP_READY", "MEDIATING"}
CASE_VIEW_NAMES = {"statement", "dispute", "mediation", "final"}
ADMIN_CASE_LINKED_TABLES = (
    "cases",
    "statements",
    "artifacts",
    "messages",
    "case_notifications",
)
MAX_ADMIN_CASE_ID_LENGTH = 128


class DatabaseError(Exception):
    """A database error safe to show without connection details."""


class DatabaseUnavailable(DatabaseError):
    pass


class StatementAlreadySubmitted(DatabaseError):
    pass


class CaseStateError(DatabaseError):
    pass


class _CaseIdCollision(Exception):
    pass


def _safe_database_unavailable(message, error):
    return DatabaseUnavailable(
        f"{message}（{safe_exception_type(error)}）"
    )


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS cases (
        case_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        a_token_hash TEXT NOT NULL,
        b_token_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'COLLECTING'
            CHECK (status IN (
                'COLLECTING', 'READY_FOR_MAP', 'MAP_READY',
                'MEDIATING', 'PAUSED', 'ARBITRATION_PENDING',
                'ARBITRATING', 'CLOSED'
            )),
        paused_by TEXT CHECK (paused_by IS NULL OR paused_by IN ('A', 'B')),
        arbitration_requested_by TEXT CHECK (
            arbitration_requested_by IS NULL
            OR arbitration_requested_by IN ('A', 'B')
        ),
        arbitration_requested_at TIMESTAMPTZ,
        arbitration_started_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    ALTER TABLE cases
        ADD COLUMN IF NOT EXISTS arbitration_requested_by TEXT
        CHECK (
            arbitration_requested_by IS NULL
            OR arbitration_requested_by IN ('A', 'B')
        )
    """,
    """
    ALTER TABLE cases
        ADD COLUMN IF NOT EXISTS arbitration_requested_at TIMESTAMPTZ
    """,
    """
    ALTER TABLE cases
        ADD COLUMN IF NOT EXISTS arbitration_started_at TIMESTAMPTZ
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'cases'::regclass
              AND conname = 'cases_status_check'
              AND pg_get_constraintdef(oid) LIKE '%ARBITRATION_PENDING%'
              AND pg_get_constraintdef(oid) LIKE '%ARBITRATING%'
        ) THEN
            ALTER TABLE cases
                DROP CONSTRAINT IF EXISTS cases_status_check;
            ALTER TABLE cases
                ADD CONSTRAINT cases_status_check CHECK (status IN (
                    'COLLECTING', 'READY_FOR_MAP', 'MAP_READY',
                    'MEDIATING', 'PAUSED', 'ARBITRATION_PENDING',
                    'ARBITRATING', 'CLOSED'
                ));
        END IF;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS statements (
        id BIGSERIAL PRIMARY KEY,
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('A', 'B')),
        content TEXT NOT NULL,
        submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(case_id, role)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id BIGSERIAL PRIMARY KEY,
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN (
            'DISPUTE_MAP', 'FINAL_JUDGMENT',
            'JUDGMENT_NORMAL', 'JUDGMENT_SWAPPED', 'META_JUDGMENT',
            'ARBITRATION_EVIDENCE'
        )),
        content TEXT NOT NULL,
        evidence_hash TEXT,
        generation_failed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(case_id, kind)
    )
    """,
    """
    ALTER TABLE artifacts
        ADD COLUMN IF NOT EXISTS evidence_hash TEXT
    """,
    """
    ALTER TABLE artifacts
        ADD COLUMN IF NOT EXISTS generation_failed_at TIMESTAMPTZ
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'artifacts'::regclass
              AND conname = 'artifacts_kind_check'
              AND pg_get_constraintdef(oid) LIKE '%ARBITRATION_EVIDENCE%'
        ) THEN
            ALTER TABLE artifacts
                DROP CONSTRAINT IF EXISTS artifacts_kind_check;
            ALTER TABLE artifacts
                ADD CONSTRAINT artifacts_kind_check CHECK (kind IN (
                    'DISPUTE_MAP', 'FINAL_JUDGMENT',
                    'JUDGMENT_NORMAL', 'JUDGMENT_SWAPPED', 'META_JUDGMENT',
                    'ARBITRATION_EVIDENCE'
                ));
        END IF;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id BIGSERIAL PRIMARY KEY,
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        sender TEXT NOT NULL CHECK (sender IN ('A', 'B', 'JUDGE', 'SYSTEM')),
        content TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS case_notifications (
        id BIGSERIAL PRIMARY KEY,
        case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
        recipient_role TEXT NOT NULL CHECK (recipient_role IN ('A', 'B')),
        event_type TEXT NOT NULL CHECK (event_type IN (
            'ARBITRATION_ACCEPTED', 'ARBITRATION_DECLINED'
        )),
        actor_role TEXT NOT NULL CHECK (actor_role IN ('A', 'B')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        read_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_case_id ON messages(case_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_statements_case_id ON statements(case_id)",
    """
    CREATE INDEX IF NOT EXISTS idx_case_notifications_unread
    ON case_notifications(case_id, recipient_role, created_at, id)
    WHERE read_at IS NULL
    """,
)


def hash_token(token):
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def _case_code(length=6):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _private_token(role):
    return f"{role}-{secrets.token_urlsafe(24)}"


def _validate_role(role):
    if role not in ROLES:
        raise ValueError("无效的案件身份。")


def _validate_notification(event_type, recipient_role, actor_role):
    if event_type not in NOTIFICATION_EVENT_TYPES:
        raise ValueError("无效的通知类型。")
    _validate_role(recipient_role)
    _validate_role(actor_role)


def _validate_evidence_hash(value):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in string.hexdigits for character in value)
    ):
        raise ValueError("证据 Hash 无效。")
    return value.lower()


def _exact_admin_case_id(case_id):
    if not isinstance(case_id, str):
        return None
    if not case_id or case_id != case_id.strip():
        return None
    if len(case_id) > MAX_ADMIN_CASE_ID_LENGTH:
        return None
    return case_id


def _evidence_record(row):
    if not row:
        return None
    try:
        snapshot = load_evidence_snapshot(row["content"], row["evidence_hash"])
    except EvidenceIntegrityError as exc:
        raise CaseStateError("冻结证据完整性校验失败。") from exc
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "kind": row["kind"],
        "content": row["content"],
        "evidence_hash": row["evidence_hash"],
        "created_at": row["created_at"],
        "snapshot": snapshot,
    }


def create_postgres_pool(
    database_url,
    min_size=2,
    max_size=5,
    pool_timeout=10,
    connect_timeout=20,
    open_timeout=30,
):
    if not database_url:
        raise DatabaseUnavailable("数据库尚未配置。")

    pool = None
    try:
        pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            timeout=pool_timeout,
            max_idle=60,
            max_lifetime=1800,
            reconnect_timeout=30,
            check=ConnectionPool.check_connection,
            open=False,
            kwargs={
                "row_factory": dict_row,
                # Also works with Supabase transaction-mode poolers.
                "prepare_threshold": None,
                "autocommit": True,
                "connect_timeout": connect_timeout,
                "sslmode": "require",
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
            },
        )
        pool.open(wait=True, timeout=open_timeout)
    except (PsycopgError, PoolClosed, PoolTimeout, ValueError) as exc:
        if pool is not None:
            try:
                pool.close()
            except Exception:
                pass
        raise _safe_database_unavailable(
            "无法连接共享数据库，请稍后重试。",
            exc,
        ) from None
    return pool


def initialize_postgres_schema(pool, pool_timeout=10):
    try:
        with pool.connection(timeout=pool_timeout) as connection:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
    except (PsycopgError, PoolClosed, PoolTimeout) as exc:
        raise _safe_database_unavailable(
            "共享数据库暂时不可用，请稍后重试。",
            exc,
        ) from None


class Database:
    create_pool = staticmethod(create_postgres_pool)

    def __init__(
        self,
        database_url=None,
        min_size=2,
        max_size=5,
        pool_timeout=10,
        connect_timeout=20,
        open_timeout=30,
        pool=None,
    ):
        self._pool_timeout = pool_timeout
        self._owns_pool = pool is None
        self._pool = (
            pool
            if pool is not None
            else self.create_pool(
                database_url,
                min_size=min_size,
                max_size=max_size,
                pool_timeout=pool_timeout,
                connect_timeout=connect_timeout,
                open_timeout=open_timeout,
            )
        )

    @property
    def pool(self):
        return self._pool

    @contextmanager
    def _connection(self):
        try:
            with self._pool.connection(timeout=self._pool_timeout) as connection:
                with connection.transaction():
                    yield connection
        except DatabaseError:
            raise
        except (PsycopgError, PoolClosed, PoolTimeout) as exc:
            raise _safe_database_unavailable(
                "共享数据库暂时不可用，请稍后重试。",
                exc,
            ) from None

    def _read_query(self, statement, params=(), fetch_all=False):
        for attempt in range(2):
            try:
                with self._pool.connection(
                    timeout=self._pool_timeout
                ) as connection:
                    cursor = connection.execute(statement, params)
                    return cursor.fetchall() if fetch_all else cursor.fetchone()
            except (InterfaceError, OperationalError, PoolTimeout) as exc:
                if attempt == 0:
                    continue
                raise _safe_database_unavailable(
                    "共享数据库暂时不可用，请稍后重试。",
                    exc,
                ) from None
            except (PsycopgError, PoolClosed) as exc:
                raise _safe_database_unavailable(
                    "共享数据库暂时不可用，请稍后重试。",
                    exc,
                ) from None

        raise DatabaseUnavailable("共享数据库暂时不可用，请稍后重试。")

    def close(self):
        if self._owns_pool and self._pool is not None:
            self._pool.close()

    def init_db(self):
        initialize_postgres_schema(self._pool, self._pool_timeout)

    def health_check(self):
        row = self._read_query("SELECT 1 AS ok")
        return bool(row and row["ok"] == 1)

    def create_case(self, title):
        clean_title = title.strip() or "未命名争议"
        a_token = _private_token("A")
        b_token = _private_token("B")

        for _ in range(10):
            case_id = "CASE-" + _case_code()
            try:
                with self._connection() as connection:
                    try:
                        connection.execute(
                            """
                            INSERT INTO cases(
                                case_id, title, a_token_hash, b_token_hash,
                                status, created_at, updated_at
                            )
                            VALUES (%s, %s, %s, %s, 'COLLECTING', NOW(), NOW())
                            """,
                            (
                                case_id,
                                clean_title,
                                hash_token(a_token),
                                hash_token(b_token),
                            ),
                        )
                    except UniqueViolation as exc:
                        raise _CaseIdCollision from exc
            except _CaseIdCollision:
                continue
            return case_id, a_token, b_token

        raise DatabaseUnavailable("无法生成唯一案件编号，请稍后重试。")

    def list_case_metadata(self, limit=25, offset=0):
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > 100
        ):
            raise ValueError("limit 必须是 1 到 100 之间的整数。")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise ValueError("offset 必须是非负整数。")

        total_row = self._read_query("SELECT COUNT(*) AS count FROM cases")
        rows = self._read_query(
            """
            SELECT case_id, status, created_at, updated_at
            FROM cases
            ORDER BY created_at DESC, case_id DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
            fetch_all=True,
        )
        return {
            "total": int(total_row["count"]),
            "cases": [dict(row) for row in rows],
        }

    def get_case_admin_metadata(self, case_id):
        exact_case_id = _exact_admin_case_id(case_id)
        if exact_case_id is None:
            return None
        row = self._read_query(
            """
            SELECT case_id, status, created_at, updated_at
            FROM cases
            WHERE case_id = %s
            """,
            (exact_case_id,),
        )
        return dict(row) if row else None

    def _admin_case_counts(self, connection, case_id):
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM cases WHERE case_id = %s) AS cases,
                (SELECT COUNT(*) FROM statements WHERE case_id = %s)
                    AS statements,
                (SELECT COUNT(*) FROM artifacts WHERE case_id = %s)
                    AS artifacts,
                (SELECT COUNT(*) FROM messages WHERE case_id = %s)
                    AS messages,
                (SELECT COUNT(*) FROM case_notifications WHERE case_id = %s)
                    AS case_notifications
            """,
            (case_id, case_id, case_id, case_id, case_id),
        ).fetchone()
        return {
            table: int(row[table])
            for table in ADMIN_CASE_LINKED_TABLES
        }

    def delete_case_exact(self, case_id):
        exact_case_id = _exact_admin_case_id(case_id)
        if exact_case_id is None:
            return None

        with self._connection() as connection:
            case = connection.execute(
                "SELECT case_id FROM cases WHERE case_id = %s FOR UPDATE",
                (exact_case_id,),
            ).fetchone()
            if not case:
                return None

            deleted_counts = self._admin_case_counts(
                connection,
                exact_case_id,
            )
            result = connection.execute(
                "DELETE FROM cases WHERE case_id = %s",
                (exact_case_id,),
            )
            residual_counts = self._admin_case_counts(
                connection,
                exact_case_id,
            )
            if result.rowcount != 1 or any(residual_counts.values()):
                raise DatabaseError("案件删除验证失败，操作已回滚。")

        return {
            "case_id": exact_case_id,
            "deleted_counts": deleted_counts,
            "residual_counts": residual_counts,
            "residual": sum(residual_counts.values()),
        }

    def delete_case_if_title_prefix(self, case_id, title_prefix):
        if not isinstance(title_prefix, str) or not title_prefix:
            raise ValueError("案件标题前缀不能为空。")
        with self._connection() as connection:
            case = connection.execute(
                "SELECT title FROM cases WHERE case_id = %s FOR UPDATE",
                (case_id,),
            ).fetchone()
            if not case or not case["title"].startswith(title_prefix):
                return False
            result = connection.execute(
                "DELETE FROM cases WHERE case_id = %s",
                (case_id,),
            )
        return result.rowcount == 1

    def authenticate(self, case_id, token):
        clean_case_id = case_id.strip().upper()
        candidate_hash = hash_token(token)
        row = self._read_query(
            """
            SELECT a_token_hash, b_token_hash
            FROM cases
            WHERE case_id = %s
            """,
            (clean_case_id,),
        )

        if not row:
            return None
        if secrets.compare_digest(candidate_hash, row["a_token_hash"]):
            return "A"
        if secrets.compare_digest(candidate_hash, row["b_token_hash"]):
            return "B"
        return None

    def get_case(self, case_id):
        return self._read_query(
            """
            SELECT
                case_id,
                title,
                status,
                paused_by,
                arbitration_requested_by,
                arbitration_requested_at,
                arbitration_started_at,
                created_at,
                updated_at
            FROM cases
            WHERE case_id = %s
            """,
            (case_id,),
        )

    def get_submission_status(self, case_id):
        rows = self._read_query(
            "SELECT role FROM statements WHERE case_id = %s",
            (case_id,),
            fetch_all=True,
        )
        submitted = {row["role"] for row in rows}
        return {"A": "A" in submitted, "B": "B" in submitted}

    def get_case_overview(self, case_id):
        row = self._read_query(
            """
            SELECT
                c.case_id,
                c.title,
                c.status,
                c.paused_by,
                c.arbitration_requested_by,
                c.arbitration_requested_at,
                c.arbitration_started_at,
                c.created_at,
                c.updated_at,
                EXISTS (
                    SELECT 1
                    FROM statements AS s
                    WHERE s.case_id = c.case_id AND s.role = 'A'
                ) AS a_submitted,
                EXISTS (
                    SELECT 1
                    FROM statements AS s
                    WHERE s.case_id = c.case_id AND s.role = 'B'
                ) AS b_submitted
            FROM cases AS c
            WHERE c.case_id = %s
            """,
            (case_id,),
        )
        if not row:
            return None
        return {
            "case": {
                "case_id": row["case_id"],
                "title": row["title"],
                "status": row["status"],
                "paused_by": row["paused_by"],
                "arbitration_requested_by": row["arbitration_requested_by"],
                "arbitration_requested_at": row["arbitration_requested_at"],
                "arbitration_started_at": row["arbitration_started_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
            "submitted": {
                "A": row["a_submitted"],
                "B": row["b_submitted"],
            },
        }

    def get_case_revision(self, case_id, role):
        _validate_role(role)
        row = self._read_query(
            """
            SELECT
                c.status,
                c.updated_at,
                COALESCE(message_revision.latest_id, 0) AS latest_message_id,
                COALESCE(artifact_revision.item_count, 0) AS artifact_count,
                artifact_revision.latest_created_at AS latest_artifact_at,
                artifact_revision.latest_failure_at AS latest_artifact_failure_at,
                COALESCE(notification_revision.item_count, 0) AS unread_count,
                COALESCE(notification_revision.first_id, 0) AS first_unread_id
            FROM cases AS c
            LEFT JOIN LATERAL (
                SELECT MAX(id) AS latest_id
                FROM messages
                WHERE case_id = c.case_id
            ) AS message_revision ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    COUNT(*) AS item_count,
                    MAX(created_at) AS latest_created_at,
                    MAX(generation_failed_at) AS latest_failure_at
                FROM artifacts
                WHERE case_id = c.case_id
            ) AS artifact_revision ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS item_count, MIN(id) AS first_id
                FROM case_notifications
                WHERE
                    case_id = c.case_id
                    AND recipient_role = %s
                    AND read_at IS NULL
            ) AS notification_revision ON TRUE
            WHERE c.case_id = %s
            """,
            (role, case_id),
        )
        return self._case_revision(row)

    def get_case_view_snapshot(
        self,
        case_id,
        role,
        view,
        last_message_id=0,
    ):
        _validate_role(role)
        if view not in CASE_VIEW_NAMES:
            raise ValueError("无效的案件页面。")
        if not isinstance(last_message_id, int) or last_message_id < 0:
            raise ValueError("无效的消息游标。")
        selected_kinds = {
            "statement": (),
            "dispute": ("DISPUTE_MAP",),
            "mediation": ("DISPUTE_MAP",),
            "final": tuple(sorted(ARTIFACT_KINDS)),
        }[view]
        row = self._read_query(
            """
            SELECT
                c.case_id,
                c.title,
                c.status,
                c.paused_by,
                c.arbitration_requested_by,
                c.arbitration_requested_at,
                c.arbitration_started_at,
                c.created_at,
                c.updated_at,
                EXISTS (
                    SELECT 1 FROM statements AS submitted_a
                    WHERE submitted_a.case_id = c.case_id
                      AND submitted_a.role = 'A'
                ) AS a_submitted,
                EXISTS (
                    SELECT 1 FROM statements AS submitted_b
                    WHERE submitted_b.case_id = c.case_id
                      AND submitted_b.role = 'B'
                ) AS b_submitted,
                own_statement.id AS statement_id,
                own_statement.content AS statement_content,
                own_statement.submitted_at AS statement_submitted_at,
                COALESCE(artifact_data.items, '{}'::jsonb) AS artifacts,
                COALESCE(artifact_data.item_count, 0) AS artifact_count,
                artifact_data.latest_created_at AS latest_artifact_at,
                artifact_data.latest_failure_at AS latest_artifact_failure_at,
                COALESCE(message_data.items, '[]'::jsonb) AS messages,
                COALESCE(message_data.latest_id, 0) AS latest_message_id,
                notification_data.first_item AS unread_notification,
                COALESCE(notification_data.item_count, 0) AS unread_count,
                COALESCE(notification_data.first_id, 0) AS first_unread_id
            FROM cases AS c
            LEFT JOIN statements AS own_statement
                ON own_statement.case_id = c.case_id
               AND own_statement.role = %s
               AND %s = 'statement'
            LEFT JOIN LATERAL (
                SELECT
                    jsonb_object_agg(
                        artifact.kind,
                        jsonb_build_object(
                            'id', artifact.id,
                            'case_id', artifact.case_id,
                            'kind', artifact.kind,
                            'content', artifact.content,
                            'evidence_hash', artifact.evidence_hash,
                            'generation_failed_at', artifact.generation_failed_at,
                            'created_at', artifact.created_at
                        )
                    ) FILTER (
                        WHERE artifact.kind = ANY(%s)
                           OR (
                               c.status = 'READY_FOR_MAP'
                               AND artifact.kind = 'DISPUTE_MAP'
                           )
                    ) AS items,
                    COUNT(*) AS item_count,
                    MAX(artifact.created_at) AS latest_created_at,
                    MAX(artifact.generation_failed_at) AS latest_failure_at
                FROM artifacts AS artifact
                WHERE artifact.case_id = c.case_id
            ) AS artifact_data ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    jsonb_agg(
                        jsonb_build_object(
                            'id', message.id,
                            'case_id', message.case_id,
                            'sender', message.sender,
                            'content', message.content,
                            'created_at', message.created_at
                        )
                        ORDER BY message.id
                    ) FILTER (
                        WHERE %s = 'mediation' AND message.id > %s
                    ) AS items,
                    MAX(message.id) AS latest_id
                FROM messages AS message
                WHERE message.case_id = c.case_id
            ) AS message_data ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    (
                        jsonb_agg(
                            jsonb_build_object(
                                'id', notification.id,
                                'case_id', notification.case_id,
                                'recipient_role', notification.recipient_role,
                                'event_type', notification.event_type,
                                'actor_role', notification.actor_role,
                                'created_at', notification.created_at,
                                'read_at', notification.read_at
                            )
                            ORDER BY notification.created_at, notification.id
                        ) -> 0
                    ) AS first_item,
                    COUNT(*) AS item_count,
                    MIN(notification.id) AS first_id
                FROM case_notifications AS notification
                WHERE
                    notification.case_id = c.case_id
                    AND notification.recipient_role = %s
                    AND notification.read_at IS NULL
            ) AS notification_data ON TRUE
            WHERE c.case_id = %s
            """,
            (
                role,
                view,
                list(selected_kinds),
                view,
                last_message_id,
                role,
                case_id,
            ),
        )
        if not row:
            return None

        case = {
            "case_id": row["case_id"],
            "title": row["title"],
            "status": row["status"],
            "paused_by": row["paused_by"],
            "arbitration_requested_by": row["arbitration_requested_by"],
            "arbitration_requested_at": row["arbitration_requested_at"],
            "arbitration_started_at": row["arbitration_started_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        statement = None
        if row["statement_id"] is not None:
            statement = {
                "id": row["statement_id"],
                "case_id": row["case_id"],
                "role": role,
                "content": row["statement_content"],
                "submitted_at": row["statement_submitted_at"],
            }
        artifacts = row["artifacts"] or {}
        evidence_row = artifacts.get("ARBITRATION_EVIDENCE")
        notification = row["unread_notification"]
        return {
            "case": case,
            "submitted": {
                "A": row["a_submitted"],
                "B": row["b_submitted"],
            },
            "statement": statement,
            "artifacts": artifacts,
            "evidence": _evidence_record(evidence_row),
            "messages": row["messages"],
            "unread_notifications": [notification] if notification else [],
            "revision": self._case_revision(row),
        }

    @staticmethod
    def _case_revision(row):
        if not row:
            return None
        return {
            "status": row["status"],
            "updated_at": row["updated_at"],
            "latest_message_id": row["latest_message_id"],
            "artifact_count": row["artifact_count"],
            "latest_artifact_at": row.get("latest_artifact_at"),
            "latest_artifact_failure_at": row.get(
                "latest_artifact_failure_at"
            ),
            "unread_count": row["unread_count"],
            "first_unread_id": row["first_unread_id"],
        }

    def get_statement(self, case_id, role):
        if role not in ROLES:
            raise ValueError("无效的案件身份。")
        return self._read_query(
            """
            SELECT id, case_id, role, content, submitted_at
            FROM statements
            WHERE case_id = %s AND role = %s
            """,
            (case_id, role),
        )

    def get_statements_for_llm(self, case_id):
        rows = self._read_query(
            """
            SELECT role, content
            FROM statements
            WHERE case_id = %s
            """,
            (case_id,),
            fetch_all=True,
        )
        statements = {row["role"]: row["content"] for row in rows}
        if set(statements) != ROLES:
            raise CaseStateError("双方独立陈述尚未全部提交。")
        return statements

    def save_statement(self, case_id, role, content):
        if role not in ROLES:
            raise ValueError("无效的案件身份。")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("独立陈述不能为空。")
        clean_content = content.strip()

        with self._connection() as connection:
            case = connection.execute(
                "SELECT status FROM cases WHERE case_id = %s FOR UPDATE",
                (case_id,),
            ).fetchone()
            if not case:
                raise CaseStateError("案件不存在。")

            try:
                connection.execute(
                    """
                    INSERT INTO statements(case_id, role, content, submitted_at)
                    VALUES (%s, %s, %s, NOW())
                    """,
                    (case_id, role, clean_content),
                )
            except UniqueViolation as exc:
                raise StatementAlreadySubmitted(
                    "你的独立陈述已经提交并冻结。"
                ) from exc

            if case["status"] != "COLLECTING":
                raise CaseStateError("当前案件状态不允许提交独立陈述。")

            count = connection.execute(
                "SELECT COUNT(*) AS count FROM statements WHERE case_id = %s",
                (case_id,),
            ).fetchone()["count"]
            if count >= 2:
                connection.execute(
                    """
                    UPDATE cases
                    SET status = 'READY_FOR_MAP', updated_at = NOW()
                    WHERE case_id = %s
                    """,
                    (case_id,),
                )
            else:
                connection.execute(
                    "UPDATE cases SET updated_at = NOW() WHERE case_id = %s",
                    (case_id,),
                )

    def both_submitted(self, case_id):
        status = self.get_submission_status(case_id)
        return status["A"] and status["B"]

    @staticmethod
    def _insert_notification(
        connection,
        case_id,
        recipient_role,
        event_type,
        actor_role,
    ):
        return connection.execute(
            """
            INSERT INTO case_notifications(
                case_id,
                recipient_role,
                event_type,
                actor_role,
                created_at
            )
            VALUES (%s, %s, %s, %s, NOW())
            RETURNING
                id,
                case_id,
                recipient_role,
                event_type,
                actor_role,
                created_at,
                read_at
            """,
            (case_id, recipient_role, event_type, actor_role),
        ).fetchone()

    def create_notification(
        self,
        case_id,
        recipient_role,
        event_type,
        actor_role,
    ):
        _validate_notification(event_type, recipient_role, actor_role)
        with self._connection() as connection:
            return self._insert_notification(
                connection,
                case_id,
                recipient_role,
                event_type,
                actor_role,
            )

    def get_unread_notifications(self, case_id, recipient_role):
        _validate_role(recipient_role)
        return self._read_query(
            """
            SELECT
                id,
                case_id,
                recipient_role,
                event_type,
                actor_role,
                created_at,
                read_at
            FROM case_notifications
            WHERE
                case_id = %s
                AND recipient_role = %s
                AND read_at IS NULL
            ORDER BY created_at ASC, id ASC
            """,
            (case_id, recipient_role),
            fetch_all=True,
        )

    def mark_notification_read(self, case_id, notification_id, recipient_role):
        _validate_role(recipient_role)
        with self._connection() as connection:
            result = connection.execute(
                """
                UPDATE case_notifications
                SET read_at = NOW()
                WHERE
                    id = %s
                    AND case_id = %s
                    AND recipient_role = %s
                    AND read_at IS NULL
                """,
                (notification_id, case_id, recipient_role),
            )
        return result.rowcount == 1

    def request_arbitration(self, case_id, role):
        _validate_role(role)
        with self._connection() as connection:
            case = connection.execute(
                """
                SELECT
                    status,
                    arbitration_requested_by,
                    arbitration_requested_at
                FROM cases
                WHERE case_id = %s
                FOR UPDATE
                """,
                (case_id,),
            ).fetchone()
            if not case:
                raise CaseStateError("案件不存在。")
            if case["status"] == "ARBITRATION_PENDING":
                return case
            if case["status"] not in ARBITRATION_REQUEST_ALLOWED_STATUSES:
                raise CaseStateError("当前案件状态不允许申请最终仲裁。")

            return connection.execute(
                """
                UPDATE cases
                SET
                    status = 'ARBITRATION_PENDING',
                    paused_by = NULL,
                    arbitration_requested_by = %s,
                    arbitration_requested_at = NOW(),
                    arbitration_started_at = NULL,
                    updated_at = NOW()
                WHERE case_id = %s
                RETURNING
                    status,
                    arbitration_requested_by,
                    arbitration_requested_at
                """,
                (role, case_id),
            ).fetchone()

    def cancel_arbitration_request(self, case_id, role):
        _validate_role(role)
        with self._connection() as connection:
            case = connection.execute(
                """
                SELECT status, arbitration_requested_by
                FROM cases
                WHERE case_id = %s
                FOR UPDATE
                """,
                (case_id,),
            ).fetchone()
            if not case:
                raise CaseStateError("案件不存在。")
            if case["status"] != "ARBITRATION_PENDING":
                raise CaseStateError("当前没有待确认的最终仲裁申请。")

            message_count = connection.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE case_id = %s",
                (case_id,),
            ).fetchone()["count"]
            target_status = "MEDIATING" if message_count else "MAP_READY"
            result = connection.execute(
                """
                UPDATE cases
                SET
                    status = %s,
                    arbitration_requested_by = NULL,
                    arbitration_requested_at = NULL,
                    arbitration_started_at = NULL,
                    updated_at = NOW()
                WHERE case_id = %s
                RETURNING status
                """,
                (target_status, case_id),
            ).fetchone()
            requester = case["arbitration_requested_by"]
            if requester in ROLES and role != requester:
                self._insert_notification(
                    connection,
                    case_id,
                    requester,
                    "ARBITRATION_DECLINED",
                    role,
                )
            return result

    def confirm_arbitration(self, case_id, role):
        _validate_role(role)
        with self._connection() as connection:
            case = connection.execute(
                """
                SELECT
                    status,
                    arbitration_requested_by,
                    arbitration_requested_at,
                    arbitration_started_at
                FROM cases
                WHERE case_id = %s
                FOR UPDATE
                """,
                (case_id,),
            ).fetchone()
            if not case:
                raise CaseStateError("案件不存在。")

            requester = case["arbitration_requested_by"]
            if requester == role:
                raise CaseStateError("申请方不能确认自己的最终仲裁申请。")
            if case["status"] in {"ARBITRATING", "CLOSED"}:
                existing = connection.execute(
                    """
                    SELECT id, case_id, kind, content, evidence_hash, created_at
                    FROM artifacts
                    WHERE case_id = %s AND kind = 'ARBITRATION_EVIDENCE'
                    """,
                    (case_id,),
                ).fetchone()
                record = _evidence_record(existing)
                if record:
                    return record
                raise CaseStateError("案件缺少冻结证据。")
            if case["status"] != "ARBITRATION_PENDING" or requester not in ROLES:
                raise CaseStateError("当前案件状态不允许确认最终仲裁。")

            statement_rows = connection.execute(
                """
                SELECT role, content
                FROM statements
                WHERE case_id = %s
                ORDER BY role
                """,
                (case_id,),
            ).fetchall()
            statements = {
                statement["role"]: statement["content"]
                for statement in statement_rows
            }
            if set(statements) != ROLES:
                raise CaseStateError("双方独立陈述尚未全部提交。")

            dispute = connection.execute(
                """
                SELECT content
                FROM artifacts
                WHERE case_id = %s AND kind = 'DISPUTE_MAP'
                """,
                (case_id,),
            ).fetchone()
            if not dispute or not dispute["content"]:
                raise CaseStateError("争议地图尚未完成。")

            messages = connection.execute(
                """
                SELECT id, sender, content, created_at
                FROM messages
                WHERE case_id = %s
                ORDER BY id ASC
                """,
                (case_id,),
            ).fetchall()
            frozen_at = connection.execute(
                "SELECT NOW() AS frozen_at"
            ).fetchone()["frozen_at"]
            cutoff = messages[-1]["id"] if messages else 0
            snapshot = build_evidence_snapshot(
                case_id=case_id,
                created_at=frozen_at,
                requester=requester,
                confirmer=role,
                statements=statements,
                dispute_map=dispute["content"],
                messages=messages,
                message_cutoff_id=cutoff,
            )
            content = canonicalize_evidence(snapshot)
            snapshot_hash = evidence_hash(snapshot)
            artifact = connection.execute(
                """
                INSERT INTO artifacts(
                    case_id, kind, content, evidence_hash, created_at
                )
                VALUES (
                    %s, 'ARBITRATION_EVIDENCE', %s, %s, %s
                )
                ON CONFLICT (case_id, kind) DO NOTHING
                RETURNING id, case_id, kind, content, evidence_hash, created_at
                """,
                (case_id, content, snapshot_hash, frozen_at),
            ).fetchone()
            if not artifact:
                existing = connection.execute(
                    """
                    SELECT id, case_id, kind, content, evidence_hash, created_at
                    FROM artifacts
                    WHERE case_id = %s AND kind = 'ARBITRATION_EVIDENCE'
                    """,
                    (case_id,),
                ).fetchone()
                if (
                    not existing
                    or existing["content"] != content
                    or existing["evidence_hash"] != snapshot_hash
                ):
                    raise CaseStateError("案件已经存在不同的冻结证据。")
                artifact = existing

            connection.execute(
                """
                UPDATE cases
                SET
                    status = 'ARBITRATING',
                    paused_by = NULL,
                    arbitration_started_at = %s,
                    updated_at = NOW()
                WHERE case_id = %s
                """,
                (frozen_at, case_id),
            )
            self._insert_notification(
                connection,
                case_id,
                requester,
                "ARBITRATION_ACCEPTED",
                role,
            )
            return _evidence_record(artifact)

    def get_arbitration_evidence(self, case_id):
        row = self._read_query(
            """
            SELECT
                id,
                case_id,
                kind,
                content,
                evidence_hash,
                created_at
            FROM artifacts
            WHERE case_id = %s AND kind = 'ARBITRATION_EVIDENCE'
            """,
            (case_id,),
        )
        return _evidence_record(row)

    def get_artifact(self, case_id, kind):
        if kind not in ARTIFACT_KINDS:
            raise ValueError("无效的仲裁产物类型。")
        return self._read_query(
            """
            SELECT
                id,
                case_id,
                kind,
                content,
                evidence_hash,
                generation_failed_at,
                created_at
            FROM artifacts
            WHERE case_id = %s AND kind = %s
            """,
            (case_id, kind),
        )

    def save_checkpoint(self, case_id, kind, content, evidence_hash):
        if kind not in CHECKPOINT_ARTIFACT_KINDS:
            raise ValueError("无效的仲裁检查点类型。")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("仲裁检查点内容不能为空。")
        evidence_hash = _validate_evidence_hash(evidence_hash)

        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO artifacts(
                    case_id, kind, content, evidence_hash, created_at
                )
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (case_id, kind) DO NOTHING
                RETURNING id, content, evidence_hash
                """,
                (case_id, kind, content, evidence_hash),
            ).fetchone()
            if row:
                return row["id"]

            existing = connection.execute(
                """
                SELECT id, content, evidence_hash
                FROM artifacts
                WHERE case_id = %s AND kind = %s
                """,
                (case_id, kind),
            ).fetchone()
            if (
                existing
                and existing["content"] == content
                and existing["evidence_hash"] == evidence_hash
            ):
                return existing["id"]
            raise CaseStateError("该仲裁检查点已由其他请求完成。")

    def claim_artifact(self, case_id, kind):
        if kind == "DISPUTE_MAP":
            statement = """
                INSERT INTO artifacts(case_id, kind, content, created_at)
                SELECT c.case_id, %s, '', NOW()
                FROM cases AS c
                WHERE c.case_id = %s AND c.status = 'READY_FOR_MAP'
                ON CONFLICT (case_id, kind) DO NOTHING
                RETURNING id
            """
        elif kind == "FINAL_JUDGMENT":
            statement = """
                INSERT INTO artifacts(
                    case_id, kind, content, evidence_hash, created_at
                )
                SELECT c.case_id, %s, '', evidence.evidence_hash, NOW()
                FROM cases AS c
                JOIN artifacts AS evidence
                  ON evidence.case_id = c.case_id
                 AND evidence.kind = 'ARBITRATION_EVIDENCE'
                WHERE c.case_id = %s AND c.status = 'ARBITRATING'
                ON CONFLICT (case_id, kind) DO NOTHING
                RETURNING id
            """
        else:
            raise ValueError("无效的仲裁产物类型。")

        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM artifacts
                WHERE case_id = %s
                  AND kind = %s
                  AND content = ''
                  AND generation_failed_at IS NULL
                  AND created_at < NOW() - INTERVAL '15 minutes'
                """,
                (case_id, kind),
            )
            row = connection.execute(
                statement,
                (kind, case_id),
            ).fetchone()
        return row["id"] if row else None

    def fail_artifact(self, case_id, artifact_id, kind):
        if kind != "DISPUTE_MAP":
            raise ValueError("只有争议地图支持生成失败恢复。")
        with self._connection() as connection:
            result = connection.execute(
                """
                UPDATE artifacts
                SET generation_failed_at = COALESCE(generation_failed_at, NOW())
                WHERE
                    id = %s
                    AND case_id = %s
                    AND kind = %s
                    AND content = ''
                """,
                (artifact_id, case_id, kind),
            )
        return result.rowcount == 1

    def retry_failed_artifact(self, case_id, kind):
        if kind != "DISPUTE_MAP":
            raise ValueError("只有争议地图支持生成失败恢复。")
        with self._connection() as connection:
            row = connection.execute(
                """
                UPDATE artifacts AS artifact
                SET generation_failed_at = NULL, created_at = NOW()
                FROM cases AS case_record
                WHERE
                    artifact.case_id = case_record.case_id
                    AND artifact.case_id = %s
                    AND artifact.kind = %s
                    AND artifact.content = ''
                    AND artifact.generation_failed_at IS NOT NULL
                    AND case_record.status = 'READY_FOR_MAP'
                RETURNING artifact.id
                """,
                (case_id, kind),
            ).fetchone()
        return row["id"] if row else None

    def complete_artifact(self, case_id, artifact_id, kind, content):
        if kind not in PUBLIC_ARTIFACT_KINDS:
            raise ValueError("无效的仲裁产物类型。")
        target_status = "MAP_READY" if kind == "DISPUTE_MAP" else "CLOSED"

        with self._connection() as connection:
            case = connection.execute(
                "SELECT status FROM cases WHERE case_id = %s FOR UPDATE",
                (case_id,),
            ).fetchone()
            if not case:
                raise CaseStateError("案件不存在。")
            allowed_statuses = (
                {"READY_FOR_MAP", "MAP_READY"}
                if kind == "DISPUTE_MAP"
                else {"ARBITRATING", "CLOSED"}
            )
            if case["status"] not in allowed_statuses:
                raise CaseStateError("当前案件状态不允许完成该结果。")

            snapshot_hash = None
            if kind == "FINAL_JUDGMENT":
                evidence = connection.execute(
                    """
                    SELECT evidence_hash
                    FROM artifacts
                    WHERE case_id = %s AND kind = 'ARBITRATION_EVIDENCE'
                    """,
                    (case_id,),
                ).fetchone()
                if not evidence or not evidence["evidence_hash"]:
                    raise CaseStateError("案件缺少冻结证据。")
                snapshot_hash = evidence["evidence_hash"]

            if snapshot_hash is None:
                artifact = connection.execute(
                    """
                    UPDATE artifacts
                    SET content = %s, generation_failed_at = NULL
                    WHERE id = %s
                      AND case_id = %s
                      AND kind = %s
                      AND content = ''
                    RETURNING id, evidence_hash
                    """,
                    (content, artifact_id, case_id, kind),
                ).fetchone()
            else:
                artifact = connection.execute(
                    """
                    UPDATE artifacts
                    SET content = %s, generation_failed_at = NULL
                    WHERE id = %s
                      AND case_id = %s
                      AND kind = %s
                      AND content = ''
                      AND evidence_hash = %s
                    RETURNING id, evidence_hash
                    """,
                    (content, artifact_id, case_id, kind, snapshot_hash),
                ).fetchone()
            if not artifact:
                existing = connection.execute(
                    """
                    SELECT content, evidence_hash
                    FROM artifacts
                    WHERE id = %s AND case_id = %s AND kind = %s
                    """,
                    (artifact_id, case_id, kind),
                ).fetchone()
                if (
                    not existing
                    or existing["content"] != content
                    or (
                        snapshot_hash is not None
                        and existing["evidence_hash"] != snapshot_hash
                    )
                ):
                    raise CaseStateError("该结果已被其他请求完成或取消。")

            updated = connection.execute(
                """
                UPDATE cases
                SET status = %s, paused_by = NULL, updated_at = NOW()
                WHERE case_id = %s AND status = ANY(%s)
                """,
                (target_status, case_id, list(allowed_statuses)),
            )
            if updated.rowcount != 1:
                raise CaseStateError("案件状态已经变化。")

    def release_artifact(self, case_id, artifact_id, kind):
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM artifacts
                WHERE id = %s AND case_id = %s AND kind = %s AND content = ''
                """,
                (artifact_id, case_id, kind),
            )

    def get_messages(self, case_id):
        return self._read_query(
            """
            SELECT id, case_id, sender, content, created_at
            FROM messages
            WHERE case_id = %s
            ORDER BY id ASC
            """,
            (case_id,),
            fetch_all=True,
        )

    def get_mediation_snapshot(self, case_id, last_message_id=0):
        if not isinstance(last_message_id, int) or last_message_id < 0:
            raise ValueError("无效的消息游标。")

        row = self._read_query(
            """
            SELECT
                c.case_id,
                c.title,
                c.status,
                c.paused_by,
                c.arbitration_requested_by,
                c.arbitration_requested_at,
                c.arbitration_started_at,
                c.created_at,
                c.updated_at,
                a.id AS artifact_id,
                a.kind AS artifact_kind,
                a.content AS artifact_content,
                a.created_at AS artifact_created_at,
                COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'id', m.id,
                            'case_id', m.case_id,
                            'sender', m.sender,
                            'content', m.content,
                            'created_at', m.created_at
                        )
                        ORDER BY m.id
                    ) FILTER (WHERE m.id IS NOT NULL),
                    '[]'::jsonb
                ) AS messages
            FROM cases AS c
            LEFT JOIN artifacts AS a
                ON a.case_id = c.case_id AND a.kind = %s
            LEFT JOIN messages AS m
                ON m.case_id = c.case_id AND m.id > %s
            WHERE c.case_id = %s
            GROUP BY
                c.case_id,
                c.title,
                c.status,
                c.paused_by,
                c.arbitration_requested_by,
                c.arbitration_requested_at,
                c.arbitration_started_at,
                c.created_at,
                c.updated_at,
                a.id,
                a.kind,
                a.content,
                a.created_at
            """,
            ("DISPUTE_MAP", last_message_id, case_id),
        )
        if not row:
            return None

        artifact = None
        if row["artifact_id"] is not None:
            artifact = {
                "id": row["artifact_id"],
                "case_id": row["case_id"],
                "kind": row["artifact_kind"],
                "content": row["artifact_content"],
                "created_at": row["artifact_created_at"],
            }

        return {
            "case": {
                "case_id": row["case_id"],
                "title": row["title"],
                "status": row["status"],
                "paused_by": row["paused_by"],
                "arbitration_requested_by": row["arbitration_requested_by"],
                "arbitration_requested_at": row["arbitration_requested_at"],
                "arbitration_started_at": row["arbitration_started_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
            "artifact": artifact,
            "messages": row["messages"],
        }

    def add_message(self, case_id, sender, content):
        if sender not in MESSAGE_SENDERS:
            raise ValueError("无效的消息发送者。")
        clean_content = content.strip()
        if not clean_content:
            raise ValueError("消息不能为空。")

        with self._connection() as connection:
            message = connection.execute(
                """
                WITH eligible_case AS (
                    UPDATE cases
                    SET
                        status = CASE
                            WHEN status = 'MAP_READY' THEN 'MEDIATING'
                            ELSE status
                        END,
                        updated_at = NOW()
                    WHERE case_id = %s AND status = ANY(%s)
                    RETURNING case_id, status, updated_at
                ),
                previous_message AS (
                    SELECT COALESCE(MAX(message.id), 0) AS id
                    FROM messages AS message
                    JOIN eligible_case
                        ON eligible_case.case_id = message.case_id
                ),
                inserted_message AS (
                    INSERT INTO messages(
                        case_id,
                        sender,
                        content,
                        created_at
                    )
                    SELECT case_id, %s, %s, NOW()
                    FROM eligible_case
                    RETURNING id, case_id, sender, content, created_at
                )
                SELECT
                    inserted_message.*,
                    eligible_case.status AS case_status,
                    eligible_case.updated_at AS case_updated_at,
                    previous_message.id AS previous_message_id
                FROM inserted_message
                JOIN eligible_case USING (case_id)
                CROSS JOIN previous_message
                """,
                (
                    case_id,
                    list(MESSAGE_ALLOWED_STATUSES),
                    sender,
                    clean_content,
                ),
            ).fetchone()
            if not message:
                raise CaseStateError("当前案件状态不允许发送新消息。")
            return message

    def ensure_judge_intervention_allowed(self, case_id):
        with self._connection() as connection:
            case = connection.execute(
                "SELECT status FROM cases WHERE case_id = %s FOR UPDATE",
                (case_id,),
            ).fetchone()
            if not case or case["status"] not in MESSAGE_ALLOWED_STATUSES:
                raise CaseStateError("当前案件状态不允许请法官介入。")
        return True

    def pause_case(self, case_id, role):
        if role not in ROLES:
            raise ValueError("无效的案件身份。")
        with self._connection() as connection:
            result = connection.execute(
                """
                UPDATE cases
                SET status = 'PAUSED', paused_by = %s, updated_at = NOW()
                WHERE case_id = %s AND status IN ('MAP_READY', 'MEDIATING')
                """,
                (role, case_id),
            )
        return result.rowcount == 1

    def resume_case(self, case_id, role):
        if role not in ROLES:
            raise ValueError("无效的案件身份。")
        with self._connection() as connection:
            result = connection.execute(
                """
                UPDATE cases
                SET status = 'MEDIATING', paused_by = NULL, updated_at = NOW()
                WHERE case_id = %s AND status = 'PAUSED' AND paused_by = %s
                """,
                (case_id, role),
            )
        return result.rowcount == 1

"""Database operations for the worker.

The worker owns all message writes. The gateway only publishes to RabbitMQ, so
the fast request path never waits for PostgreSQL.

Idempotency:
    The gateway generates ``task_id`` and ``message_id``. If RabbitMQ redelivers
    a task (worker crash before ack), the same IDs are reused. ``ON CONFLICT``
    prevents duplicate messages, and an already-DELIVERED message is skipped.
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


async def _upsert_user(cursor, username: str):
    await cursor.execute(
        """
        INSERT INTO users (username)
        VALUES (%s)
        ON CONFLICT (username) DO UPDATE SET username = EXCLUDED.username
        RETURNING id
        """,
        (username,),
    )
    row = await cursor.fetchone()
    return row["id"]


async def _upsert_task_log(
    cursor,
    *,
    task_id: str,
    message_id: str,
    status: str,
    request_id: str,
    attempts: int,
    error: str | None,
) -> None:
    await cursor.execute(
        """
        INSERT INTO task_logs (task_id, message_id, status, attempts, error, request_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (task_id) DO UPDATE
        SET status = EXCLUDED.status,
            attempts = EXCLUDED.attempts,
            error = EXCLUDED.error,
            updated_at = now()
        """,
        (task_id, message_id, status, attempts, error, request_id),
    )


async def save_processing(
    pool: AsyncConnectionPool,
    *,
    task_id: str,
    message_id: str,
    request_id: str,
    sender: str,
    receiver: str,
    body: str,
    attachment_id: str | None = None,
) -> bool:
    """Create users/message/task_log with status PROCESSING.

    Returns False when the message was already DELIVERED (a redelivery that
    should be acknowledged without calling the notification provider again).
    """

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            sender_id = await _upsert_user(cursor, sender)
            receiver_id = await _upsert_user(cursor, receiver)

            await cursor.execute(
                """
                INSERT INTO messages (id, sender_id, receiver_id, body, status, attachment_id)
                VALUES (%s, %s, %s, %s, 'PROCESSING', %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                (message_id, sender_id, receiver_id, body, attachment_id),
            )
            inserted = await cursor.fetchone()

            if inserted is None:
                # Redelivery: inspect the existing row.
                await cursor.execute(
                    "SELECT status FROM messages WHERE id = %s", (message_id,)
                )
                existing = await cursor.fetchone()

                if existing is not None and existing["status"] in ("DELIVERED", "READ"):
                    await _upsert_task_log(
                        cursor,
                        task_id=task_id,
                        message_id=message_id,
                        status="DELIVERED",
                        request_id=request_id,
                        attempts=0,
                        error=None,
                    )
                    return False

                await cursor.execute(
                    """
                    UPDATE messages
                    SET status = 'PROCESSING', updated_at = now()
                    WHERE id = %s
                    """,
                    (message_id,),
                )

            await _upsert_task_log(
                cursor,
                task_id=task_id,
                message_id=message_id,
                status="PROCESSING",
                request_id=request_id,
                attempts=0,
                error=None,
            )

    return True


async def finalize(
    pool: AsyncConnectionPool,
    *,
    task_id: str,
    message_id: str,
    status: str,
    attempts: int,
    error: str | None,
) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE messages
                SET status = %s,
                    delivered_at = CASE
                        WHEN %s = 'DELIVERED'
                        THEN COALESCE(delivered_at, now())
                        ELSE delivered_at
                    END,
                    updated_at = now()
                WHERE id = %s
                """,
                (status, status, message_id),
            )
            await cursor.execute(
                """
                UPDATE task_logs
                SET status = %s, attempts = %s, error = %s, updated_at = now()
                WHERE task_id = %s
                """,
                (status, attempts, error, task_id),
            )

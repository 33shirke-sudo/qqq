"""SQLite storage layer for account registration pipeline.

Replaces five .txt files with a single SQLite database for atomic transactions,
fast queries, and better concurrency support for parallel workers.

Database schema:
    accounts table: stores email accounts with their status through the pipeline
    - email: primary key
    - password: pinmx password
    - nick: original nickname
    - pinmx_created_at: timestamp when email was created
    - devin_status: NULL (pending), 'success', or 'error'
    - devin_error: error message if devin_status='error'
    - devin_registered_at: timestamp when Devin registration succeeded
    - identity_name: full name from identity generation
    - identity_address: full address from identity generation
    - created_at: record creation timestamp

Backward compatibility:
    - Imports existing data from .txt files on first run
    - Exports to .txt files for compatibility with external tools
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator


class AccountDB:
    """Thread-safe SQLite database for account management."""

    def __init__(self, db_path: Path):
        """Initialize database connection.

        Args:
            db_path: Path to SQLite database file. Created if doesn't exist.
        """
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn'):
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None  # autocommit mode for better concurrency
            )
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """Context manager for database transactions with retry on lock."""
        conn = self._get_conn()

        # Retry logic for SQLITE_BUSY (database locked)
        max_retries = 5
        for attempt in range(max_retries):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))  # Exponential backoff
                else:
                    raise

        try:
            cursor = conn.cursor()
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_db(self) -> None:
        """Create database schema if it doesn't exist."""
        conn = self._get_conn()

        # Enable WAL mode for better concurrency
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                email TEXT PRIMARY KEY,
                password TEXT,
                nick TEXT,
                pinmx_created_at TEXT,
                devin_status TEXT,
                devin_error TEXT,
                devin_registered_at TEXT,
                identity_name TEXT,
                identity_address TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_devin_status ON accounts(devin_status);
            CREATE INDEX IF NOT EXISTS idx_nick ON accounts(nick);
            CREATE INDEX IF NOT EXISTS idx_created_at ON accounts(created_at);

            -- Входные данные для Step 1
            CREATE TABLE IF NOT EXISTS nicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nick TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- BIN-коды для Step 4
            CREATE TABLE IF NOT EXISTS bins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bin TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Живые карты (Step 4)
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_number TEXT NOT NULL,
                exp_month TEXT NOT NULL,
                exp_year TEXT NOT NULL,
                cvv TEXT NOT NULL,
                bin TEXT,
                status TEXT DEFAULT 'live',
                checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TEXT,
                used_at TEXT,
                used_by_email TEXT,
                UNIQUE(card_number, exp_month, exp_year, cvv)
            );

            -- История использования карт (для activate_accounts.py)
            CREATE TABLE IF NOT EXISTS card_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card TEXT NOT NULL,
                email TEXT NOT NULL,
                outcome TEXT NOT NULL,
                attempted_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Активированные аккаунты (финальный результат)
            CREATE TABLE IF NOT EXISTS activated_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                card TEXT NOT NULL,
                holder_name TEXT NOT NULL,
                activated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Индексы для новых таблиц
            CREATE INDEX IF NOT EXISTS idx_nicks_status ON nicks(status);
            CREATE INDEX IF NOT EXISTS idx_bins_status ON bins(status);
            CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
            CREATE INDEX IF NOT EXISTS idx_cards_bin ON cards(bin);
            CREATE INDEX IF NOT EXISTS idx_card_attempts_email ON card_attempts(email);
            CREATE INDEX IF NOT EXISTS idx_card_attempts_card ON card_attempts(card);
        """)

    # ---------------------------------------------------------------------------
    # Email creation (Step 1: create_emails.py)
    # ---------------------------------------------------------------------------

    def add_email(self, email: str, password: str, nick: str) -> None:
        """Add newly created email account.

        Args:
            email: Full email address (e.g., nick@pingmx.com)
            password: Account password
            nick: Original nickname
        """
        with self._transaction() as cursor:
            cursor.execute("""
                INSERT OR REPLACE INTO accounts (email, password, nick, pinmx_created_at)
                VALUES (?, ?, ?, ?)
            """, (email, password, nick, datetime.utcnow().isoformat()))

    def mark_nick_taken(self, nick: str) -> None:
        """Mark nickname as taken (already exists on pinmx).

        Args:
            nick: Nickname that was rejected
        """
        with self._transaction() as cursor:
            cursor.execute("""
                INSERT OR IGNORE INTO accounts (nick, devin_status)
                VALUES (?, 'taken')
            """, (nick,))

    def is_nick_taken(self, nick: str) -> bool:
        """Check if nickname is already taken.

        Args:
            nick: Nickname to check

        Returns:
            True if nick exists in database (either as email or marked taken)
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT 1 FROM accounts WHERE nick = ? OR email LIKE ?
        """, (nick, f"{nick}@%"))
        return cursor.fetchone() is not None

    def get_done_nicks(self) -> set[str]:
        """Get all nicks that have been processed (have email or marked taken).

        Returns:
            Set of lowercase nicknames
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT nick FROM accounts WHERE nick IS NOT NULL
        """)
        return {row['nick'].lower() for row in cursor.fetchall() if row['nick']}

    # ---------------------------------------------------------------------------
    # Nicks management (входные данные для Step 1)
    # ---------------------------------------------------------------------------

    def add_nick(self, nick: str) -> None:
        """Add nickname to nicks table.

        Args:
            nick: Nickname to add
        """
        with self._transaction() as cursor:
            cursor.execute("""
                INSERT OR IGNORE INTO nicks (nick, status)
                VALUES (?, 'pending')
            """, (nick,))

    def get_pending_nicks(self, limit: int | None = None) -> list[str]:
        """Get pending nicknames for email creation.

        Args:
            limit: Maximum number of nicks to return

        Returns:
            List of nicknames with status='pending'
        """
        conn = self._get_conn()
        query = """
            SELECT nick FROM nicks
            WHERE status = 'pending'
            ORDER BY created_at
        """
        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query)
        return [row['nick'] for row in cursor.fetchall()]

    def get_all_nicks(self) -> list[dict]:
        """Get all nicknames for GUI display.

        Returns:
            List of dicts with keys: id, nick, status, created_at
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT id, nick, status, created_at FROM nicks
            ORDER BY created_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    def mark_nick_processing(self, nick: str) -> None:
        """Mark nickname as being processed.

        Args:
            nick: Nickname being processed
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE nicks SET status = 'processing'
                WHERE nick = ?
            """, (nick,))

    def mark_nick_done(self, nick: str) -> None:
        """Mark nickname as done (email created successfully).

        Args:
            nick: Nickname that was processed
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE nicks SET status = 'done'
                WHERE nick = ?
            """, (nick,))

    def mark_nick_taken_in_nicks(self, nick: str) -> None:
        """Mark nickname as taken in nicks table.

        Args:
            nick: Nickname that was rejected
        """
        with self._transaction() as cursor:
            cursor.execute("""
                INSERT OR REPLACE INTO nicks (nick, status)
                VALUES (?, 'taken')
            """, (nick,))

    def delete_nick(self, nick_id: int) -> None:
        """Delete nickname by ID.

        Args:
            nick_id: ID of nickname to delete
        """
        with self._transaction() as cursor:
            cursor.execute("DELETE FROM nicks WHERE id = ?", (nick_id,))

    def update_nick(self, nick_id: int, nick: str, status: str) -> None:
        """Update nickname.

        Args:
            nick_id: ID of nickname to update
            nick: New nickname value
            status: New status value
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE nicks SET nick = ?, status = ?
                WHERE id = ?
            """, (nick, status, nick_id))

    def search_nicks(self, query: str | None = None, status: str | None = None) -> list[dict]:
        """Search nicknames by query and/or status.

        Args:
            query: Search query (matches nick)
            status: Filter by status

        Returns:
            List of dicts with keys: id, nick, status, created_at
        """
        conn = self._get_conn()
        sql = "SELECT id, nick, status, created_at FROM nicks WHERE 1=1"
        params = []

        if query:
            sql += " AND nick LIKE ?"
            params.append(f"%{query}%")

        if status:
            sql += " AND status = ?"
            params.append(status)

        sql += " ORDER BY created_at DESC"

        cursor = conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    # ---------------------------------------------------------------------------
    # Devin registration (Step 2: register_devin.py)
    # ---------------------------------------------------------------------------

    def get_pending_devin(self, limit: int | None = None) -> list[dict]:
        """Get accounts pending Devin registration.

        Args:
            limit: Maximum number of accounts to return

        Returns:
            List of dicts with keys: email, password
        """
        conn = self._get_conn()
        query = """
            SELECT email, password FROM accounts
            WHERE password IS NOT NULL
              AND devin_status IS NULL
            ORDER BY created_at
        """
        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query)
        return [dict(row) for row in cursor.fetchall()]

    def mark_devin_success(self, email: str) -> None:
        """Mark Devin registration as successful.

        Args:
            email: Email address that was successfully registered
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE accounts
                SET devin_status = 'success',
                    devin_registered_at = ?
                WHERE email = ?
            """, (datetime.utcnow().isoformat(), email))

    def mark_devin_error(self, email: str, error: str) -> None:
        """Mark Devin registration as failed.

        Args:
            email: Email address that failed registration
            error: Error message
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE accounts
                SET devin_status = 'error',
                    devin_error = ?
                WHERE email = ?
            """, (error, email))

    def get_devin_done_emails(self) -> set[str]:
        """Get all emails that have been processed for Devin (success or error).

        Returns:
            Set of lowercase email addresses
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT email FROM accounts WHERE devin_status IN ('success', 'error')
        """)
        return {row['email'].lower() for row in cursor.fetchall()}

    # ---------------------------------------------------------------------------
    # Identity generation (Step 3: add_identities.py)
    # ---------------------------------------------------------------------------

    def get_pending_identities(self, limit: int | None = None) -> list[str]:
        """Get emails that need identity generation (successful Devin accounts).

        Args:
            limit: Maximum number of emails to return

        Returns:
            List of email addresses
        """
        conn = self._get_conn()
        query = """
            SELECT email FROM accounts
            WHERE devin_status = 'success'
              AND identity_name IS NULL
            ORDER BY devin_registered_at
        """
        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query)
        return [row['email'] for row in cursor.fetchall()]

    def add_identity(self, email: str, name: str, address: str) -> None:
        """Add identity information to account.

        Args:
            email: Email address
            name: Full name (e.g., "Иван Петров")
            address: Full address (e.g., "Улица 123, 12345, Город")
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE accounts
                SET identity_name = ?,
                    identity_address = ?
                WHERE email = ?
            """, (name, address, email))

    def has_identity(self, email: str) -> bool:
        """Check if email already has identity.

        Args:
            email: Email address to check

        Returns:
            True if identity exists
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT 1 FROM accounts WHERE email = ? AND identity_name IS NOT NULL
        """, (email,))
        return cursor.fetchone() is not None

    # ---------------------------------------------------------------------------
    # BINs management (входные данные для Step 4)
    # ---------------------------------------------------------------------------

    def add_bin(self, bin: str) -> None:
        """Add BIN code to bins table.

        Args:
            bin: BIN code (6 digits)
        """
        with self._transaction() as cursor:
            cursor.execute("""
                INSERT OR IGNORE INTO bins (bin, status)
                VALUES (?, 'pending')
            """, (bin,))

    def get_pending_bins(self, limit: int | None = None) -> list[str]:
        """Get pending BIN codes for card checking.

        Args:
            limit: Maximum number of BINs to return

        Returns:
            List of BIN codes with status='pending'
        """
        conn = self._get_conn()
        query = """
            SELECT bin FROM bins
            WHERE status = 'pending'
            ORDER BY created_at
        """
        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query)
        return [row['bin'] for row in cursor.fetchall()]

    def get_all_bins(self) -> list[dict]:
        """Get all BIN codes for GUI display.

        Returns:
            List of dicts with keys: id, bin, status, created_at
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT id, bin, status, created_at FROM bins
            ORDER BY created_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    def mark_bin_processing(self, bin: str) -> None:
        """Mark BIN as being processed.

        Args:
            bin: BIN code being processed
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE bins SET status = 'processing'
                WHERE bin = ?
            """, (bin,))

    def mark_bin_done(self, bin: str) -> None:
        """Mark BIN as done (cards checked).

        Args:
            bin: BIN code that was processed
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE bins SET status = 'done'
                WHERE bin = ?
            """, (bin,))

    def delete_bin(self, bin_id: int) -> None:
        """Delete BIN by ID.

        Args:
            bin_id: ID of BIN to delete
        """
        with self._transaction() as cursor:
            cursor.execute("DELETE FROM bins WHERE id = ?", (bin_id,))

    def search_bins(self, query: str | None = None, status: str | None = None) -> list[dict]:
        """Search BINs by query and/or status.

        Args:
            query: Search query (matches bin)
            status: Filter by status

        Returns:
            List of dicts with keys: id, bin, status, created_at
        """
        conn = self._get_conn()
        sql = "SELECT id, bin, status, created_at FROM bins WHERE 1=1"
        params = []

        if query:
            sql += " AND bin LIKE ?"
            params.append(f"%{query}%")

        if status:
            sql += " AND status = ?"
            params.append(status)

        sql += " ORDER BY created_at DESC"

        cursor = conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    # ---------------------------------------------------------------------------
    # Cards management (результаты Step 4)
    # ---------------------------------------------------------------------------

    def add_card(self, card_number: str, exp_month: str, exp_year: str,
                 cvv: str, bin: str | None = None, status: str = 'live') -> int:
        """Add card to cards table.

        Args:
            card_number: Card number
            exp_month: Expiration month (MM)
            exp_year: Expiration year (YYYY)
            cvv: CVV code
            bin: BIN code (optional)
            status: Card status (live, confirmed, used, failed)

        Returns:
            Card ID
        """
        with self._transaction() as cursor:
            cursor.execute("""
                INSERT OR IGNORE INTO cards
                (card_number, exp_month, exp_year, cvv, bin, status, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (card_number, exp_month, exp_year, cvv, bin, status,
                  datetime.utcnow().isoformat()))
            return cursor.lastrowid

    def get_cards(self, status: str | None = None, limit: int | None = None) -> list[dict]:
        """Get cards by status.

        Args:
            status: Filter by status (live, confirmed, used, failed)
            limit: Maximum number of cards to return

        Returns:
            List of dicts with card data
        """
        conn = self._get_conn()
        query = "SELECT * FROM cards WHERE 1=1"
        params = []

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY checked_at DESC"

        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_all_cards(self) -> list[dict]:
        """Get all cards for GUI display.

        Returns:
            List of dicts with card data
        """
        return self.get_cards(status=None, limit=None)

    def mark_card_confirmed(self, card_id: int) -> None:
        """Mark card as confirmed (passed recheck).

        Args:
            card_id: ID of card to mark
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE cards
                SET status = 'confirmed', confirmed_at = ?
                WHERE id = ?
            """, (datetime.utcnow().isoformat(), card_id))

    def mark_card_used(self, card_id: int, email: str) -> None:
        """Mark card as used by an account.

        Args:
            card_id: ID of card to mark
            email: Email that used this card
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE cards
                SET status = 'used', used_at = ?, used_by_email = ?
                WHERE id = ?
            """, (datetime.utcnow().isoformat(), email, card_id))

    def mark_card_failed(self, card_id: int) -> None:
        """Mark card as failed.

        Args:
            card_id: ID of card to mark
        """
        with self._transaction() as cursor:
            cursor.execute("""
                UPDATE cards SET status = 'failed'
                WHERE id = ?
            """, (card_id,))

    def delete_card(self, card_id: int) -> None:
        """Delete card by ID.

        Args:
            card_id: ID of card to delete
        """
        with self._transaction() as cursor:
            cursor.execute("DELETE FROM cards WHERE id = ?", (card_id,))

    def search_cards(self, query: str | None = None, status: str | None = None) -> list[dict]:
        """Search cards by query and/or status.

        Args:
            query: Search query (matches card_number or bin)
            status: Filter by status

        Returns:
            List of dicts with card data
        """
        conn = self._get_conn()
        sql = "SELECT * FROM cards WHERE 1=1"
        params = []

        if query:
            sql += " AND (card_number LIKE ? OR bin LIKE ?)"
            params.extend([f"%{query}%", f"%{query}%"])

        if status:
            sql += " AND status = ?"
            params.append(status)

        sql += " ORDER BY checked_at DESC"

        cursor = conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_cards_by_bin(self, bin: str) -> list[dict]:
        """Get all cards for a specific BIN.

        Args:
            bin: BIN code

        Returns:
            List of dicts with card data
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT * FROM cards WHERE bin = ?
            ORDER BY checked_at DESC
        """, (bin,))
        return [dict(row) for row in cursor.fetchall()]

    def get_used_cards(self) -> set[str]:
        """Get set of used cards in NUMBER|MM|YYYY|CVV format.

        Returns:
            Set of card strings for activate_accounts.py
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT card_number, exp_month, exp_year, cvv FROM cards
            WHERE status IN ('used', 'failed')
        """)
        return {f"{row['card_number']}|{row['exp_month']}|{row['exp_year']}|{row['cvv']}"
                for row in cursor.fetchall()}

    def get_accepted_cards(self) -> list[tuple[str, str]]:
        """Get list of accepted cards (email, card) pairs.

        Returns:
            List of (email, card) tuples for activate_accounts.py
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT used_by_email, card_number, exp_month, exp_year, cvv FROM cards
            WHERE status = 'used' AND used_by_email IS NOT NULL
        """)
        return [(row['used_by_email'],
                 f"{row['card_number']}|{row['exp_month']}|{row['exp_year']}|{row['cvv']}")
                for row in cursor.fetchall()]

    # ---------------------------------------------------------------------------
    # Card attempts (история для activate_accounts.py)
    # ---------------------------------------------------------------------------

    def add_card_attempt(self, card: str, email: str, outcome: str) -> None:
        """Record card attempt.

        Args:
            card: Card in NUMBER|MM|YYYY|CVV format
            email: Email that attempted the card
            outcome: Outcome (success, declined, timeout, used)
        """
        with self._transaction() as cursor:
            cursor.execute("""
                INSERT INTO card_attempts (card, email, outcome, attempted_at)
                VALUES (?, ?, ?, ?)
            """, (card, email, outcome, datetime.utcnow().isoformat()))

    def get_card_attempts(self, email: str | None = None) -> list[dict]:
        """Get card attempt history.

        Args:
            email: Filter by email (optional)

        Returns:
            List of dicts with attempt data
        """
        conn = self._get_conn()
        if email:
            cursor = conn.execute("""
                SELECT * FROM card_attempts
                WHERE email = ?
                ORDER BY attempted_at DESC
            """, (email,))
        else:
            cursor = conn.execute("""
                SELECT * FROM card_attempts
                ORDER BY attempted_at DESC
            """)
        return [dict(row) for row in cursor.fetchall()]

    # ---------------------------------------------------------------------------
    # Activated accounts (финальный результат)
    # ---------------------------------------------------------------------------

    def add_activated_account(self, email: str, card: str, holder_name: str) -> None:
        """Record activated account.

        Args:
            email: Email that was activated
            card: Card used for activation
            holder_name: Cardholder name used
        """
        with self._transaction() as cursor:
            cursor.execute("""
                INSERT OR REPLACE INTO activated_accounts
                (email, card, holder_name, activated_at)
                VALUES (?, ?, ?, ?)
            """, (email, card, holder_name, datetime.utcnow().isoformat()))

    def get_activated_accounts(self) -> list[dict]:
        """Get all activated accounts.

        Returns:
            List of dicts with activation data
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT * FROM activated_accounts
            ORDER BY activated_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    def is_account_activated(self, email: str) -> bool:
        """Check if account is already activated.

        Args:
            email: Email to check

        Returns:
            True if activated
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT 1 FROM activated_accounts WHERE email = ?
        """, (email,))
        return cursor.fetchone() is not None

    # ---------------------------------------------------------------------------
    # Extended methods for external scripts
    # ---------------------------------------------------------------------------

    def get_all_accounts(self) -> list[dict]:
        """Get all accounts with full data.

        Returns:
            List of dicts with all account fields
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT * FROM accounts
            WHERE password IS NOT NULL
            ORDER BY created_at
        """)
        return [dict(row) for row in cursor.fetchall()]

    def get_devin_accounts(self, status: str = 'success') -> list[dict]:
        """Get Devin accounts by status with credentials.

        Args:
            status: Filter by devin_status

        Returns:
            List of dicts with at least ``email`` and ``password`` keys.
            Остальные поля из ``accounts`` тоже включены — удобно для
            вывода в GUI/пиплайнах.
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT * FROM accounts
            WHERE devin_status = ?
            ORDER BY devin_registered_at
        """, (status,))
        return [dict(row) for row in cursor.fetchall()]

    def get_all_identities(self) -> dict[str, dict]:
        """Get all identities as dict.

        Returns:
            Dict {email: {name, address}} for activate_accounts.py
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT email, identity_name, identity_address FROM accounts
            WHERE identity_name IS NOT NULL
        """)
        return {row['email'].lower(): {
            'full_name': row['identity_name'],
            'street': row['identity_address'].split(', ')[0] if ', ' in row['identity_address'] else '',
            'zip_code': row['identity_address'].split(', ')[1] if row['identity_address'].count(', ') >= 1 else '',
            'city': row['identity_address'].split(', ')[2] if row['identity_address'].count(', ') >= 2 else '',
        } for row in cursor.fetchall()}

    def get_identity_by_email(self, email: str) -> dict | None:
        """Get identity for specific email.

        Args:
            email: Email address

        Returns:
            Dict with identity data or None
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT identity_name, identity_address FROM accounts
            WHERE email = ? AND identity_name IS NOT NULL
        """, (email,))
        row = cursor.fetchone()
        if not row:
            return None

        address_parts = row['identity_address'].split(', ') if row['identity_address'] else []
        return {
            'full_name': row['identity_name'],
            'street': address_parts[0] if len(address_parts) > 0 else '',
            'zip_code': address_parts[1] if len(address_parts) > 1 else '',
            'city': address_parts[2] if len(address_parts) > 2 else '',
        }

    # ---------------------------------------------------------------------------
    # Export to .txt files (backward compatibility)
    # ---------------------------------------------------------------------------

    def export_emails_txt(self, output_path: Path) -> int:
        """Export email:password pairs to имейлы pingmx.txt format.

        Args:
            output_path: Path to output file

        Returns:
            Number of records exported
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT email, password FROM accounts
            WHERE password IS NOT NULL
            ORDER BY created_at
        """)

        with output_path.open("w", encoding="utf-8") as f:
            count = 0
            for row in cursor.fetchall():
                f.write(f"{row['email']}:{row['password']}\n")
                count += 1

        return count

    def export_taken_txt(self, output_path: Path) -> int:
        """Export taken nicknames to taken.txt format.

        Args:
            output_path: Path to output file

        Returns:
            Number of records exported
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT nick FROM accounts
            WHERE devin_status = 'taken' AND nick IS NOT NULL
            ORDER BY created_at
        """)

        with output_path.open("w", encoding="utf-8") as f:
            count = 0
            for row in cursor.fetchall():
                f.write(f"{row['nick']}\n")
                count += 1

        return count

    def export_devin_accounts_txt(self, output_path: Path) -> int:
        """Export successful Devin accounts to аккаунты devin.txt format.

        Args:
            output_path: Path to output file

        Returns:
            Number of records exported
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT email FROM accounts
            WHERE devin_status = 'success'
            ORDER BY devin_registered_at
        """)

        with output_path.open("w", encoding="utf-8") as f:
            count = 0
            for row in cursor.fetchall():
                f.write(f"{row['email']}\n")
                count += 1

        return count

    def export_devin_errors_txt(self, output_path: Path) -> int:
        """Export Devin registration errors to devin_errors.txt format.

        Args:
            output_path: Path to output file

        Returns:
            Number of records exported
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT email, devin_error FROM accounts
            WHERE devin_status = 'error'
            ORDER BY created_at
        """)

        with output_path.open("w", encoding="utf-8") as f:
            count = 0
            for row in cursor.fetchall():
                f.write(f"{row['email']}\t{row['devin_error']}\n")
                count += 1

        return count

    def export_identities_txt(self, output_path: Path) -> int:
        """Export identities to личности.txt format (TSV).

        Args:
            output_path: Path to output file

        Returns:
            Number of records exported
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT email, identity_name, identity_address FROM accounts
            WHERE identity_name IS NOT NULL
            ORDER BY devin_registered_at
        """)

        with output_path.open("w", encoding="utf-8") as f:
            count = 0
            for row in cursor.fetchall():
                f.write(f"{row['email']}\t{row['identity_name']}, {row['identity_address']}\n")
                count += 1

        return count

    # ---------------------------------------------------------------------------
    # Import from .txt files (migration)
    # ---------------------------------------------------------------------------

    def import_from_txt_files(
        self,
        emails_path: Path,
        taken_path: Path,
        devin_accounts_path: Path,
        devin_errors_path: Path,
        identities_path: Path,
    ) -> dict[str, int]:
        """Import existing data from .txt files into database.

        Args:
            emails_path: Path to имейлы pingmx.txt
            taken_path: Path to taken.txt
            devin_accounts_path: Path to аккаунты devin.txt
            devin_errors_path: Path to devin_errors.txt
            identities_path: Path to личности.txt

        Returns:
            Dict with counts: {emails, taken, devin_success, devin_errors, identities}
        """
        counts = {
            'emails': 0,
            'taken': 0,
            'devin_success': 0,
            'devin_errors': 0,
            'identities': 0,
        }

        # Import emails
        if emails_path.exists():
            for line in emails_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or ':' not in line:
                    continue
                email, password = line.split(':', 1)
                nick = email.split('@')[0]
                self.add_email(email, password, nick)
                counts['emails'] += 1

        # Import taken nicks
        if taken_path.exists():
            for line in taken_path.read_text(encoding="utf-8").splitlines():
                nick = line.strip()
                if nick:
                    self.mark_nick_taken(nick)
                    counts['taken'] += 1

        # Import Devin successes
        if devin_accounts_path.exists():
            for line in devin_accounts_path.read_text(encoding="utf-8").splitlines():
                email = line.strip()
                if email:
                    self.mark_devin_success(email)
                    counts['devin_success'] += 1

        # Import Devin errors
        if devin_errors_path.exists():
            for line in devin_errors_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or '\t' not in line:
                    continue
                email, error = line.split('\t', 1)
                self.mark_devin_error(email, error)
                counts['devin_errors'] += 1

        # Import identities
        if identities_path.exists():
            for line in identities_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or '\t' not in line:
                    continue
                email, identity = line.split('\t', 1)
                # Split "Name, Address" format
                if ', ' in identity:
                    parts = identity.split(', ', 1)
                    name = parts[0]
                    address = parts[1] if len(parts) > 1 else ''
                    self.add_identity(email, name, address)
                    counts['identities'] += 1

        return counts

    # ---------------------------------------------------------------------------
    # Import/Export for new tables
    # ---------------------------------------------------------------------------

    def import_nicks_from_txt(self, path: Path) -> int:
        """Import nicknames from .txt file.

        Args:
            path: Path to file with nicknames (one per line)

        Returns:
            Number of nicks imported
        """
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            nick = line.strip().lstrip("﻿")
            if nick and not nick.startswith("#"):
                self.add_nick(nick)
                count += 1
        return count

    def import_bins_from_txt(self, path: Path) -> int:
        """Import BIN codes from .txt file.

        Args:
            path: Path to file with BINs (one per line)

        Returns:
            Number of BINs imported
        """
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            bin = line.strip().lstrip("﻿")
            if bin and not bin.startswith("#"):
                self.add_bin(bin)
                count += 1
        return count

    def import_cards_from_txt(self, path: Path, status: str = 'live') -> int:
        """Import cards from .txt file.

        Args:
            path: Path to file with cards (NUMBER|MM|YYYY|CVV format)
            status: Status to assign (live or confirmed)

        Returns:
            Number of cards imported
        """
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip().lstrip("﻿")
            if not line or line.startswith("#") or '|' not in line:
                continue

            parts = line.split('|')
            if len(parts) >= 4:
                card_number, exp_month, exp_year, cvv = parts[0], parts[1], parts[2], parts[3]
                bin = card_number[:6] if len(card_number) >= 6 else None
                self.add_card(card_number, exp_month, exp_year, cvv, bin, status)
                count += 1
        return count

    def export_nicks_to_txt(self, path: Path, status: str | None = None) -> int:
        """Export nicknames to .txt file.

        Args:
            path: Output file path
            status: Filter by status (optional)

        Returns:
            Number of nicks exported
        """
        nicks = self.search_nicks(status=status)
        with path.open("w", encoding="utf-8") as f:
            for nick_data in nicks:
                f.write(f"{nick_data['nick']}\n")
        return len(nicks)

    def export_bins_to_txt(self, path: Path, status: str | None = None) -> int:
        """Export BIN codes to .txt file.

        Args:
            path: Output file path
            status: Filter by status (optional)

        Returns:
            Number of BINs exported
        """
        bins = self.search_bins(status=status)
        with path.open("w", encoding="utf-8") as f:
            for bin_data in bins:
                f.write(f"{bin_data['bin']}\n")
        return len(bins)

    def export_cards_to_txt(self, path: Path, status: str | None = None) -> int:
        """Export cards to .txt file in NUMBER|MM|YYYY|CVV format.

        Args:
            path: Output file path
            status: Filter by status (optional)

        Returns:
            Number of cards exported
        """
        cards = self.get_cards(status=status)
        with path.open("w", encoding="utf-8") as f:
            for card in cards:
                f.write(f"{card['card_number']}|{card['exp_month']}|{card['exp_year']}|{card['cvv']}\n")
        return len(cards)

    def export_used_cards_to_txt(self, path: Path) -> int:
        """Export used cards to .txt file.

        Args:
            path: Output file path

        Returns:
            Number of cards exported
        """
        used = self.get_used_cards()
        with path.open("w", encoding="utf-8") as f:
            for card in used:
                f.write(f"{card}\n")
        return len(used)

    def export_accepted_cards_to_txt(self, path: Path) -> int:
        """Export accepted cards to .txt file (email TAB card format).

        Args:
            path: Output file path

        Returns:
            Number of cards exported
        """
        accepted = self.get_accepted_cards()
        with path.open("w", encoding="utf-8") as f:
            for email, card in accepted:
                f.write(f"{email}\t{card}\n")
        return len(accepted)

    def export_activated_accounts_to_txt(self, path: Path) -> int:
        """Export activated accounts to .txt file (email TAB card TAB name format).

        Args:
            path: Output file path

        Returns:
            Number of accounts exported
        """
        accounts = self.get_activated_accounts()
        with path.open("w", encoding="utf-8") as f:
            for acc in accounts:
                f.write(f"{acc['email']}\t{acc['card']}\t{acc['holder_name']}\n")
        return len(accounts)

    def close(self) -> None:
        """Close database connection."""
        if hasattr(self._local, 'conn'):
            self._local.conn.close()
            del self._local.conn

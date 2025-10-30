import sqlite3
from datetime import datetime

DB_PATH = "reservations.db"

def init_db():
    """Initialize the reservations database and ensure all columns exist."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                reservation_id TEXT PRIMARY KEY,
                datetime TEXT,
                business TEXT,
                party_size INTEGER,
                customer_name TEXT,
                customer_email TEXT,
                contact_phone TEXT,
                table_number TEXT,
                notes TEXT,
                status TEXT
            )
        """)
        conn.commit()
    print("✅ Database initialized and columns verified.")

def add_reservation(reservation: dict):
    """Insert a new reservation into the database."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO reservations (
                reservation_id, datetime, business, party_size,
                customer_name, customer_email, contact_phone,
                table_number, notes, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            reservation.get("reservation_id"),
            reservation.get("datetime"),
            reservation.get("business"),
            reservation.get("party_size"),
            reservation.get("customer_name"),
            reservation.get("customer_email"),
            reservation.get("contact_phone"),
            reservation.get("table_number"),
            reservation.get("notes"),
            reservation.get("status", "confirmed"),
        ))
        conn.commit()

def get_reservations():
    """Retrieve all reservations from the database."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT reservation_id, datetime, business, party_size,
                   customer_name, customer_email, contact_phone,
                   table_number, notes, status
            FROM reservations
            ORDER BY datetime DESC
        """)
        rows = cur.fetchall()

    keys = ["reservation_id", "datetime", "business", "party_size",
            "customer_name", "customer_email", "contact_phone",
            "table_number", "notes", "status"]

    return [dict(zip(keys, row)) for row in rows]

def update_status(reservation_id: str, new_status: str) -> bool:
    """Update reservation status (cancelled, confirmed, updated)."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE reservations
            SET status = ?
            WHERE reservation_id = ?
        """, (new_status, reservation_id))
        conn.commit()
        return cur.rowcount > 0

def update_reservation(reservation_id: str, updates: dict) -> bool:
    """Update reservation details such as datetime, party_size, table_number, etc."""
    if not updates:
        return False

    fields = [f"{key} = ?" for key in updates.keys()]
    values = list(updates.values()) + [reservation_id]

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE reservations
            SET {', '.join(fields)}, status = 'updated'
            WHERE reservation_id = ?
        """, values)
        conn.commit()
        return cur.rowcount > 0

def get_insights():
    """Return quick analytics for dashboard overview cards."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM reservations")
        total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM reservations WHERE status='confirmed'")
        confirmed = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM reservations WHERE status='cancelled'")
        cancelled = cur.fetchone()[0]

        today_str = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT COUNT(*) FROM reservations WHERE datetime LIKE ?", (f"{today_str}%",))
        today_reservations = cur.fetchone()[0]

    return {
        "total": total,
        "confirmed": confirmed,
        "cancelled": cancelled,
        "today_reservations": today_reservations
    }

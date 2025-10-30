import sqlite3
from datetime import datetime

def fix_missing_ids():
    conn = sqlite3.connect("reservations.db")
    cur = conn.cursor()

    # Create reservation_id for any rows that don't have one
    cur.execute("SELECT id FROM reservations WHERE reservation_id IS NULL OR reservation_id = ''")
    rows = cur.fetchall()

    count = 0
    for (row_id,) in rows:
        new_id = f"RES-{datetime.now().strftime('%H%M%S')}-{row_id}"
        cur.execute("UPDATE reservations SET reservation_id = ? WHERE id = ?", (new_id, row_id))
        count += 1

    conn.commit()
    conn.close()
    print(f"✅ Fixed {count} reservations missing IDs.")

if __name__ == "__main__":
    fix_missing_ids()

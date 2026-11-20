
from app import get_connection

try:
    conn = get_connection()

    with conn.cursor() as cursor:
        cursor.execute("SELECT version();")
        print(cursor.fetchone()[0])

    print("Flask database configuration works!")

except Exception as e:
    print(type(e).__name__, str(e))

finally:
    if "conn" in locals():
        conn.close()
